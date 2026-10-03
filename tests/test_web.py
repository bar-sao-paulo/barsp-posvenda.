import re
from datetime import date, datetime, timezone

import pytest

from posvenda import servico
from posvenda.db import criar_engine
from posvenda.web import criar_app
from tests.dados import sessoes_de_02_10

HOJE = date(2026, 10, 3)


class TakeatFalso:
    def __init__(self, sessoes):
        self.sessoes = sessoes
        self.chamadas = []

    def __call__(self, inicio, fim):
        self.chamadas.append((inicio, fim))
        return self.sessoes


@pytest.fixture
def ambiente():
    engine = criar_engine("sqlite://")
    takeat = TakeatFalso(sessoes_de_02_10())
    app = criar_app(engine=engine, buscar_sessoes=takeat, usuario="thais", senha="senha-ficticia", hoje=lambda: HOJE)
    app.config["TESTING"] = True
    return app, engine, takeat


def _csrf(cliente):
    with cliente.session_transaction() as s:
        return s["csrf"]


def _logar(cliente):
    cliente.get("/entrar")
    r = cliente.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": _csrf(cliente)})
    assert r.status_code == 302


def test_precisa_de_login(ambiente):
    app, _, _ = ambiente
    r = app.test_client().get("/envio")
    assert r.status_code == 302 and "/entrar" in r.headers["Location"]


def test_senha_errada(ambiente):
    app, _, _ = ambiente
    c = app.test_client()
    c.get("/entrar")
    r = c.post("/entrar", data={"usuario": "thais", "senha": "errada", "csrf": _csrf(c)})
    assert "incorretos" in r.get_data(as_text=True)


def test_post_sem_csrf_e_recusado(ambiente):
    app, _, _ = ambiente
    c = app.test_client()
    _logar(c)
    assert c.post("/envio/buscar", data={"data": "2026-10-02"}).status_code == 400


def test_lista_de_ontem_busca_sozinha_no_takeat(ambiente):
    app, _, takeat = ambiente
    c = app.test_client()
    _logar(c)
    html = c.get("/envio").get_data(as_text=True)
    assert len(takeat.chamadas) == 1
    inicio, fim = takeat.chamadas[0]
    assert (fim - inicio).days <= 3
    assert "5 mensagens prontas" in html and "| 3 não enviados" in html
    assert "Oi, João! Aqui é a Thais" in html and "a picanha cheff ontem?" in html
    assert "Pedido do iFood" in html
    assert "90000-0023" not in html.replace("5511900000023", "")  # telefone só no link, não à mostra
    # segunda visita não busca de novo
    c.get("/envio")
    assert len(takeat.chamadas) == 1


def test_marcar_enviado_e_regra_dos_7_dias(ambiente):
    app, engine, takeat = ambiente
    c = app.test_client()
    _logar(c)
    c.get("/envio?data=2026-10-02")
    dados = servico.obter_lista(engine, date(2026, 10, 2))
    joao = next(e for e in dados.envios if e["nome"] == "João")
    r = c.post(f"/envio/{joao['id']}/enviado", data={"csrf": _csrf(c), "mensagem": "Oi, João! Texto ajustado."})
    assert r.get_json() == {"ok": True}

    # buscar de novo o mesmo dia mantém o enviado e a versão
    versoes = {e["telefone"]: e["versao"] for e in dados.envios}
    c.post("/envio/buscar", data={"csrf": _csrf(c), "data": "2026-10-02"})
    depois = servico.obter_lista(engine, date(2026, 10, 2))
    assert {e["telefone"]: e["versao"] for e in depois.envios} == versoes
    j2 = next(e for e in depois.envios if e["nome"] == "João")
    assert j2["enviado_em"] is not None and j2["mensagem"] == "Oi, João! Texto ajustado."

    # dia seguinte: João já recebeu nos últimos 7 dias
    takeat.sessoes = [{**s, "start_time": s["start_time"].replace("2026-10-02", "2026-10-03").replace("2026-10-03T02:50", "2026-10-04T02:50")}
                      for s in sessoes_de_02_10()]
    servico.gerar_lista(engine, date(2026, 10, 3), takeat, hoje=date(2026, 10, 4))
    d3 = servico.obter_lista(engine, date(2026, 10, 3))
    assert "5511900000023" not in [e["telefone"] for e in d3.envios]
    assert any(n["motivo"] == "Recebeu a pesquisa nos últimos 7 dias" for n in d3.nao_enviados)


def test_csv_lista_do_envio(ambiente):
    app, _, _ = ambiente
    c = app.test_client()
    _logar(c)
    c.get("/envio")
    r = c.get("/envio/lista.csv?data=2026-10-02")
    linhas = r.get_data(as_text=True).strip().split("\n")
    assert linhas[0] == "Cliente;Telefone;Origem;Prato/pedido;Versão"
    assert any(re.fullmatch(r"JOÃO PEDRO SILVA;5511900000023;Salão;Picanha Cheff;[ABC]", l) for l in linhas)
    assert len(linhas) == 6


def test_nao_enviar(ambiente):
    app, engine, _ = ambiente
    c = app.test_client()
    _logar(c)
    r = c.post("/nao-enviar", data={"csrf": _csrf(c), "telefone": "(11) 90000-0025", "motivo": "pediu"}, follow_redirects=True)
    assert "não recebe mais" in r.get_data(as_text=True)
    r = c.post("/nao-enviar", data={"csrf": _csrf(c), "telefone": "123"}, follow_redirects=True)
    assert "Telefone inválido" in r.get_data(as_text=True)
    servico.gerar_lista(engine, date(2026, 10, 2), lambda a, b: sessoes_de_02_10(), hoje=HOJE)
    nomes = [e["nome"] for e in servico.obter_lista(engine, date(2026, 10, 2)).envios]
    assert "Camila" not in nomes
    c.post("/nao-enviar", data={"csrf": _csrf(c), "remover": "5511900000025"})
    assert servico.listar_bloqueios(engine) == []


def test_saude_sem_login(ambiente):
    app, _, _ = ambiente
    assert app.test_client().get("/saude").get_data(as_text=True) == "ok"
