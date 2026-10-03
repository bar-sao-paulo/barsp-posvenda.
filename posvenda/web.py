"""Painel do pós-venda (celular): lista do dia com os links do WhatsApp e quem não quer receber.

Rodar no Railway: gunicorn "posvenda.web:criar_app()" --bind 0.0.0.0:$PORT
Variáveis: DATABASE_URL, TAKEAT_API_KEY, TAKEAT_BASE_URL (opcional), SECRET_KEY, LOGIN_USUARIO, LOGIN_SENHA.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import date, timedelta

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from jinja2 import DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix

from . import lista, servico
from .db import criar_engine
from .takeat import TakeatClient, TakeatError

log = logging.getLogger("posvenda.web")


def _buscar_takeat():
    chave = os.environ.get("TAKEAT_API_KEY")
    if not chave:
        return None
    cliente = TakeatClient(chave, base_url=os.environ.get("TAKEAT_BASE_URL", "https://public-api.takeat.app"),
                           scopes=("table-sessions:read",))
    return cliente.listar_sessoes


def criar_app(engine=None, buscar_sessoes=None, usuario: str | None = None, senha: str | None = None,
              hoje=servico.hoje_local) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    chave = os.environ.get("SECRET_KEY")
    if not chave:
        log.warning("SECRET_KEY não definida: os logins caem a cada reinício.")
    app.secret_key = chave or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    app.jinja_loader = DictLoader(TEMPLATES)
    engine = engine or criar_engine()
    buscar = buscar_sessoes if buscar_sessoes is not None else _buscar_takeat()
    login_usuario = usuario if usuario is not None else os.environ.get("LOGIN_USUARIO", "")
    login_senha = senha if senha is not None else os.environ.get("LOGIN_SENHA", "")

    @app.before_request
    def _csrf_e_login():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST":
            enviado = request.form.get("csrf") or request.headers.get("X-CSRF", "")
            if not hmac.compare_digest(enviado, session["csrf"]):
                abort(400)
        if request.endpoint in ("entrar", "saude", "static"):
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

    @app.get("/saude")
    def saude():
        return "ok"

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
        return render_template("envio.html", dados=dados, dia=dia, hoje=hoje(), motivos=motivos,
                               mascarar=lista.mascarar, takeat_ok=buscar is not None, timedelta=timedelta,
                               tz=lista.TZ)

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
        return render_template("nao_enviar.html", bloqueios=servico.listar_bloqueios(engine), mascarar=lista.mascarar)

    return app


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
input,textarea{font:inherit;padding:8px;border:1px solid var(--bd);border-radius:8px;width:100%}
textarea{min-height:92px;resize:vertical}
.cli{display:grid;gap:8px}.cli .topo{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
.tag{border:1px solid var(--bd);border-radius:999px;padding:1px 9px;font-size:13px}
.enviado{opacity:.6}.ok{color:var(--ac);font-weight:600}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--bd);font-size:14px}
.linha{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
</style></head><body>
{% if g.usuario %}<header><nav><a href="{{ url_for('envio') }}">Envio</a><a href="{{ url_for('nao_enviar') }}">Não enviar</a></nav>
<form method="post" action="{{ url_for('sair') }}"><input type="hidden" name="csrf" value="{{ session['csrf'] }}"><button class="btn2">Sair</button></form></header>{% endif %}
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block corpo %}{% endblock %}</main></body></html>""",

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
{% if dados.envios %}<a class="btn btn2" href="{{ url_for('envio_csv', data=dia.isoformat()) }}">Baixar "Lista do envio" (CSV)</a>{% endif %}</div></div>

{% for e in dados.envios %}<div class="card cli{{ ' enviado' if e.enviado_em }}" id="e{{ e.id }}">
<div class="topo"><strong>{{ e.nome_completo or e.nome or 'Sem nome' }}</strong><span class="tag">{{ e.origem }}</span>
<span class="tag">Versão {{ e.versao }}</span><span class="quieto">{{ e.prato or 'seu pedido' }} · {{ mascarar(e.telefone) }}</span></div>
<textarea id="m{{ e.id }}" aria-label="Mensagem">{{ e.mensagem }}</textarea>
<div class="linha">{% if e.enviado_em %}<span class="ok">✓ enviado</span>
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

    "nao_enviar.html": """{% extends 'base.html' %}{% block corpo %}<div class="card"><h3 style="margin-top:0">Não enviar a pesquisa</h3>
<p class="quieto">Quem está aqui nunca entra na lista do envio.</p>
<form method="post" class="cli"><input type="hidden" name="csrf" value="{{ session['csrf'] }}">
<input name="telefone" placeholder="Telefone com DDD, ex.: (11) 98765-4321" required>
<input name="motivo" placeholder="Motivo (opcional)"><div><button>Incluir</button></div></form></div>
{% if bloqueios %}<div class="card"><table><tr><th>Telefone</th><th>Motivo</th><th>Desde</th><th></th></tr>
{% for b in bloqueios %}<tr><td>{{ mascarar(b.telefone) }}</td><td>{{ b.motivo or '—' }}</td><td>{{ b.criado_em.strftime('%d/%m/%Y') }}</td>
<td><form method="post"><input type="hidden" name="csrf" value="{{ session['csrf'] }}"><input type="hidden" name="remover" value="{{ b.telefone }}">
<button class="btn2" onclick="return confirm('Voltar a enviar a pesquisa para este telefone?')">Remover</button></form></td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}""",
}
