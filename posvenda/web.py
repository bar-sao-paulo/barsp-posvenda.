"""Painel do pós-venda (celular): lista do dia, envio pelo WhatsApp, respostas e quem não quer receber.

Rodar no Railway: gunicorn "posvenda.web:criar_app()" --bind 0.0.0.0:$PORT
Variáveis: DATABASE_URL, TAKEAT_API_KEY, TAKEAT_BASE_URL (opcional), SECRET_KEY, LOGIN_USUARIO, LOGIN_SENHA.
Etapa 2 (opcionais): WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN,
WHATSAPP_API_VERSION, WHATSAPP_WABA_ID, WHATSAPP_MODELO_A/B/C, ANTHROPIC_API_KEY, ENVIO_AUTOMATICO, ENVIO_HORA, RESPOSTA_AUTOMATICA,
ALERTA_EMAIL, RESEND_API_KEY, EMAIL_FROM, PAINEL_URL.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

from . import automacao, clientes, historico, lista, respostas, servico
from .db import criar_engine
from .takeat import TakeatClient, TakeatError, TakeatForbidden
from .whatsapp import WhatsApp, WhatsAppErro, assinatura_valida, ler_webhook

log = logging.getLogger("posvenda.web")


def _buscar_takeat():
    chave = os.environ.get("TAKEAT_API_KEY")
    if not chave:
        return None
    cliente = TakeatClient(chave, base_url=os.environ.get("TAKEAT_BASE_URL", "https://public-api.takeat.app"),
                           scopes=("table-sessions:read",))
    return cliente.listar_sessoes


def _buscar_clube():
    chave = os.environ.get("TAKEAT_API_KEY")
    if not chave:
        return None
    cliente = TakeatClient(chave, base_url=os.environ.get("TAKEAT_BASE_URL", "https://public-api.takeat.app"),
                           scopes=("clube:read",))
    return cliente.listar_clientes_clube


def _whatsapp():
    token, numero = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not numero:
        return None
    return WhatsApp(token, numero, versao=os.environ.get("WHATSAPP_API_VERSION", "v23.0"))


def _classificador():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from .ia import Classificador
    return Classificador()


def _transcritor():
    chave = os.environ.get("OPENAI_API_KEY")
    if not chave:
        return None
    from .transcricao import MODELO_PADRAO, Transcritor
    return Transcritor(chave, modelo=os.environ.get("TRANSCRICAO_MODELO", MODELO_PADRAO))


def _alerta():
    destino, chave, remetente = (os.environ.get(x) for x in ("ALERTA_EMAIL", "RESEND_API_KEY", "EMAIL_FROM"))
    if not (destino and chave and remetente):
        return None
    return automacao.alerta_por_email(destino, chave, remetente, os.environ.get("PAINEL_URL", ""))


_PADRAO = object()


def criar_app(engine=None, buscar_sessoes=None, usuario: str | None = None, senha: str | None = None,
              hoje=servico.hoje_local, whatsapp=_PADRAO, classificar=_PADRAO, app_secret: str | None = None,
              verify_token: str | None = None, config: automacao.Config | None = None,
              agendador: bool | None = None, buscar_clientes=_PADRAO) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    chave = os.environ.get("SECRET_KEY")
    if not chave:
        log.warning("SECRET_KEY não definida: os logins caem a cada reinício.")
    app.secret_key = chave or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    app.jinja_loader = DictLoader(TEMPLATES)

    @app.context_processor
    def _telefone_na_tela():
        def ver_telefone(envio_id: int, telefone: str) -> Markup:
            """Telefone mascarado + botão "Ver telefone": o número completo só vem quando alguém clica."""
            return Markup('{} <button type="button" class="btn2 mini" onclick="verTelefone(this, {})">Ver telefone'
                          '</button>').format(lista.mascarar(telefone), int(envio_id))
        return {"ver_telefone": ver_telefone}
    producao = engine is None
    engine = engine or criar_engine()
    buscar = buscar_sessoes if buscar_sessoes is not None else _buscar_takeat()
    login_usuario = usuario if usuario is not None else os.environ.get("LOGIN_USUARIO", "")
    login_senha = senha if senha is not None else os.environ.get("LOGIN_SENHA", "")
    wa = _whatsapp() if whatsapp is _PADRAO else whatsapp
    busca_clube = _buscar_clube() if buscar_clientes is _PADRAO else buscar_clientes
    clube = clientes.Cache(busca_clube) if busca_clube else None
    classificador = _classificador() if classificar is _PADRAO else classificar
    segredo_app = app_secret if app_secret is not None else os.environ.get("WHATSAPP_APP_SECRET", "")
    token_verificacao = verify_token if verify_token is not None else os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    cfg = config or automacao.Config.do_ambiente()
    # O agendador só liga no Railway (criar_app() sem argumentos); nos testes fica desligado.
    if agendador if agendador is not None else producao:
        automacao.iniciar_agendador(engine, cfg, buscar=buscar, whatsapp=wa, classificar=classificador,
                                    alertar=_alerta(), transcrever=_transcritor())
    app.config["POSVENDA_CFG"] = cfg

    def _bloquear(telefone: str) -> None:
        d = "".join(c for c in telefone if c.isdigit())
        if len(d) == 12 and d.startswith("55"):  # WhatsApp às vezes manda o celular sem o 9
            d = d[:4] + "9" + d[4:]
        servico.bloquear(engine, d, "Respondeu PARAR no WhatsApp")

    @app.before_request
    def _csrf_e_login():
        if request.endpoint in ("webhook_verificar", "webhook_receber", "api_resumo"):
            return None  # chamado pela Meta (assinatura) ou pelo painel financeiro (token): sem login nem CSRF
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST":
            enviado = request.form.get("csrf") or request.headers.get("X-CSRF", "")
            if not hmac.compare_digest(enviado, session["csrf"]):
                abort(400)
        if request.endpoint in ("entrar", "saude", "static", "privacidade"):
            return None
        if not session.get("usuario"):
            return redirect(url_for("entrar", proximo=request.full_path))
        g.usuario = session["usuario"]
        return None

    def _dia_da_url() -> date:
        texto = request.values.get("data")
        if texto:
            try:
                return date.fromisoformat(texto)
            except ValueError:
                flash("Data inválida; mostrando ontem.")
        return hoje() - timedelta(days=1)

    def _mes_da_url() -> tuple[int, int] | None:
        texto = request.values.get("mes", "")
        try:
            ano, mes = (int(x) for x in texto.split("-"))
            date(ano, mes, 1)
            return ano, mes
        except ValueError:
            if texto:
                flash("Mês inválido; mostrando o dia.")
            return None

    def _periodo_da_url() -> tuple[date, date, date, tuple[int, int] | None]:
        """(de, até, dia, mês): com ?mes=AAAA-MM o mês inteiro (até ontem); senão o dia de ?data=."""
        mes = _mes_da_url()
        if mes is None:
            dia = _dia_da_url()
            return dia, dia, dia, None
        de = date(*mes, 1)
        fim = (de + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        ate = max(de, min(fim, hoje() - timedelta(days=1)))
        return de, ate, de, mes

    def _volta() -> dict:
        """Volta para o mesmo dia ou mês depois de um botão da tela Respostas."""
        if request.form.get("mes"):
            return dict(mes=request.form["mes"])
        return dict(data=request.form.get("data", ""))

    from .api_resumo import registrar as registrar_api_resumo
    registrar_api_resumo(app, engine=engine, hoje=hoje)

    @app.get("/saude")
    def saude():
        return "ok"

    @app.get("/privacidade")
    def privacidade():
        return render_template("privacidade.html")

    @app.route("/entrar", methods=["GET", "POST"])
    def entrar():
        if request.method == "POST":
            u = request.form.get("usuario", "")
            s = request.form.get("senha", "")
            if (login_usuario and login_senha and hmac.compare_digest(u, login_usuario)
                    and hmac.compare_digest(s, login_senha)):
                session.clear()
                session.permanent = True
                session["usuario"] = u
                session["csrf"] = secrets.token_urlsafe(32)
                proximo = request.args.get("proximo") or ""
                return redirect(proximo if proximo.startswith("/") and not proximo.startswith("//") else url_for("envio"))
            flash("Usuário ou senha incorretos." if login_usuario else "Login não configurado no Railway.")
        return render_template("entrar.html")

    @app.post("/sair")
    def sair():
        session.clear()
        return redirect(url_for("entrar"))

    @app.get("/")
    def inicio():
        return redirect(url_for("envio"))

    @app.get("/envio")
    def envio():
        dia = _dia_da_url()
        dados = servico.obter_lista(engine, dia)
        if dados.buscado_em is None and buscar is not None and dia < hoje():
            try:
                dados = servico.gerar_lista(engine, dia, buscar, hoje=hoje())
            except TakeatError as exc:
                log.warning("Falha ao buscar o Takeat: %s", exc)
                flash(f"Não consegui buscar a lista no Takeat: {exc}")
        motivos: dict[str, int] = {}
        for n in dados.nao_enviados:
            motivos[n["motivo"]] = motivos.get(n["motivo"], 0) + 1
        pendentes = sum(1 for e in dados.envios if e["enviado_em"] is None)
        for e, (txt, _) in zip(dados.envios, historico.compras(
                engine, [(e["telefone"], e["data_visita"]) for e in dados.envios], hoje())):
            e["compras"] = txt
        return render_template("envio.html", dados=dados, dia=dia, hoje=hoje(), motivos=motivos,
                               mascarar=lista.mascarar, takeat_ok=buscar is not None, timedelta=timedelta,
                               tz=lista.TZ, wa_ok=wa is not None, pendentes=pendentes, cfg=cfg,
                               rotina=automacao.ultima_rotina(engine, "envio:"))

    @app.post("/envio/buscar")
    def envio_buscar():
        dia = _dia_da_url()
        if buscar is None:
            flash("TAKEAT_API_KEY não configurada no Railway.")
        elif dia >= hoje():
            flash("A lista só pode ser montada para dias que já terminaram.")
        else:
            try:
                dados = servico.gerar_lista(engine, dia, buscar, hoje=hoje())
                flash(f"Lista atualizada do Takeat: {dados.resumo}.")
            except TakeatError as exc:
                log.warning("Falha ao buscar o Takeat: %s", exc)
                flash(f"Não consegui buscar a lista no Takeat: {exc}")
        return redirect(url_for("envio", data=dia.isoformat()))

    @app.post("/envio/whatsapp")
    def envio_whatsapp():
        dia = _dia_da_url()
        if wa is None:
            flash("WhatsApp não configurado no Railway (WHATSAPP_TOKEN e WHATSAPP_PHONE_NUMBER_ID).")
        else:
            res = automacao.enviar_dia(engine, dia, wa.enviar_modelo, cfg.modelos)
            flash(f"Pesquisa pelo WhatsApp: {res.resumo}."
                  + (f" Primeiro erro: {res.erros[0]}" if res.erros else ""))
        return redirect(url_for("envio", data=dia.isoformat()))

    @app.post("/envio/<int:envio_id>/enviado")
    def envio_enviado(envio_id: int):
        if not servico.marcar_enviado(engine, envio_id, request.form.get("mensagem")):
            abort(404)
        return jsonify(ok=True)

    @app.post("/envio/<int:envio_id>/desfazer")
    def envio_desfazer(envio_id: int):
        servico.desmarcar_enviado(engine, envio_id)
        return redirect(url_for("envio", data=request.form.get("data", "")))

    @app.get("/envio/lista.csv")
    def envio_csv():
        dia = _dia_da_url()
        corpo = servico.csv_lista_do_envio(servico.obter_lista(engine, dia))
        return Response(corpo, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=lista-do-envio-{dia.isoformat()}.csv"})

    # ----------------------------------------------------------------------- clientes do Clube (Takeat)
    TOPOS = (20, 50, 100, 200)

    def _selecao() -> tuple[int | None, int]:
        topo = request.values.get("topo", "50")
        try:
            minimo = max(0, int(request.values.get("minimo") or 0))
        except ValueError:
            minimo = 0
        return (int(topo) if topo.isdigit() and int(topo) in TOPOS else None), minimo, request.values.get("busca", "")[:60]

    def _clientes_do_clube(atualizar: bool = False) -> tuple[list, str | None]:
        if clube is None:
            return [], "Takeat não configurado no Railway (TAKEAT_API_KEY)."
        try:
            return clube.obter(atualizar), None
        except TakeatForbidden:
            return [], ("A chave do Takeat ainda não tem permissão para ler o Clube. No Takeat, edite as permissões "
                        "da chave da API e marque clube:read (Clube de Fidelidade, leitura).")
        except TakeatError as exc:
            log.warning("Clube do Takeat indisponível: %s", exc)
            return [], f"Não consegui ler o Clube do Takeat: {exc}"

    @app.get("/clientes")
    def clientes_pagina():
        topo, minimo, busca = _selecao()
        todos, erro = _clientes_do_clube()
        escolhidos = clientes.selecionar(todos, topo, minimo, busca)
        bloqueados = {lista.chave_telefone(b["telefone"]) for b in servico.listar_bloqueios(engine)}
        pratos = historico.pratos_favoritos(engine, [c.telefone for c in escolhidos], hoje())
        ultimos = historico.ultimos_pedidos(engine, [c.telefone for c in escolhidos])
        return render_template("clientes.html", pratos=pratos, ultimos=ultimos, carregando=not historico.completo(engine, hoje()), todos=todos, escolhidos=escolhidos, erro=erro, topo=topo,
                               minimo=minimo, busca=busca, primeiro_nome=clientes.primeiro_nome, TOPOS=TOPOS, formatar=lista.formatar_telefone, chave=lista.chave_telefone,
                               bloqueados=bloqueados, idade=clube.idade_minutos() if clube else None)

    @app.post("/clientes/atualizar")
    def clientes_atualizar():
        _, erro = _clientes_do_clube(atualizar=True)
        flash(erro or "Lista do Clube atualizada do Takeat.")
        return redirect(url_for("clientes_pagina", topo=request.form.get("topo", "50"),
                                minimo=request.form.get("minimo", "0"), busca=request.form.get("busca", "")))

    @app.get("/clientes/planilha.csv")
    def clientes_csv():
        topo, minimo, busca = _selecao()
        todos, erro = _clientes_do_clube()
        if erro:
            flash(erro)
            return redirect(url_for("clientes_pagina"))
        corpo = clientes.planilha_csv(clientes.selecionar(todos, topo, minimo, busca))
        return Response(corpo, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=clientes-mais-pedidos.csv"})

    @app.route("/nao-enviar", methods=["GET", "POST"])
    def nao_enviar():
        if request.method == "POST":
            if request.form.get("remover"):
                servico.desbloquear(engine, request.form["remover"])
                flash("Telefone removido da lista de quem não quer receber.")
            else:
                tel = servico.bloquear(engine, request.form.get("telefone", ""), request.form.get("motivo", ""))
                flash("Telefone incluído: não recebe mais a pesquisa." if tel else
                      "Telefone inválido. Use DDD + número, ex.: (11) 98765-4321.")
            return redirect(url_for("nao_enviar"))
        return render_template("nao_enviar.html", bloqueios=servico.listar_bloqueios(engine),
                               formatar=lista.formatar_telefone)

    # ----------------------------------------------------------------------- webhook do WhatsApp (Meta)
    @app.get("/webhook/whatsapp")
    def webhook_verificar():
        if (request.args.get("hub.mode") == "subscribe" and token_verificacao
                and hmac.compare_digest(request.args.get("hub.verify_token", ""), token_verificacao)):
            return request.args.get("hub.challenge", "")
        abort(403)

    @app.post("/webhook/whatsapp")
    def webhook_receber():
        corpo = request.get_data()
        if not assinatura_valida(segredo_app, corpo, request.headers.get("X-Hub-Signature-256")):
            abort(403)
        recebidas, situacoes = ler_webhook(request.get_json(silent=True) or {})
        for m in recebidas:
            try:
                respostas.registrar_recebida(engine, m.telefone, m.texto, m.wa_id, m.quando,
                                             enviar_texto=wa.enviar_texto if wa else None, bloquear=_bloquear,
                                             midia_id=m.midia_id, midia_tipo=m.midia_tipo)
            except Exception:
                log.exception("Falha ao gravar mensagem recebida")
        for st in situacoes:
            try:
                respostas.atualizar_situacao(engine, st.wa_id, st.status, st.erro)
            except Exception:
                log.exception("Falha ao gravar situação do envio")
        return "ok"

    # ----------------------------------------------------------------------- respostas (planilha do dia)
    @app.get("/respostas")
    def respostas_pagina():
        de, ate, dia, mes = _periodo_da_url()
        rows = respostas.listar(engine, de, ate, hoje())
        fora = []
        if mes is None:
            inicio = datetime.combine(dia + timedelta(days=1), time(), lista.TZ)
            fora = respostas.fora_da_lista(engine, inicio, inicio + timedelta(days=2))
        for m in fora:
            q = m["criado_em"]
            m["quando"] = (q if q.tzinfo else q.replace(tzinfo=timezone.utc)).astimezone(lista.TZ).strftime("%d/%m %H:%M")
        mes_nps = mes or (dia.year, dia.month)
        inicio_mes = date(*mes_nps, 1)
        fim_mes = (inicio_mes + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        meses = respostas.meses_com_respostas(engine)
        if mes and mes not in meses:
            meses = sorted({*meses, mes}, reverse=True)
        return render_template("respostas.html", rows=rows, dia=dia, hoje=hoje(), timedelta=timedelta,
                               mes=f"{mes[0]}-{mes[1]:02d}" if mes else "", meses=meses,
                               nome_do_mes=respostas.nome_do_mes, mes_nps=respostas.nome_do_mes(*mes_nps),
                               nps=respostas.nps(engine, inicio_mes, fim_mes),
                               contagem=respostas.contagem(rows), fora=fora,
                               positivas=len(respostas.positivas_para_enviar(rows)), mascarar=lista.mascarar,
                               wa_ok=wa is not None, ia_ok=classificador is not None, R=respostas, tz=lista.TZ)

    @app.post("/respostas/<int:resposta_id>/enviar")
    def respostas_enviar(resposta_id: int):
        if wa is None:
            flash("WhatsApp não configurado no Railway.")
        else:
            try:
                respostas.enviar_resposta(engine, resposta_id, request.form.get("texto", ""), wa.enviar_texto)
                flash("Resposta enviada.")
            except (respostas.RespostaErro, WhatsAppErro) as exc:
                flash(str(exc))
        return redirect(url_for("respostas_pagina", **_volta()) + f"#r{resposta_id}")

    @app.post("/respostas/<int:resposta_id>/audio")
    def respostas_audio(resposta_id: int):
        try:
            respostas.escrever_audio(engine, resposta_id, request.form.get("texto", ""))
            flash("Anotado. A IA refaz a nota e a resposta em instantes (recarregue a página).")
        except respostas.RespostaErro as exc:
            flash(str(exc))
        return redirect(url_for("respostas_pagina", **_volta()) + f"#r{resposta_id}")

    @app.get("/telefone/<int:envio_id>")
    def telefone(envio_id: int):
        """Número completo de um cliente da lista, para ligar (só com login; o painel mostra mascarado)."""
        tel = servico.telefone_do_envio(engine, envio_id)
        if not tel:
            abort(404)
        log.info("Telefone completo mostrado (envio %s) para %s", envio_id, g.usuario)
        resp = jsonify(telefone=tel, formatado=lista.formatar_telefone(tel))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @app.get("/midia/<int:mensagem_id>")
    def midia(mensagem_id: int):
        """Toca o áudio (ou mostra a foto) do cliente, buscando na Meta na hora. O arquivo não é guardado."""
        midia_id = respostas.midia_da_mensagem(engine, mensagem_id)
        if not midia_id or wa is None:
            abort(404)
        try:
            conteudo, tipo = wa.baixar_midia(midia_id)
        except WhatsAppErro as exc:
            log.warning("Mídia %s indisponível: %s", mensagem_id, exc)
            return Response("A Meta não tem mais esse arquivo (ela guarda por pouco tempo).", status=410,
                            mimetype="text/plain")
        if not tipo.startswith(("audio/", "image/")):
            abort(415)
        return Response(conteudo, mimetype=tipo, headers={"Cache-Control": "private, no-store"})

    @app.post("/respostas/<int:resposta_id>/ignorar")
    def respostas_ignorar(resposta_id: int):
        respostas.ignorar(engine, resposta_id)
        return redirect(url_for("respostas_pagina", **_volta()) + f"#r{resposta_id}")

    @app.post("/respostas/enviar-positivas")
    def respostas_enviar_positivas():
        de, ate, _, _ = _periodo_da_url()
        if wa is None:
            flash("WhatsApp não configurado no Railway.")
        else:
            res = respostas.enviar_positivas(engine, de, wa.enviar_texto, ate=ate)
            flash(f"{res.enviadas} resposta(s) 🟢 enviada(s)."
                  + (f" {len(res.erros)} não saíram e voltaram para revisão. Primeiro erro: {res.erros[0]}"
                     if res.erros else ""))
        return redirect(url_for("respostas_pagina", **_volta()))

    @app.post("/respostas/analisar")
    def respostas_analisar():
        if classificador is None:
            flash("ANTHROPIC_API_KEY não configurada no Railway.")
        else:
            n = respostas.processar_pendentes(engine, classificador, espera=timedelta(0))
            flash(f"{n} resposta(s) analisada(s)." if n else "Nenhuma resposta aguardando análise.")
        return redirect(url_for("respostas_pagina", **_volta()))

    @app.get("/respostas/planilha.csv")
    def respostas_csv():
        de, ate, dia, mes = _periodo_da_url()
        corpo = respostas.planilha_csv(respostas.listar(engine, de, ate))
        nome = f"{mes[0]}-{mes[1]:02d}" if mes else dia.isoformat()
        return Response(corpo, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=pos-venda-{nome}.csv"})

    # ----------------------------------------------------------------------- número do WhatsApp (registro)
    @app.route("/whatsapp", methods=["GET", "POST"])
    def whatsapp_numero():
        situacao, erro, ligados, erro_ligados = None, None, None, None
        if wa is not None and request.method == "POST":
            if request.form.get("acao") == "ligar":
                conta = request.form.get("waba", "").strip()
                try:
                    wa.ligar_app(conta)
                    nomes = wa.apps_ligados(conta)
                    flash("Recebimento ligado. Apps que recebem as mensagens desta conta: "
                          + (", ".join(nomes) or "nenhum") + ".")
                except WhatsAppErro as exc:
                    flash(f"Não consegui ligar o recebimento: {exc}")
                return redirect(url_for("whatsapp_numero", waba=conta))
            # O PIN vai direto para a Meta: não é gravado, nem mostrado, nem vai para o log.
            try:
                wa.registrar_numero(request.form.get("pin", "").strip())
                flash("Número registrado na Meta. Em alguns minutos o WhatsApp Manager deve mostrar \"Conectado\".")
            except WhatsAppErro as exc:
                flash(f"Não consegui registrar o número: {exc}")
            return redirect(url_for("whatsapp_numero"))
        conta = request.args.get("waba") or os.environ.get("WHATSAPP_WABA_ID", "")
        if wa is not None:
            try:
                situacao = wa.situacao_do_numero()
            except WhatsAppErro as exc:
                erro = str(exc)
            if conta:
                try:
                    ligados = wa.apps_ligados(conta)
                except WhatsAppErro as exc:
                    erro_ligados = str(exc)
        return render_template("whatsapp.html", wa_ok=wa is not None, situacao=situacao, erro=erro,
                               STATUS=_STATUS_NUMERO, conta=conta, ligados=ligados, erro_ligados=erro_ligados)

    return app


_STATUS_NUMERO = {
    "CONNECTED": "Conectado", "PENDING": "Pendente (falta registrar)", "DISCONNECTED": "Desconectado",
    "FLAGGED": "Sinalizado pela Meta", "RESTRICTED": "Restrito", "BANNED": "Bloqueado pela Meta",
    "MIGRATED": "Migrado", "UNVERIFIED": "Não verificado", "DELETED": "Apagado",
    "APPROVED": "Aprovado", "PENDING_REVIEW": "Em análise", "DECLINED": "Recusado", "EXPIRED": "Expirado",
    "AVAILABLE_WITHOUT_REVIEW": "Liberado sem análise", "NONE": "Sem análise", "VERIFIED": "Verificado",
    "NOT_VERIFIED": "Não verificado", "GREEN": "Alta", "YELLOW": "Média", "RED": "Baixa", "UNKNOWN": "Ainda sem avaliação",
}


TEMPLATES = {
    "base.html": """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pós-venda · Bar São Paulo</title>
<style>
:root{--bg:#f6f4f0;--card:#fff;--tx:#1f1d1a;--qt:#6b655c;--ac:#1f7a4d;--bd:#e3ded5;--al:#b3261e}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--tx)}
header{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;background:#1f1d1a;color:#fff}
header a{color:#fff;text-decoration:none;margin-right:14px}header form{margin:0}
main{max-width:980px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin:0 0 14px}
.flash{background:#fff7e0;border:1px solid #ecd9a0;border-radius:8px;padding:10px;margin:0 0 12px}
.resumo{font-weight:600;font-size:16px}.quieto{color:var(--qt);font-size:13px}
button,.btn{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:9px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
.btn2{background:#fff;color:var(--tx);border:1px solid var(--bd)}
.mini{padding:2px 8px;font-size:12px;min-height:0;width:auto}
input,textarea{font:inherit;padding:8px;border:1px solid var(--bd);border-radius:8px;width:100%}
textarea{min-height:92px;resize:vertical}
.cli{display:grid;gap:8px}.cli .topo{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
.tag{border:1px solid var(--bd);border-radius:999px;padding:1px 9px;font-size:13px}
.enviado{opacity:.6}.ok{color:var(--ac);font-weight:600}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--bd);font-size:14px}
.linha{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.erro{color:var(--al)}.critico{border-left:5px solid var(--al)}.atencao{border-left:5px solid #d9a400}.positivo{border-left:5px solid var(--ac)}
.fala{background:#f1efe9;border-radius:8px;padding:8px 10px;white-space:pre-wrap}
select{font:inherit;padding:8px;border:1px solid var(--bd);border-radius:8px;background:#fff}
.topo-resp{display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between;align-items:flex-start}
.filtros{display:grid;gap:8px}.nps{border:1px solid var(--bd);border-radius:10px;padding:10px 14px;min-width:220px;max-width:320px}
.nps-valor{font-size:34px;font-weight:700;line-height:1.1}.positivo-tx{color:var(--ac)}.atencao-tx{color:#a37b00}
</style></head><body>
{% if g.usuario %}<header><nav><a href="{{ url_for('envio') }}">Envio</a><a href="{{ url_for('respostas_pagina') }}">Respostas</a><a href="{{ url_for('clientes_pagina') }}">Clientes</a><a href="{{ url_for('nao_enviar') }}">Não enviar</a><a href="{{ url_for('whatsapp_numero') }}">WhatsApp</a></nav>
<form method="post" action="{{ url_for('sair') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}"><button class="btn2">Sair</button></form></header>{% endif %}
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block corpo %}{% endblock %}</main>
<script>
function verTelefone(b, id) {
  fetch('/telefone/' + id).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (!d) { b.textContent = 'não consegui mostrar'; return; }
    var s = document.createElement('span');
    s.textContent = d.formatado + ' ';
    var a = document.createElement('a'); a.href = 'tel:+' + d.telefone; a.textContent = '📞 Ligar';
    s.appendChild(a); b.replaceWith(s);
  });
}
</script></body></html>""",

    "entrar.html": """{% extends 'base.html' %}{% block corpo %}<div class="card" style="max-width:380px;margin:40px auto">
<h2 style="margin-top:0">Pós-venda · Bar São Paulo</h2><form method="post"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<p><input name="usuario" placeholder="Usuário" autocomplete="username" required></p>
<p><input name="senha" type="password" placeholder="Senha" autocomplete="current-password" required></p>
<button>Entrar</button></form></div>{% endblock %}""",

    "envio.html": """{% extends 'base.html' %}{% block corpo %}
<div class="card"><form method="get" class="linha"><label>Dia da visita/pedido</label>
<input type="date" name="data" value="{{ dia.isoformat() }}" max="{{ (hoje - timedelta(days=1)).isoformat() }}" style="width:auto">
<button class="btn2">Ver</button></form>
{% if dados.buscado_em %}<p class="resumo">{{ dados.resumo }}</p>
<p class="quieto">Lista buscada no Takeat em {{ dados.buscado_em.astimezone(tz).strftime('%d/%m %H:%M') if dados.buscado_em.tzinfo else dados.buscado_em.strftime('%d/%m %H:%M') }}.
Toque em "Abrir no WhatsApp", envie a mensagem e volte para o próximo.</p>
{% else %}<p class="quieto">A lista deste dia ainda não foi buscada.</p>{% endif %}
<div class="linha">{% if takeat_ok and dia < hoje %}<form method="post" action="{{ url_for('envio_buscar') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input type="hidden" name="data" value="{{ dia.isoformat() }}"><button class="btn2">Buscar de novo no Takeat</button></form>{% endif %}
{% if dados.envios %}<a class="btn btn2" href="{{ url_for('envio_csv', data=dia.isoformat()) }}">Baixar "Lista do envio" (CSV)</a>{% endif %}</div>
{% if wa_ok and pendentes %}<form method="post" action="{{ url_for('envio_whatsapp') }}" style="margin-top:10px"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input type="hidden" name="data" value="{{ dia.isoformat() }}">
<button onclick="return confirm('Mandar a pesquisa pelo WhatsApp para {{ pendentes }} cliente(s) agora?')">Enviar agora pelo WhatsApp ({{ pendentes }})</button></form>{% endif %}
<p class="quieto">Envio automático: {{ ('ligado, às %dh' % cfg.hora) if cfg.envio_automatico else 'desligado' }}{% if rotina %} · último: {{ rotina.resultado or 'rodando' }}{% endif %}.</p></div>

{% for e in dados.envios %}<div class="card cli{{ ' enviado' if e.enviado_em }}" id="e{{ e.id }}">
<div class="topo"><strong>{{ e.nome_completo or e.nome or 'Sem nome' }}</strong><span class="tag">{{ e.origem }}</span>
<span class="tag">Versão {{ e.versao }}</span><span class="quieto">{{ e.prato or 'seu pedido' }} · {{ ver_telefone(e.id, e.telefone) }} · 🛒 {{ e.compras }}</span></div>
{% if e.itens %}<p class="quieto"><b>Consumiu:</b> {{ e.itens }}</p>{% endif %}
<textarea id="m{{ e.id }}" aria-label="Mensagem">{{ e.mensagem }}</textarea>
{% if e.erro %}<p class="erro">Erro no WhatsApp: {{ e.erro }}</p>{% endif %}
<div class="linha">{% if e.enviado_em %}<span class="ok">✓ enviado{% if e.wa_status %} · {{ {'sent': 'saiu', 'delivered': 'entregue', 'read': 'lida', 'failed': 'falhou'}.get(e.wa_status, e.wa_status) }}{% endif %}</span>
<form method="post" action="{{ url_for('envio_desfazer', envio_id=e.id) }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input type="hidden" name="data" value="{{ dia.isoformat() }}"><button class="btn2">Desfazer</button></form>
{% else %}<button type="button" onclick="abrir({{ e.id }}, '{{ e.telefone }}')">Abrir no WhatsApp</button>{% endif %}</div></div>
{% endfor %}

{% if dados.nao_enviados %}<div class="card"><h3 style="margin-top:0">Não enviados</h3>
<p class="quieto">{% for m, n in motivos.items() %}{{ m }}: {{ n }}{{ ' · ' if not loop.last }}{% endfor %}</p>
<table><tr><th>Cliente</th><th>Origem</th><th>Motivo</th></tr>
{% for n in dados.nao_enviados %}<tr><td>{{ n.nome_completo or '—' }}</td><td>{{ n.origem }}</td><td>{{ n.motivo }}</td></tr>{% endfor %}</table></div>{% endif %}
<script>
function abrir(id, tel) {
  var msg = document.getElementById('m' + id).value;
  window.open('https://wa.me/' + tel + '?text=' + encodeURIComponent(msg), '_blank');
  var dados = new FormData(); dados.append('csrf', '{{ session['csrf'] }}'); dados.append('mensagem', msg);
  fetch('/envio/' + id + '/enviado', {method: 'POST', body: dados}).then(function (r) {
    if (r.ok) { var c = document.getElementById('e' + id); c.classList.add('enviado');
      c.querySelector('.linha').innerHTML = '<span class="ok">✓ enviado</span>'; }
  });
}
</script>{% endblock %}""",

    "respostas.html": """{% extends 'base.html' %}{% block corpo %}
{% set periodo %}{% if mes %}<input type="hidden" name="mes" value="{{ mes }}">{% else %}{{ periodo }}{% endif %}{% endset %}
<div class="card"><div class="topo-resp"><div class="filtros">
<form method="get" class="linha"><label>Dia da visita/pedido</label>
<input type="date" name="data" value="{{ dia.isoformat() if not mes else '' }}" max="{{ (hoje - timedelta(days=1)).isoformat() }}" style="width:auto">
<button class="btn2">Ver</button></form>
<form method="get" class="linha"><label>Mês das pesquisas</label>
<select name="mes" onchange="this.form.submit()"><option value="">escolha o mês</option>{% for a, m in meses %}
<option value="{{ '%d-%02d' % (a, m) }}"{{ ' selected' if mes == '%d-%02d' % (a, m) }}>{{ nome_do_mes(a, m) }}</option>{% endfor %}</select>
<noscript><button class="btn2">Ver</button></noscript></form>
{% if mes %}<p class="quieto" style="margin:0">Mostrando o mês inteiro. Para ver um dia só, escolha o dia acima.</p>{% endif %}
</div>
<div class="nps"><div class="quieto">NPS de {{ mes_nps }}</div>
{% if nps %}<div class="nps-valor {{ 'positivo-tx' if nps.valor >= 50 else 'atencao-tx' if nps.valor >= 0 else 'erro' }}">{{ nps.valor }}</div>
<div class="quieto">{{ nps.faixa }} · {{ nps.total }} nota{{ 's' if nps.total != 1 }}</div>
<div class="quieto">{{ nps.promotores }} promotores (9–10) · {{ nps.neutros }} neutros (7–8) · {{ nps.detratores }} detratores (0–6)</div>
{% if nps.estimadas %}<div class="quieto">{{ nps.estimadas }} nota{{ 's' if nps.estimadas != 1 }} estimada{{ 's' if nps.estimadas != 1 }} pela IA</div>{% endif %}
{% else %}<div class="nps-valor quieto">—</div><div class="quieto">Nenhuma nota neste mês ainda.</div>{% endif %}</div></div>
<p class="resumo">{{ contagem }}</p>
<div class="linha">{% if ia_ok %}<form method="post" action="{{ url_for('respostas_analisar') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
{{ periodo }}<button class="btn2">Analisar agora</button></form>{% else %}<span class="quieto">IA não configurada (ANTHROPIC_API_KEY).</span>{% endif %}
{% if rows %}<a class="btn btn2" href="{{ url_for('respostas_csv', mes=mes) if mes else url_for('respostas_csv', data=dia.isoformat()) }}">Baixar planilha (CSV)</a>{% endif %}
{% if wa_ok and positivas %}<form method="post" action="{{ url_for('respostas_enviar_positivas') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
{{ periodo }}
<button onclick="if(!confirm('Mandar a resposta sugerida para as {{ positivas }} avaliação(ões) 🟢 {{ 'do mês' if mes else 'do dia' }} de uma vez?'))return false;this.disabled=true;this.form.submit();return false">Enviar todas as 🟢 ({{ positivas }})</button></form>{% endif %}</div>
<p class="quieto">Da mais crítica para a menos crítica. 🔴 nunca sai sozinha: revise e envie.</p></div>
{% for r in rows %}<div class="card cli {{ 'critico' if r.gravidade == R.CRITICO else 'atencao' if r.gravidade == R.ATENCAO else 'positivo' if r.gravidade == R.POSITIVO else '' }}" id="r{{ r.id }}">
<div class="topo"><strong>{{ r.nome_completo or r.nome or 'Sem nome' }}</strong><span class="tag">{{ r.origem }}</span><span class="tag">Versão {{ r.versao }}</span>
<span class="quieto">{{ r.prato or 'seu pedido' }} · {{ ver_telefone(r.envio_id, r.telefone) }} · 🛒 {{ r.historico }}</span></div>
{% if r.itens %}<p class="quieto"><b>Consumiu:</b> {{ r.itens }}</p>{% endif %}
<div class="linha"><span class="tag">{{ r.status }}</span>{% if r.nota is not none %}<strong>{{ r.gravidade }} · nota {{ r.nota }}{{ ' (estimada)' if r.nota_estimada }}</strong>{% endif %}</div>
{% for f in r.falas %}{% if f.direcao == 'equipe' %}<div class="fala"><span class="quieto">No áudio (escrito pela equipe):</span> {{ f.texto }}</div>
{% elif f.direcao == 'transcrito' %}<div class="fala"><span class="quieto">No áudio (transcrição automática, confira ouvindo):</span> {{ f.texto }}</div>
{% elif f.midia_id and f.midia_tipo == 'audio' %}<div class="fala">🎤 Áudio{% if wa_ok %} <audio controls preload="none" src="{{ url_for('midia', mensagem_id=f.id) }}"></audio>{% endif %}</div>
{% elif f.midia_id and f.midia_tipo == 'image' %}<div class="fala">📷 Foto{% if wa_ok %} <a href="{{ url_for('midia', mensagem_id=f.id) }}" target="_blank" rel="noopener">abrir</a>{% endif %}{{ f.texto[6:] }}</div>
{% else %}<div class="fala">{{ f.texto }}</div>{% endif %}{% endfor %}
{% if r.audio_pendente %}<form method="post" action="{{ url_for('respostas_audio', resposta_id=r.id) }}" class="cli">
<input type="hidden" name="csrf" value="{{ session['csrf'] }}">{{ periodo }}
<textarea name="texto" aria-label="O que o cliente disse no áudio" placeholder="Ouça o áudio e escreva aqui o que o cliente disse. A IA refaz a nota e a resposta."></textarea>
<div class="linha"><button class="btn2">Salvar o que o cliente disse</button></div></form>{% endif %}
{% if r.resumo %}<p><b>Resumo:</b> {{ r.resumo }}</p>{% endif %}
{% if r.motivo_revisao %}<p class="erro">Revisar: {{ r.motivo_revisao }}</p>{% endif %}
{% if r.acao_sugerida %}<p><b>Ação sugerida:</b> {{ r.acao_sugerida }}</p>{% endif %}
{% if r.status in (R.REVISAR, R.PODE_ENVIAR) %}<form method="post" action="{{ url_for('respostas_enviar', resposta_id=r.id) }}" class="cli">
<input type="hidden" name="csrf" value="{{ session['csrf'] }}">{{ periodo }}
<textarea name="texto" aria-label="Resposta">{{ r.resposta_sugerida }}</textarea>
<div class="linha">{% if wa_ok %}<button>Enviar resposta</button>{% endif %}
<button class="btn2" formaction="{{ url_for('respostas_ignorar', resposta_id=r.id) }}">Não responder</button></div></form>
{% elif r.status == R.RESPONDIDO %}<p class="ok">✓ Respondido: {{ r.resposta_sugerida }}</p>{% endif %}
</div>{% else %}<div class="card quieto">Nenhuma resposta para as visitas {{ 'deste mês' if mes else 'deste dia' }} ainda.</div>{% endfor %}
{% if fora %}<div class="card"><h3 style="margin-top:0">Fora da lista</h3><p class="quieto">Mensagens de números que não estão na lista. Não foram ligadas a ninguém.</p>
<table><tr><th>Quando</th><th>Telefone</th><th>Mensagem</th></tr>{% for m in fora %}<tr><td>{{ m.quando }}</td>
<td>{{ mascarar(m.telefone) }}</td><td>{{ m.texto }}</td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}""",

    "privacidade.html": """{% extends 'base.html' %}{% block corpo %}<div class="card">
<h2 style="margin-top:0">Política de privacidade da pesquisa de satisfação</h2>
<p>Bar São Paulo (CNPJ 54.615.219/0001-24).</p>
<p><b>O que usamos.</b> Depois da sua visita ou do seu pedido no delivery, usamos o primeiro nome, o telefone e o prato
registrados no nosso sistema de vendas para mandar uma pesquisa de satisfação pelo WhatsApp. Guardamos a sua resposta
para melhorar o atendimento.</p>
<p><b>Para quê.</b> Só para saber como foi a sua experiência e responder você. Não usamos esses dados para propaganda
e não vendemos nem cedemos para ninguém.</p>
<p><b>Quem mais vê.</b> A mensagem passa pelo WhatsApp (Meta). Para organizar as respostas, o texto que você escreve
é lido por uma ferramenta de inteligência artificial (Claude, da Anthropic), e uma pessoa da nossa equipe revisa as
respostas mais importantes.</p>
<p><b>Por quanto tempo.</b> Mandamos no máximo uma pesquisa por semana. Os dados ficam guardados no servidor do nosso
painel de pós-venda enquanto forem úteis para o atendimento.</p>
<p><b>Não quer receber?</b> Responda PARAR na conversa e você não recebe mais. Para pedir que apaguemos seus dados ou
tirar dúvidas, fale com a gente pelo mesmo WhatsApp.</p>
</div>{% endblock %}""",

    "whatsapp.html": """{% extends 'base.html' %}{% block corpo %}<div class="card"><h3 style="margin-top:0">Número do WhatsApp</h3>
{% if not wa_ok %}<p class="erro">WhatsApp não configurado no Railway (WHATSAPP_TOKEN e WHATSAPP_PHONE_NUMBER_ID).</p>
{% elif erro %}<p class="erro">Não consegui ler a situação do número: {{ erro }}</p>
{% elif situacao %}<table>
<tr><th>Número</th><td>{{ situacao.display_phone_number or '—' }}</td></tr>
<tr><th>Nome</th><td>{{ situacao.verified_name or '—' }}</td></tr>
<tr><th>Situação</th><td><strong>{{ STATUS.get(situacao.status, situacao.status or '—') }}</strong></td></tr>
<tr><th>Nome de exibição</th><td>{{ STATUS.get(situacao.name_status, situacao.name_status or '—') }}</td></tr>
<tr><th>Verificação por código</th><td>{{ STATUS.get(situacao.code_verification_status, situacao.code_verification_status or '—') }}</td></tr>
<tr><th>Qualidade</th><td>{{ STATUS.get(situacao.quality_rating, situacao.quality_rating or '—') }}</td></tr></table>{% endif %}</div>
{% if wa_ok %}<div class="card"><h3 style="margin-top:0">Registrar o número</h3>
<p class="quieto">Use quando o WhatsApp Manager mostrar o número como "Pendente". Invente um PIN de 6 números e guarde num
lugar seguro: ele vira a senha do número (verificação em duas etapas) e a Meta pode pedir de novo no futuro.
O PIN vai direto para a Meta e não fica gravado no painel.</p>
<form method="post" class="cli" autocomplete="off"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input name="pin" type="password" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="PIN de 6 números"
 autocomplete="new-password" required style="max-width:220px">
<div><button onclick="return confirm('Registrar o número do WhatsApp na Meta com este PIN?')">Registrar número</button></div></form></div>
<div class="card"><h3 style="margin-top:0">Receber as respostas</h3>
<p class="quieto">Liga este app à conta do WhatsApp onde está o número, para a Meta mandar ao painel as mensagens que os
clientes escrevem. A identificação da conta aparece no WhatsApp Manager, no quadro do canto de cima (só números).</p>
{% if ligados is not none %}<p>Apps que recebem as mensagens desta conta: <strong>{{ ligados|join(', ') or 'nenhum' }}</strong></p>{% endif %}
{% if erro_ligados %}<p class="erro">Não consegui ver os apps ligados: {{ erro_ligados }}</p>{% endif %}
<form method="post" class="cli"><input type="hidden" name="csrf" value="{{ session['csrf'] }}"><input type="hidden" name="acao" value="ligar">
<input name="waba" value="{{ conta }}" inputmode="numeric" pattern="[0-9]+" placeholder="Identificação da conta do WhatsApp" required style="max-width:320px">
<div><button>Ligar o recebimento</button></div></form></div>{% endif %}
{% endblock %}""",

    "clientes.html": """{% extends 'base.html' %}{% block corpo %}<div class="card"><h3 style="margin-top:0">Clientes com mais pedidos</h3>
<p class="quieto">Clientes cadastrados no Clube de Fidelidade do Takeat. "Pedidos" é o número de visitas que o Takeat registra.</p>
<form method="get" class="cli"><input name="busca" value="{{ busca }}" placeholder="Buscar cliente pelo nome ou telefone" aria-label="Buscar cliente">
<div class="linha"><label>Mostrar os</label>
<select name="topo">{% for t in TOPOS %}<option value="{{ t }}"{{ ' selected' if topo == t }}>{{ t }}</option>{% endfor %}<option value="todos"{{ ' selected' if not topo }}>todos</option></select>
<label>que mais pediram, com pelo menos</label><input type="number" name="minimo" min="0" value="{{ minimo }}" style="width:90px"><label>pedidos</label>
<button>Buscar</button></div></form>
{% if erro %}<p class="erro">{{ erro }}</p>{% else %}
<div class="linha"><span class="resumo">{{ escolhidos|length }} de {{ todos|length }} clientes do Clube</span>
<form method="post" action="{{ url_for('clientes_atualizar') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input type="hidden" name="topo" value="{{ topo or 'todos' }}"><input type="hidden" name="minimo" value="{{ minimo }}"><input type="hidden" name="busca" value="{{ busca }}">
<button class="btn2 mini">Atualizar do Takeat</button></form>{% if idade is not none %}<span class="quieto">lido há {{ idade }} min</span>{% endif %}</div>{% endif %}</div>
{% if escolhidos %}<div class="card cli"><h3 style="margin:0">Mensagem</h3>
<textarea id="msg" aria-label="Mensagem para os clientes">Oi, {nome}! </textarea>
<p class="quieto" style="margin:0">{nome} vira o primeiro nome de cada cliente. O botão abre o WhatsApp com a mensagem pronta, um cliente por vez: confira e toque em enviar.</p>
<div class="linha"><button id="enviar" type="button" onclick="enviarSelecionados()" disabled>Enviar mensagem aos selecionados (0)</button>
<span id="fila" class="quieto"></span></div></div>
<div class="card" style="overflow-x:auto"><table><tr><th><input type="checkbox" aria-label="Marcar todos" onclick="marcarTodos(this.checked)" style="width:auto"></th><th>#</th><th>Nome</th><th>Telefone</th><th>Pedidos</th><th>Total gasto</th><th>Último pedido</th><th>Prato favorito</th><th></th></tr>
{% for c in escolhidos %}{% set bloq = chave(c.telefone) in bloqueados %}<tr id="c{{ loop.index }}">
<td>{% if c.telefone and not bloq %}<input type="checkbox" class="sel" style="width:auto" aria-label="Selecionar {{ c.nome }}" data-tel="{{ c.telefone }}" data-nome="{{ primeiro_nome(c.nome) }}" data-linha="c{{ loop.index }}" onchange="contar()">{% endif %}</td>
<td>{{ loop.index }}</td><td>{{ c.nome or '—' }}{% if bloq %} <span class="tag">não enviar</span>{% endif %}</td>
<td style="white-space:nowrap">{{ formatar(c.telefone) if c.telefone else '—' }}</td><td><strong>{{ c.pedidos }}</strong></td>
<td>{{ ('R$ %.2f' % c.total_gasto).replace('.', ',') }}</td>
<td>{% set u = ultimos.get(chave(c.telefone)) %}{{ u.strftime('%d/%m/%Y') if u else ('carregando…' if carregando else '—') }}</td><td>{{ pratos.get(chave(c.telefone)) or ('carregando…' if carregando else '—') }}</td>
<td class="estado"></td></tr>{% endfor %}</table></div>
<script>
var fila = [], pos = 0;
function selecionados() { return Array.prototype.slice.call(document.querySelectorAll('.sel:checked')); }
function marcarTodos(v) { document.querySelectorAll('.sel').forEach(function (c) { c.checked = v; }); contar(); }
function contar() { fila = []; pos = 0; var n = selecionados().length; var b = document.getElementById('enviar');
  b.disabled = !n; b.textContent = 'Enviar mensagem aos selecionados (' + n + ')'; document.getElementById('fila').textContent = ''; }
function enviarSelecionados() {
  var texto = document.getElementById('msg').value.trim();
  if (!texto || texto === 'Oi, {nome}!') { alert('Escreva a mensagem antes de enviar.'); return; }
  if (!fila.length) { fila = selecionados(); pos = 0; }
  var c = fila[pos]; if (!c) return;
  window.open('https://wa.me/' + c.dataset.tel + '?text=' + encodeURIComponent(texto.split('{nome}').join(c.dataset.nome)), '_blank');
  document.querySelector('#' + c.dataset.linha + ' .estado').innerHTML = '<span class="ok">✓ aberto</span>';
  pos += 1; var b = document.getElementById('enviar');
  if (pos < fila.length) { b.textContent = 'Próximo: ' + (fila[pos].dataset.nome || 'cliente') + ' (' + (pos + 1) + ' de ' + fila.length + ')'; }
  else { b.textContent = 'Pronto: ' + fila.length + ' mensagem(ns) aberta(s)'; b.disabled = true; }
}
</script>{% endif %}
{% endblock %}""",

    "nao_enviar.html": """{% extends 'base.html' %}{% block corpo %}<div class="card"><h3 style="margin-top:0">Não enviar a pesquisa</h3>
<p class="quieto">Quem está aqui nunca entra na lista do envio.</p>
<form method="post" class="cli"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input name="telefone" placeholder="Telefone com DDD, ex.: (11) 98765-4321" required>
<input name="motivo" placeholder="Motivo (opcional)"><div><button>Incluir</button></div></form></div>
{% if bloqueios %}<div class="card"><table><tr><th>Nome</th><th>Telefone</th><th>Motivo</th><th>Desde</th><th></th></tr>
{% for b in bloqueios %}<tr><td>{{ b.nome or '—' }}</td><td>{{ formatar(b.telefone) }}</td><td>{{ b.motivo or '—' }}</td><td>{{ b.criado_em.strftime('%d/%m/%Y') }}</td>
<td><form method="post"><input type="hidden" name="csrf" value="{{ session['csrf'] }}"><input type="hidden" name="remover" value="{{ b.telefone }}">
<button class="btn2" onclick="return confirm('Voltar a enviar a pesquisa para este telefone?')">Remover</button></form></td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}""",
}
