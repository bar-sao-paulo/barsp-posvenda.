"""Tudo o que o cliente consumiu no dia (comandas fictícias)."""
import os
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import update

from posvenda import automacao, lista, servico
from posvenda.db import criar_engine, envios, metadata
from posvenda.ia import montar_contexto
from posvenda.web import criar_app
from tests.dados import conta, pedido, sessoes_de_02_10

HOJE = date(2026, 10, 3)
DIA = date(2026, 10, 2)
JOAO, CAMILA = "5511900000023", "5511900000025"


@pytest.fixture
def engine():
    url = os.environ.get("POSVENDA_TEST_PG")
    if url:
        metadata.drop_all(criar_engine(url))
    return criar_engine(url or "sqlite://")


def _itens(engine, telefone):
    return next(e["itens"] for e in servico.obter_lista(engine, DIA).envios if e["telefone"] == telefone)


def test_itens_da_conta_soma_quantidade_e_tira_cancelado():
    c = conta("Fulano", "(11) 90000-0040", pedido("Chopp Brahma", 14.9, qtd=2), pedido("Chopp Brahma", 14.9),
              pedido("Picanha", 90.0, cancelado=True), pedido("Fritas  Tradicionais", 29.9))
    assert lista.texto_dos_itens(lista.itens_da_conta(c)) == "3x Chopp Brahma · 1x Fritas Tradicionais"


def test_lista_guarda_tudo_o_que_consumiu(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    assert _itens(engine, JOAO) == "1x Chopp Brahma · 1x Picanha Cheff 1 Pessoa · 1x Fritas Tradicionais"
    # Camila teve duas contas no dia (salada e brownie): o cartão mostra as duas
    assert _itens(engine, CAMILA) == "1x Salada Caesar · 1x Brownie"


def test_envio_ja_feito_recebe_os_itens_e_listas_antigas_sao_preenchidas(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    e = next(x for x in servico.obter_lista(engine, DIA).envios if x["telefone"] == JOAO)
    servico.marcar_enviado(engine, e["id"])
    with engine.begin() as con:  # como as listas de antes desta mudança
        con.execute(update(envios).values(itens=None))
    chamadas = []

    def buscar(i, f):
        chamadas.append((i, f))
        return sessoes_de_02_10()
    assert servico.preencher_itens(engine, buscar, HOJE) == DIA
    assert _itens(engine, JOAO).startswith("1x Chopp Brahma")
    assert servico.preencher_itens(engine, buscar, HOJE) is None
    assert len(chamadas) == 1
    # buscar a lista de novo atualiza também quem já recebeu
    with engine.begin() as con:
        con.execute(update(envios).values(itens=None))
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    assert _itens(engine, JOAO).startswith("1x Chopp Brahma")


def test_comanda_sumiu_fica_vazio_e_nao_busca_de_novo(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    with engine.begin() as con:
        con.execute(update(envios).values(itens=None))
    assert servico.preencher_itens(engine, lambda i, f: [], HOJE) == DIA
    assert _itens(engine, JOAO) == ""
    assert servico.preencher_itens(engine, lambda i, f: [], HOJE) is None


def test_agendador_preenche_itens(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    with engine.begin() as con:
        con.execute(update(envios).values(itens=None))
    automacao.rodar_uma_vez(engine, automacao.Config(), buscar=lambda i, f: sessoes_de_02_10(),
                            agora=datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc))
    assert _itens(engine, CAMILA) == "1x Salada Caesar · 1x Brownie"


def test_pagina_e_ia_mostram_os_itens(engine):
    app = criar_app(engine=engine, buscar_sessoes=lambda i, f: sessoes_de_02_10(), usuario="thais",
                    senha="senha-ficticia", hoje=lambda: HOJE, whatsapp=None, classificar=None)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    html = c.get("/envio").get_data(as_text=True)
    assert "<b>Consumiu:</b> 1x Salada Caesar · 1x Brownie" in html
    e = next(x for x in servico.obter_lista(engine, DIA).envios if x["telefone"] == JOAO)
    assert "Tudo o que consumiu: 1x Chopp Brahma" in montar_contexto(e, ["ótimo"], "sem compras registradas")


def test_mesa_no_salao_e_numero_do_pedido_no_delivery():
    from posvenda import lista
    from tests.dados import conta, pedido, sessao
    s = [sessao("2026-10-02T15:00:00Z", conta("Ana", "(11) 90000-0050", pedido("Parmegiana", 99.0)), mesa=12, numero=2163),
         sessao("2026-10-02T16:00:00Z", conta("Bia", "(11) 90000-0051", pedido("Risoto", 79.0)), delivery=True,
                numero=2170, mesa=0),
         sessao("2026-10-02T17:00:00Z", conta("Caio", "(11) 90000-0052", pedido("Paulistinha", 59.0)), tipo="command",
                mesa=7),
         sessao("2026-10-02T18:00:00Z", conta("Duda", "(11) 90000-0053", pedido("Paulistinha", 59.0)))]
    locais = {c.nome: c.local for c in lista.clientes_do_dia(s, DIA)}
    assert locais == {"Ana": "Mesa 12", "Bia": "Pedido #2170", "Caio": "Comanda 7", "Duda": ""}
    # 15h e 16h UTC = 12h e 13h em São Paulo (almoço); 17h e 18h UTC = 14h e 15h (almoço também)
    assert lista.turno("2026-10-02T19:59:00Z") == "Almoço" and lista.turno("2026-10-02T20:00:00Z") == "Janta"
    assert {c.nome: c.turno for c in lista.clientes_do_dia(s, DIA)}["Ana"] == "Almoço"


def test_lista_grava_a_mesa_e_completa_as_antigas():
    from tests.dados import sessao
    e = criar_engine("sqlite://")
    s = [sessao("2026-10-02T15:00:00Z", conta("Ana", "(11) 90000-0050", pedido("Parmegiana", 99.0)), mesa=12)]
    servico.gerar_lista(e, DIA, lambda i, f: s, hoje=date(2026, 10, 3))
    assert [x["local"] for x in servico.obter_lista(e, DIA).envios] == ["Mesa 12"]
    with e.begin() as con:
        con.execute(update(envios).values(local=None, turno=None))  # lista de antes desta mudança
    assert servico.preencher_itens(e, lambda i, f: s, date(2026, 10, 3)) == DIA
    assert [x["local"] for x in servico.obter_lista(e, DIA).envios] == ["Mesa 12"]
    assert servico.preencher_itens(e, lambda i, f: s, date(2026, 10, 3)) is None
    app = criar_app(engine=e, buscar_sessoes=lambda i, f: s, usuario="thais", senha="senha-ficticia",
                    hoje=lambda: date(2026, 10, 3), whatsapp=None, classificar=None)
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as ss:
        csrf = ss["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    assert "Salão · Mesa 12 · Almoço" in c.get("/envio?data=2026-10-02").get_data(as_text=True)
