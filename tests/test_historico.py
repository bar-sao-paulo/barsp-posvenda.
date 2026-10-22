"""Histórico de compras do cliente (comandas do Takeat dos últimos 12 meses), com dados fictícios."""
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from posvenda import automacao, historico, lista, respostas, servico
from posvenda.db import criar_engine, metadata
from tests.dados import conta, pedido, sessao, sessoes_de_02_10

HOJE = date(2026, 10, 3)
DIA = date(2026, 10, 2)
CAMILA = "5511900000025"


@pytest.fixture
def engine():
    url = os.environ.get("POSVENDA_TEST_PG")
    if url:
        metadata.drop_all(criar_engine(url))
    return criar_engine(url or "sqlite://")


class TakeatFalso:
    """Devolve, para cada janela pedida, uma comanda da Camila no meio da janela."""

    def __init__(self):
        self.janelas = []

    def __call__(self, inicio, fim):
        self.janelas.append((inicio, fim))
        meio = inicio + (fim - inicio) / 2
        return [sessao(meio.strftime("%Y-%m-%dT%H:%M:%SZ"), conta("Camila", "(11) 90000-0025", pedido("Brownie", 19.0)))]


def test_visitas_das_sessoes():
    v = lista.visitas_das_sessoes(sessoes_de_02_10(), DIA, DIA)
    chaves = {c for c, _ in v}
    assert all(d == DIA for _, d in v)
    assert "1100000025" in chaves  # Camila: duas contas no dia contam uma vez
    assert "1100000030" not in chaves  # comanda cancelada
    assert "1100000031" not in chaves  # 01/10 à noite em São Paulo
    assert "1100000032" in chaves  # 02/10 às 23h50 em São Paulo
    assert len([c for c, _ in v if c == "1100000025"]) == 1


def test_blocos_cobrem_12_meses_sem_dia_de_hoje(engine):
    blocos = historico.blocos_pendentes(engine, HOJE)
    assert blocos == sorted(blocos, reverse=True)
    assert blocos[0] + timedelta(days=2) < HOJE
    assert blocos[-1] <= HOJE - timedelta(days=365)
    assert all(historico.inicio_do_bloco(b) == b for b in blocos)
    assert len(blocos) in (122, 123)


def test_carregar_aos_poucos_sem_reler(engine):
    takeat = TakeatFalso()
    assert historico.carregar(engine, takeat, HOJE, limite=4) == 4
    for de, ate in takeat.janelas:
        assert ate - de == timedelta(days=3)  # a Takeat aceita no máximo três dias
        assert de.tzinfo is not None
    assert not historico.completo(engine, HOJE)
    while historico.carregar(engine, takeat, HOJE, limite=50):
        pass
    assert historico.completo(engine, HOJE)
    n_janelas = len(takeat.janelas)
    assert historico.carregar(engine, takeat, HOJE) == 0
    assert len(takeat.janelas) == n_janelas
    texto, n = historico.compras(engine, [(CAMILA, HOJE - timedelta(days=1))], HOJE)[0]
    assert n == n_janelas - 1 or n == n_janelas  # um dia por bloco; o mais antigo pode passar dos 12 meses
    assert texto.startswith(f"{n} compras em 12 meses (1ª em ")


def test_texto_das_compras(engine):
    historico.gravar(engine, sessoes_de_02_10(), DIA, DIA)
    assert historico.compras(engine, [(CAMILA, DIA)])[0] == ("1ª compra em 12 meses", 1)
    antigas = [sessao("2026-03-10T15:00:00Z", conta("Camila", "(11) 90000-0025", pedido("Brownie", 19.0))),
               sessao("2025-09-01T15:00:00Z", conta("Camila", "(11) 90000-0025", pedido("Brownie", 19.0))),
               sessao("2026-10-20T15:00:00Z", conta("Camila", "(11) 90000-0025", pedido("Brownie", 19.0)))]
    historico.gravar(engine, antigas, date(2025, 1, 1), date(2026, 12, 31))
    # 01/09/2025 passa dos 12 meses e 20/10 é depois da visita: ficam de fora
    assert historico.compras(engine, [(CAMILA, DIA)])[0] == ("2 compras em 12 meses (1ª em 10/03/2026)", 2)
    assert historico.compras(engine, [("5511900000099", DIA)])[0] == ("sem compras registradas", 0)
    assert historico.compras(engine, [("5511900000099", DIA)], HOJE)[0] == ("histórico ainda carregando", 0)
    # WhatsApp sem o 9: mesma pessoa
    assert historico.compras(engine, [("551100000025", DIA)])[0][1] == 2


def test_lista_do_dia_grava_as_visitas(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    rows = servico.obter_lista(engine, DIA).envios
    assert {r["telefone"] for r in rows} >= {CAMILA}
    assert historico.compras(engine, [(CAMILA, DIA)])[0][1] == 1
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)  # buscar de novo não duplica
    assert historico.compras(engine, [(CAMILA, DIA)])[0][1] == 1


def test_resposta_mostra_compras(engine):
    servico.gerar_lista(engine, DIA, lambda i, f: sessoes_de_02_10(), hoje=HOJE)
    historico.gravar(engine, [sessao("2026-08-10T15:00:00Z", conta("Camila", "(11) 90000-0025", pedido("Brownie", 19.0)))],
                     date(2026, 8, 10), date(2026, 8, 10))
    e = next(x for x in servico.obter_lista(engine, DIA).envios if x["telefone"] == CAMILA)
    assert respostas.historico_cliente(engine, e) == ("2 compras em 12 meses (1ª em 10/08/2026)", 2)


def test_agendador_le_o_historico_e_aguenta_falha(engine):
    takeat = TakeatFalso()
    agora = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    automacao.rodar_uma_vez(engine, automacao.Config(), buscar=takeat, agora=agora)
    assert len(takeat.janelas) == 4

    def quebrado(i, f):
        raise RuntimeError("Takeat fora do ar")
    automacao.rodar_uma_vez(engine, automacao.Config(), buscar=quebrado, agora=agora)  # não derruba a rotina
    assert len(historico.blocos_pendentes(engine, HOJE)) > 0


def test_prato_favorito_e_o_prato_principal_mais_pedido(engine):
    tel = "(11) 90000-0040"
    dias = [("2026-07-01", "Parmegiana de Filé 2 Pessoas", "Chopp Brahma"), ("2026-08-01", "Risoto de Camarão", "Suco"),
            ("2026-09-01", "Parmegiana de Filé 2 Pessoas", "Arroz Branco"), ("2026-09-20", "Risoto de Camarão", None)]
    for dia, prato, outro in dias:
        itens = [pedido(prato, 120.0)] + ([pedido(outro, 15.0)] if outro else [])
        historico.gravar(engine, [sessao(f"{dia}T15:00:00Z", conta("Fulana", tel, *itens))],
                         date.fromisoformat(dia), date.fromisoformat(dia))
    # Só bebida no dia: não vira prato.
    historico.gravar(engine, [sessao("2026-09-25T15:00:00Z", conta("Fulana", tel, pedido("Chopp Brahma", 14.9)))],
                     date(2026, 9, 25), date(2026, 9, 25))
    # Empate 2 x 2: fica o mais recente.
    assert historico.pratos_favoritos(engine, ["5511900000040"], HOJE) == {"1100000040": "Risoto de Camarão"}
    historico.gravar(engine, [sessao("2026-09-28T15:00:00Z", conta("Fulana", tel, pedido("Parmegiana de Filé", 99.0),
                                                                    pedido("Pepsi Lata", 7.0)))],
                     date(2026, 9, 28), date(2026, 9, 28))
    assert historico.pratos_favoritos(engine, ["5511900000040"], HOJE) == {"1100000040": "Parmegiana de Filé"}


def test_visita_antiga_sem_prato_ganha_o_prato_ao_reler(engine):
    from sqlalchemy import insert
    from posvenda.db import visitas
    with engine.begin() as con:
        con.execute(insert(visitas).values(chave="1100000041", dia=date(2026, 9, 1)))
    historico.gravar(engine, [sessao("2026-09-01T15:00:00Z", conta("Beltrano", "(11) 90000-0041",
                                                                   pedido("Paulistinha", 59.0)))],
                     date(2026, 9, 1), date(2026, 9, 1))
    assert historico.pratos_favoritos(engine, ["11900000041"], HOJE) == {"1100000041": "Paulistinha"}
    agora = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    historico.carregar(engine, TakeatFalso(), HOJE, limite=10)
    antes = len(historico.blocos_pendentes(engine, HOJE))
    automacao.rodar_uma_vez(engine, automacao.Config(), buscar=TakeatFalso(), agora=agora)  # reler-pratos:v1
    assert len(historico.blocos_pendentes(engine, HOJE)) == antes + 10 - 4  # esqueceu os 10 e já releu 4
    automacao.rodar_uma_vez(engine, automacao.Config(), buscar=TakeatFalso(), agora=agora)  # só uma vez
    assert len(historico.blocos_pendentes(engine, HOJE)) == antes + 10 - 8


def test_valor_da_visita_e_preenchido_nas_visitas_antigas():
    from sqlalchemy import select, update

    from posvenda.db import visitas
    engine = criar_engine("sqlite://")
    dia = date(2026, 10, 10)
    s = [sessao("2026-10-10T15:00:00Z", conta("Ana Fictícia", "(11) 90000-0051", pedido("Parmegiana", 80.0)),
                conta("Ana de Novo", "(11) 90000-0051", pedido("Pudim", 20.0)))]
    historico.gravar(engine, s, dia, dia)
    with engine.begin() as con:
        assert con.execute(select(visitas.c.valor)).scalar() == 110.0  # duas contas do dia, com serviço
        con.execute(update(visitas).values(valor=None))  # visita gravada antes da coluna existir
    historico.gravar(engine, s, dia, dia)
    with engine.connect() as con:
        assert con.execute(select(visitas.c.valor)).scalar() == 110.0
    assert historico.contatos_com_pedidos(engine, date(2026, 10, 22)) == [("5511900000051", "Ana de Novo", 1, 110.0)]
