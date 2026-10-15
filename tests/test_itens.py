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
