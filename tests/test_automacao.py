"""Etapa 2: envio pelo WhatsApp, webhook, análise das respostas e planilha (tudo com dados e chaves fictícios)."""
import hashlib
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from posvenda import automacao, respostas, servico
from posvenda.db import criar_engine, mensagens, metadata, respostas as t_respostas
from posvenda.ia import Analise, IAErro, _ler
from posvenda.web import criar_app
from posvenda.whatsapp import WhatsApp, WhatsAppErro, assinatura_valida, ler_webhook
from tests.dados import sessoes_de_02_10

HOJE = date(2026, 10, 3)
DIA = date(2026, 10, 2)
SEGREDO = "segredo-ficticio"
AGORA = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
JOAO = "5511900000023"


class WhatsAppFalso:
    def __init__(self, falhar=()):
        self.modelos, self.textos, self.falhar = [], [], set(falhar)

    def enviar_modelo(self, telefone, modelo, parametros, idioma="pt_BR"):
        if telefone in self.falhar:
            raise WhatsAppErro("WhatsApp recusou (HTTP 400, código 131026): número sem WhatsApp", 131026)
        self.modelos.append((telefone, modelo, parametros))
        return f"wamid.{len(self.modelos)}"

    def enviar_texto(self, telefone, texto):
        self.textos.append((telefone, texto))
        return f"wamid.t{len(self.textos)}"

    def situacao_do_numero(self):
        return {"display_phone_number": "+55 11 90000-0000", "verified_name": "Bar Fictício", "status": "PENDING",
                "name_status": "APPROVED", "quality_rating": "UNKNOWN"}

    def ligar_app(self, waba):
        self.ligados = getattr(self, "ligados", []) + [waba]

    def apps_ligados(self, waba):
        return ["Pos-venda Ficticio"] if waba in getattr(self, "ligados", []) else []

    def registrar_numero(self, pin):
        self.registros = getattr(self, "registros", []) + [pin]


def analise(nota=10, estimada=False, resposta="Oi, João! Ficamos felizes que tenha gostado. Volte sempre!",
            ressalva="", compensacao=False, resumo="Gostou da picanha"):
    return Analise(nota, estimada, resumo, ressalva, resposta, "Agradecer e convidar para voltar", compensacao)


class ClaudeFalso:
    def __init__(self, a=None, erro=False):
        self.a, self.erro, self.chamadas = a or analise(), erro, []

    def __call__(self, cliente, textos, historico):
        self.chamadas.append((cliente["telefone"], textos, historico))
        if self.erro:
            raise IAErro("A IA não respondeu (HTTP 529).")
        return self.a


@pytest.fixture
def engine():
    # POSVENDA_TEST_PG="postgresql+psycopg://usuario@localhost:5433/banco" roda também contra o Postgres.
    url = os.environ.get("POSVENDA_TEST_PG")
    if url:
        metadata.drop_all(criar_engine(url))
    e = criar_engine(url or "sqlite://")
    servico.gerar_lista(e, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    return e


def _enviar_tudo(engine, wa=None):
    wa = wa or WhatsAppFalso()
    return automacao.enviar_dia(engine, DIA, wa.enviar_modelo), wa


def _receber(engine, texto, telefone=JOAO, wa_id="wamid.in1", quando=None, wa=None):
    return respostas.registrar_recebida(engine, telefone, texto, wa_id, quando or AGORA - timedelta(hours=1),
                                        enviar_texto=wa.enviar_texto if wa else None,
                                        bloquear=lambda t: servico.bloquear(engine, t, "PARAR"))


def _resposta(engine):
    with engine.connect() as con:
        return con.execute(select(t_respostas)).first()


# --------------------------------------------------------------------------- whatsapp.py
def test_assinatura_do_webhook():
    corpo = b'{"a":1}'
    certo = "sha256=" + hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
    assert assinatura_valida(SEGREDO, corpo, certo)
    assert not assinatura_valida(SEGREDO, corpo + b" ", certo)
    assert not assinatura_valida("", corpo, certo)
    assert not assinatura_valida(SEGREDO, corpo, None)


def test_ler_webhook_mensagens_e_situacoes():
    payload = {"entry": [{"changes": [{"value": {
        "messages": [
            {"from": JOAO, "id": "m1", "timestamp": "1790000000", "type": "text", "text": {"body": " Nota 9 "}},
            {"from": JOAO, "id": "m2", "timestamp": "1790000001", "type": "button", "button": {"text": "Parar"}},
            {"from": JOAO, "id": "m3", "timestamp": "1790000002", "type": "image", "image": {"caption": "olha"}},
        ],
        "statuses": [{"id": "wamid.1", "status": "failed",
                      "errors": [{"code": 131026, "error_data": {"details": "sem WhatsApp"}}]}],
    }}]}]}
    rec, sit = ler_webhook(payload)
    assert [r.texto for r in rec] == ["Nota 9", "Parar", "[foto] olha"]
    assert sit[0].status == "failed" and "131026" in sit[0].erro


def test_whatsapp_erro_nao_expoe_token():
    class Resp:
        status_code = 401

        def json(self):
            return {"error": {"code": 190, "message": "Invalid OAuth access token"}}

    class Sessao:
        def post(self, url, json, timeout, headers):
            self.headers = headers
            return Resp()

    s = Sessao()
    wa = WhatsApp("token-ficticio", "123", session=s)
    with pytest.raises(WhatsAppErro) as exc:
        wa.enviar_texto(JOAO, "oi")
    assert "token-ficticio" not in str(exc.value) and "token-ficticio" not in repr(wa)
    assert exc.value.codigo == 190


def test_registrar_numero_manda_pin_para_a_meta():
    class Resp:
        status_code = 200

        def json(self):
            return {"success": True}

    class Sessao:
        def post(self, url, json, timeout, headers):
            self.url, self.json = url, json
            return Resp()

    s = Sessao()
    wa = WhatsApp("token-ficticio", "123", versao="v23.0", session=s)
    wa.registrar_numero("246813")
    assert s.url == "https://graph.facebook.com/v23.0/123/register"
    assert s.json == {"messaging_product": "whatsapp", "pin": "246813"}
    for ruim in ("12345", "abcdef", "1234567"):
        with pytest.raises(WhatsAppErro):
            wa.registrar_numero(ruim)


def test_ligar_app_na_conta_do_whatsapp():
    class Resp:
        status_code = 200

        def __init__(self, dados):
            self.dados = dados

        def json(self):
            return self.dados

    class Sessao:
        def post(self, url, json, timeout, headers):
            self.post_url = url
            return Resp({"success": True})

        def get(self, url, params, timeout, headers):
            self.get_url = url
            return Resp({"data": [{"whatsapp_business_api_data": {"name": "Pos-venda Ficticio", "id": "9"}}]})

    s = Sessao()
    wa = WhatsApp("token-ficticio", "123", versao="v23.0", session=s)
    wa.ligar_app("777")
    assert s.post_url == "https://graph.facebook.com/v23.0/777/subscribed_apps"
    assert wa.apps_ligados("777") == ["Pos-venda Ficticio"]
    with pytest.raises(WhatsAppErro):
        wa.ligar_app("777/../x")


def test_ia_le_json_e_limita_nota():
    a = _ler(json.dumps({"nota": 14, "nota_estimada": False, "resumo": "x", "ressalva": "", "resposta_sugerida": "y",
                         "acao_sugerida": "z", "pediu_compensacao": False}))
    assert a.nota == 10
    with pytest.raises(IAErro):
        _ler("não é json")


# --------------------------------------------------------------------------- envio automático
def test_enviar_dia_usa_modelo_e_marca_enviado(engine):
    res, wa = _enviar_tudo(engine)
    assert res.enviados == 5 and res.falhas == 0
    joao = next(m for m in wa.modelos if m[0] == JOAO)
    versao = next(e for e in servico.obter_lista(engine, DIA).envios if e["telefone"] == JOAO)["versao"]
    assert joao[1] == f"pesquisa_{versao.lower()}"
    assert joao[2] == ["João", "a picanha cheff"]
    assert all(e["enviado_em"] and e["wa_status"] == "sent" for e in servico.obter_lista(engine, DIA).envios)
    assert automacao.enviar_dia(engine, DIA, wa.enviar_modelo).enviados == 0  # nunca duas vezes


def test_enviar_dia_sem_nome_usa_tudo_bem():
    assert automacao.parametros_do_modelo({"nome": None, "prato": None}) == ["tudo bem", "seu pedido"]


def test_enviar_dia_pula_bloqueado_e_guarda_erro(engine):
    servico.bloquear(engine, "(11) 90000-0025")
    res, _ = _enviar_tudo(engine, WhatsAppFalso(falhar={"5511900000024"}))
    assert (res.enviados, res.falhas, res.pulados) == (3, 1, 1)
    falhou = next(e for e in servico.obter_lista(engine, DIA).envios if e["telefone"] == "5511900000024")
    assert falhou["enviado_em"] is None and "131026" in falhou["erro"]


def test_envio_do_dia_respeita_hora_e_trava(engine):
    wa = WhatsAppFalso()
    buscar = lambda i, f: sessoes_de_02_10()
    cedo = datetime(2026, 10, 3, 10, 59)
    assert automacao.envio_do_dia(engine, cedo, buscar, wa.enviar_modelo, hora=11) is None
    res = automacao.envio_do_dia(engine, datetime(2026, 10, 3, 11, 0), buscar, wa.enviar_modelo, hora=11)
    assert res.enviados == 5
    assert automacao.envio_do_dia(engine, datetime(2026, 10, 3, 12, 0), buscar, wa.enviar_modelo, hora=11) is None
    assert automacao.ultima_rotina(engine, "envio:")["resultado"] == "5 enviadas"


def test_rodar_uma_vez_desligado_nao_envia(engine):
    wa = WhatsAppFalso()
    automacao.rodar_uma_vez(engine, automacao.Config(envio_automatico=False), buscar=lambda i, f: [], whatsapp=wa,
                            agora=AGORA)
    assert wa.modelos == []


# --------------------------------------------------------------------------- respostas
def test_resposta_liga_pelo_telefone_mesmo_sem_o_9(engine):
    _enviar_tudo(engine)
    r = _receber(engine, "Nota 10, tudo ótimo", telefone="551100000023")  # sem o 9
    assert r.acao == "resposta"
    assert _resposta(engine).status == respostas.AGUARDANDO


def test_numero_fora_da_lista_nao_e_ligado(engine):
    _enviar_tudo(engine)
    assert _receber(engine, "quem é?", telefone="5521988887777").acao == "fora_da_lista"
    assert _resposta(engine) is None
    fora = respostas.fora_da_lista(engine, AGORA - timedelta(days=1), AGORA)
    assert len(fora) == 1


def test_mensagem_repetida_do_webhook_e_ignorada(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 10")
    assert _receber(engine, "Nota 10").acao == "duplicada"


def test_parar_bloqueia_e_confirma(engine):
    _, _ = _enviar_tudo(engine)
    wa = WhatsAppFalso()
    assert _receber(engine, "PARAR", wa=wa).acao == "parar"
    assert JOAO in servico.telefones_bloqueados(engine)
    assert wa.textos == [(JOAO, respostas.CONFIRMACAO_PARAR)]
    assert _resposta(engine).status == respostas.IGNORADO


def test_positiva_pode_enviar_e_sai_sozinha_so_com_flag(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 10, a picanha estava perfeita")
    wa = WhatsAppFalso()
    n = respostas.processar_pendentes(engine, ClaudeFalso(), agora=AGORA, enviar_texto=wa.enviar_texto)
    assert n == 1 and wa.textos == []
    r = _resposta(engine)
    assert r.status == respostas.PODE_ENVIAR and r.gravidade == respostas.POSITIVO

    with engine.begin() as con:
        con.execute(t_respostas.update().values(status=respostas.AGUARDANDO))
    respostas.processar_pendentes(engine, ClaudeFalso(), agora=AGORA, enviar_texto=wa.enviar_texto,
                                  resposta_automatica=True)
    assert len(wa.textos) == 1 and _resposta(engine).status == respostas.RESPONDIDO


def test_critica_nunca_sai_sozinha_e_avisa(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 3, demorou muito")
    wa, alertas = WhatsAppFalso(), []
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=3, resumo="Demora")), agora=AGORA,
                                  enviar_texto=wa.enviar_texto, resposta_automatica=True, alertar=alertas.append)
    r = _resposta(engine)
    assert r.status == respostas.REVISAR and r.gravidade == respostas.CRITICO and "crítica" in r.motivo_revisao
    assert wa.textos == [] and len(alertas) == 1 and r.alerta_em is not None


def test_espera_o_cliente_terminar_de_escrever(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 10", quando=AGORA - timedelta(minutes=2))
    assert respostas.processar_pendentes(engine, ClaudeFalso(), agora=AGORA) == 0


def test_ambigua_vira_8_e_ressalva_vai_para_resumo_e_acao(engine):
    _enviar_tudo(engine)
    _receber(engine, "ok")
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=10, estimada=True, ressalva="preço alto")),
                                  agora=AGORA)
    r = _resposta(engine)
    assert r.nota == 8 and r.gravidade == respostas.ATENCAO
    assert "preço alto" in r.resumo and "preço alto" in r.acao_sugerida


def test_resposta_com_compensacao_ou_vocabulario_proibido_vai_para_revisao(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 9")
    ruim = "Oi, João! Infelizmente atrasou. Na próxima tem desconto 🎉🎉🎉"
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=9, resposta=ruim)), agora=AGORA)
    r = _resposta(engine)
    assert r.status == respostas.REVISAR
    for m in ("vocabulário proibido", "compensação", "mais de 2 emojis"):
        assert m in r.motivo_revisao


def test_falha_da_ia_vai_para_revisao(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 9")
    respostas.processar_pendentes(engine, ClaudeFalso(erro=True), agora=AGORA)
    assert _resposta(engine).status == respostas.REVISAR


def test_resposta_fora_das_24h_e_recusada(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 9", quando=AGORA - timedelta(hours=30))
    with pytest.raises(respostas.RespostaErro):
        respostas.enviar_resposta(engine, _resposta(engine).id, "Oi!", WhatsAppFalso().enviar_texto, AGORA)


def test_planilha_ordena_da_mais_critica(engine):
    _enviar_tudo(engine)
    _receber(engine, "Nota 10", wa_id="a")
    _receber(engine, "Nota 2", telefone="5511900000025", wa_id="b")
    claude = ClaudeFalso()
    respostas.processar_pendentes(engine, lambda c, t, h: analise(nota=2) if "2" in t[0] else analise(), agora=AGORA)
    rows = respostas.listar(engine, DIA, DIA)
    assert [r["nota"] for r in rows] == [2, 10]
    assert respostas.contagem(rows) == "🔴 1 críticos | 🟡 0 atenção | 🟢 1 positivos"
    csv = respostas.planilha_csv(rows).splitlines()
    assert csv[0].startswith("Data;Cliente;Telefone") and len(csv) == 3
    assert not claude.chamadas


# --------------------------------------------------------------------------- web
@pytest.fixture
def app_wa(engine):
    wa = WhatsAppFalso()
    app = criar_app(engine=engine, buscar_sessoes=lambda i, f: sessoes_de_02_10(), usuario="thais",
                    senha="senha-ficticia", hoje=lambda: HOJE, whatsapp=wa, classificar=ClaudeFalso(),
                    app_secret=SEGREDO, verify_token="verifica-ficticio")
    app.config["TESTING"] = True
    return app, wa


def _logado(app):
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    with c.session_transaction() as s:
        return c, s["csrf"]


def test_webhook_verificacao(app_wa):
    app, _ = app_wa
    c = app.test_client()
    ok = c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=verifica-ficticio&hub.challenge=42")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "42"
    assert c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=errado&hub.challenge=42").status_code == 403


def test_webhook_exige_assinatura_e_grava(app_wa, engine):
    app, _ = app_wa
    _enviar_tudo(engine)
    c = app.test_client()
    corpo = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": JOAO, "id": "m1", "timestamp": "1790000000", "type": "text", "text": {"body": "Nota 10"}}]}}]}]}).encode()
    assert c.post("/webhook/whatsapp", data=corpo, content_type="application/json").status_code == 403
    assin = "sha256=" + hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
    r = c.post("/webhook/whatsapp", data=corpo, content_type="application/json",
               headers={"X-Hub-Signature-256": assin})
    assert r.status_code == 200
    with engine.connect() as con:
        assert con.execute(select(mensagens.c.texto)).scalar() == "Nota 10"
    assert _resposta(engine).status == respostas.AGUARDANDO


def test_botao_enviar_agora(app_wa, engine):
    app, wa = app_wa
    c, csrf = _logado(app)
    pagina = c.get("/envio?data=2026-10-02").get_data(as_text=True)
    assert "Enviar agora pelo WhatsApp (5)" in pagina
    r = c.post("/envio/whatsapp", data={"csrf": csrf, "data": "2026-10-02"}, follow_redirects=True)
    assert "5 enviadas" in r.get_data(as_text=True) and len(wa.modelos) == 5


def test_pagina_respostas_e_envio_manual(app_wa, engine):
    app, wa = app_wa
    _enviar_tudo(engine)
    _receber(engine, "Nota 3, demorou", quando=datetime.now(timezone.utc) - timedelta(minutes=30))
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=3)), espera=timedelta(0))
    c, csrf = _logado(app)
    pagina = c.get("/respostas?data=2026-10-02").get_data(as_text=True)
    assert "🔴 1 críticos" in pagina and "Nota 3, demorou" in pagina and "90000-0023" not in pagina
    rid = _resposta(engine).id
    c.post(f"/respostas/{rid}/enviar", data={"csrf": csrf, "data": "2026-10-02", "texto": "Oi, João! Desculpa."})
    assert wa.textos == [(JOAO, "Oi, João! Desculpa.")] and _resposta(engine).status == respostas.RESPONDIDO
    csv = c.get("/respostas/planilha.csv?data=2026-10-02")
    assert csv.status_code == 200 and "Gravidade" in csv.get_data(as_text=True)


def test_privacidade_e_publica(app_wa):
    app, _ = app_wa
    r = app.test_client().get("/privacidade")
    assert r.status_code == 200 and "PARAR" in r.get_data(as_text=True)


def test_pagina_do_numero_mostra_situacao_e_registra(app_wa):
    app, wa = app_wa
    c, csrf = _logado(app)
    pagina = c.get("/whatsapp").get_data(as_text=True)
    assert "Pendente (falta registrar)" in pagina and "Registrar número" in pagina
    r = c.post("/whatsapp", data={"csrf": csrf, "pin": "246813"}, follow_redirects=True)
    assert wa.registros == ["246813"]
    texto = r.get_data(as_text=True)
    assert "Número registrado" in texto and "246813" not in texto
    assert c.post("/whatsapp", data={"pin": "246813"}).status_code == 400  # sem CSRF


def test_pagina_do_numero_liga_o_recebimento(app_wa):
    app, wa = app_wa
    c, csrf = _logado(app)
    r = c.post("/whatsapp", data={"csrf": csrf, "acao": "ligar", "waba": "777"}, follow_redirects=True)
    texto = r.get_data(as_text=True)
    assert wa.ligados == ["777"] and "Recebimento ligado" in texto and "Pos-venda Ficticio" in texto
    assert not getattr(wa, "registros", [])  # ligar não registra o número


def test_enviar_todas_as_positivas(app_wa, engine):
    app, wa = app_wa
    _enviar_tudo(engine)
    _receber(engine, "Nota 10, tudo ótimo", quando=datetime.now(timezone.utc) - timedelta(minutes=30))
    _receber(engine, "Nota 3", telefone="5511900000025", wa_id="wamid.in2",
             quando=datetime.now(timezone.utc) - timedelta(minutes=30))
    respostas.processar_pendentes(engine, ClaudeFalso(), espera=timedelta(0))  # as duas saem com nota 10 da IA falsa...
    with engine.begin() as con:  # ...e a segunda vira 🔴, que nunca entra no lote
        rid = con.execute(select(t_respostas.c.id).order_by(t_respostas.c.id.desc())).scalar()
        con.execute(t_respostas.update().where(t_respostas.c.id == rid).values(
            nota=3, gravidade=respostas.CRITICO, status=respostas.REVISAR))
    c, csrf = _logado(app)
    pagina = c.get("/respostas?data=2026-10-02").get_data(as_text=True)
    assert "Enviar todas as 🟢 (1)" in pagina
    r = c.post("/respostas/enviar-positivas", data={"csrf": csrf, "data": "2026-10-02"}, follow_redirects=True)
    assert "1 resposta(s) 🟢 enviada(s)" in r.get_data(as_text=True)
    assert [t[0] for t in wa.textos] == [JOAO]
    c.post("/respostas/enviar-positivas", data={"csrf": csrf, "data": "2026-10-02"})  # segundo clique: nada
    assert len(wa.textos) == 1


def test_lote_fora_das_24h_volta_para_revisao(engine):
    _enviar_tudo(engine)
    antiga = datetime.now(timezone.utc) - timedelta(hours=30)
    _receber(engine, "Nota 10", quando=antiga)
    respostas.processar_pendentes(engine, ClaudeFalso(), espera=timedelta(0))
    wa = WhatsAppFalso()
    res = respostas.enviar_positivas(engine, DIA, wa.enviar_texto)
    assert res.enviadas == 0 and len(res.erros) == 1 and wa.textos == []
    assert _resposta(engine).status == respostas.REVISAR


def test_prompt_nao_obriga_frase_repetida():
    from posvenda.ia import SISTEMA
    assert "não em toda mensagem" in SISTEMA


@pytest.mark.parametrize("textos", [["👍"], ["💗"], ["👍🏾"], ["Ótimo!"], ["boa tarde Thais, tudo certo"],
                                    ["Oi", "Tava td bem"], ["Amei ❤️"], ["Muito bom, obrigada"]])
def test_elogio_curto_e_positivo(textos):
    assert respostas.eh_positiva_curta(textos)


@pytest.mark.parametrize("textos", [["ok"], ["Oi"], ["Oi, boa tarde"], ["👎"], ["ótimo, mas demorou"],
                                    ["Mensagem automática: estamos fora do horário"], ["😡"], ["bom, mas frio"]])
def test_nao_e_elogio_curto(textos):
    assert not respostas.eh_positiva_curta(textos)


def test_elogio_curto_vira_9_estimada(engine):
    _enviar_tudo(engine)
    _receber(engine, "boa tarde Thais, tudo certo")
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=8, estimada=True)), agora=AGORA)
    r = _resposta(engine)
    assert r.nota == 9 and r.gravidade == respostas.POSITIVO and r.nota_estimada


def test_reavaliacao_unica_das_notas_8(engine):
    _enviar_tudo(engine)
    _receber(engine, "❤️")
    respostas.processar_pendentes(engine, ClaudeFalso(analise(nota=8, estimada=True)), agora=AGORA)
    with engine.begin() as con:  # simula a análise antiga (regra do 8)
        con.execute(t_respostas.update().values(nota=8, gravidade=respostas.ATENCAO))
    claude = ClaudeFalso(analise(nota=8, estimada=True))
    automacao.rodar_uma_vez(engine, automacao.Config(), classificar=claude, agora=AGORA)
    assert _resposta(engine).nota == 9 and len(claude.chamadas) == 1
    automacao.rodar_uma_vez(engine, automacao.Config(), classificar=claude, agora=AGORA)  # só uma vez
    assert len(claude.chamadas) == 1


def test_resposta_nao_fala_quantas_vezes_o_cliente_veio(engine):
    assert "fala quantas vezes o cliente veio" in respostas.problemas_na_resposta(
        "Oi, Isabelle! Já é a sua terceira visita e ficamos muito felizes!")
    assert respostas.problemas_na_resposta("Oi! Obrigada mais uma vez. Esperamos te ver de novo em breve!") == []
    _enviar_tudo(engine)
    _receber(engine, "Tudo excelente")
    respostas.analisar(engine, _resposta(engine).id, ClaudeFalso(analise(
        resposta="Oi, João! Que bom te ver pela 3ª vez. Volte sempre!")))
    r = _resposta(engine)
    assert r.status == respostas.REVISAR and "quantas vezes" in r.motivo_revisao


def test_sugestoes_antigas_citando_visitas_vao_para_revisao(engine):
    _enviar_tudo(engine)
    _receber(engine, "Tudo excelente")
    from sqlalchemy import update
    with engine.begin() as con:
        con.execute(update(t_respostas).values(status=respostas.PODE_ENVIAR, motivo_revisao="",
                                               resposta_sugerida="Oi! Já é a sua terceira visita!"))
    automacao.rodar_uma_vez(engine, automacao.Config(), agora=AGORA)
    assert _resposta(engine).status == respostas.REVISAR


def test_nps_do_mes_e_filtro_por_mes(app_wa, engine):
    app, _ = app_wa
    _enviar_tudo(engine)
    _receber(engine, "Nota 10", wa_id="a")
    _receber(engine, "Nota 3", telefone="5511900000025", wa_id="b")
    respostas.processar_pendentes(engine, lambda c, t, h: analise(nota=3) if "3" in t[0] else analise(estimada=True),
                                  agora=AGORA)
    n = respostas.nps(engine, date(2026, 10, 1), date(2026, 10, 31))
    assert (n["valor"], n["promotores"], n["detratores"], n["neutros"], n["estimadas"]) == (0, 1, 1, 0, 1)
    assert n["faixa"] == "Aperfeiçoamento"
    assert respostas.nps(engine, date(2026, 9, 1), date(2026, 9, 30)) is None
    assert respostas.meses_com_respostas(engine) == [(2026, 10)]
    c, csrf = _logado(app)
    dia = c.get("/respostas?data=2026-10-02").get_data(as_text=True)
    assert "NPS de outubro/2026" in dia and 'value="2026-10">outubro/2026' in dia
    mes = c.get("/respostas?mes=2026-10").get_data(as_text=True)
    assert "Mostrando o mês inteiro" in mes and "Nota 10" in mes and "Nota 3" in mes
    assert 'value="2026-10" selected' in mes and 'name="mes" value="2026-10"' in mes
    csv = c.get("/respostas/planilha.csv?mes=2026-10")
    assert "pos-venda-2026-10.csv" in csv.headers["Content-Disposition"] and len(csv.get_data(as_text=True).splitlines()) == 3
    rid = _resposta(engine).id
    r = c.post(f"/respostas/{rid}/ignorar", data={"csrf": csrf, "mes": "2026-10"})
    assert "mes=2026-10" in r.headers["Location"]
    vazio = c.get("/respostas?mes=2026-09").get_data(as_text=True)
    assert "Nenhuma nota neste mês ainda" in vazio and "deste mês" in vazio
