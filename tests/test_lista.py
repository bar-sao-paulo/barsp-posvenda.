import random
from collections import Counter
from datetime import date
from urllib.parse import unquote

import pytest

from posvenda import lista
from tests.dados import conta, pedido, sessoes_de_02_10

DIA = date(2026, 10, 2)
HOJE = date(2026, 10, 3)


@pytest.mark.parametrize("bruto,esperado", [
    ("(11) 98765-4321", "5511987654321"),
    ("11987654321", "5511987654321"),
    ("+55 (21) 99876-5432", "5521998765432"),
    ("(11) 3333-4444", None),        # fixo
    ("(99) 99999-9999", None),       # preenchimento
    ("987654321", None),             # sem DDD
    ("", None),
    (None, None),
])
def test_normalizar_telefone(bruto, esperado):
    assert lista.normalizar_telefone(bruto) == esperado


def test_mascarar_mostra_so_o_final():
    assert lista.mascarar("5511987654321") == "(11) •••••-4321"


@pytest.mark.parametrize("nome,esperado", [
    ("JOÃO PEDRO SILVA", "João"), ("camila", "Camila"), ("Mesa 4", None), ("Cliente", None),
    ("", None), (None, None), ("A", None), ("Balcão", None),
])
def test_primeiro_nome(nome, esperado):
    assert lista.primeiro_nome(nome) == esperado


@pytest.mark.parametrize("prato,frase", [
    ("Picanha Cheff", "a picanha cheff"),
    ("Risoto de Camarão", "o risoto de camarão"),
    ("Fritas Tradicionais", "as fritas tradicionais"),
    ("Pastéis Sticks de Carne Seca", "os pastéis sticks de carne seca"),
    ("Parmegiana de Filé Mignon", "a parmegiana de filé mignon"),
    ("Filé de Frango Empanado", "o filé de frango empanado"),
    ("Refeição", "a refeição"),
    ("Burguer BSP", "o burguer BSP"),
    (None, "seu pedido"),
])
def test_frase_do_prato(prato, frase):
    assert lista.frase_do_prato(prato) == frase


def test_prato_principal_ignora_bebida_e_cancelado():
    c = conta("X", "(11) 98765-4321", pedido("Jarra de Suco Familiar 1,750", 120.0), pedido("Chopp Brahma", 14.9),
              pedido("Bife Ancho 2 Pessoas", 150.0, cancelado=True), pedido("Parmegiana de Filé Mignon 2 a 3 Pessoas", 99.0))
    assert lista.prato_principal(c) == "Parmegiana de Filé Mignon"


def test_limpar_prato_abreviacoes():
    assert lista.limpar_prato("Isca de Frango c/ Provolone 1 Pessoa") == "Isca de Frango com Provolone"


def test_prato_principal_so_bebida_vira_seu_pedido():
    c = conta("X", "(11) 98765-4321", pedido("Chopp Brahma", 14.9), pedido("Água s/ Gás", 5.0))
    assert lista.prato_principal(c) is None


def test_mensagens_das_versoes():
    a = lista.montar_mensagem("A", "João", "Picanha Cheff", DIA, HOJE)
    assert a == ("Oi, João! Aqui é a Thais, do Bar São Paulo :) Como foi sua experiência com a picanha cheff ontem? "
                 "Sua opinião é muito importante para nós, e é rapidinho!")
    b = lista.montar_mensagem("B", None, None, DIA, HOJE)
    assert b.startswith("Oi! Aqui é a Thais") and "experiência com seu pedido ontem?" in b
    c = lista.montar_mensagem("C", "Camila", "Salada Caesar", DIA, date(2026, 10, 5))
    assert "com a salada caesar no dia 02/10." in c
    for m in (a, b, c):
        assert " de a " not in m and " de o " not in m


def test_link_whatsapp_codifica_a_mensagem():
    link = lista.link_whatsapp("5511987654321", "Oi, João! Como foi? :)")
    assert link.startswith("https://wa.me/5511987654321?text=")
    assert " " not in link and unquote(link.split("text=")[1]) == "Oi, João! Como foi? :)"


def test_clientes_do_dia_respeita_horario_de_sao_paulo():
    nomes = [c.nome_completo for c in lista.clientes_do_dia(sessoes_de_02_10(), DIA)]
    assert "Madrugada" in nomes
    assert "Ontem à Noite" not in nomes
    assert "Cancelado" not in nomes
    cands = {c.nome_completo: c for c in lista.clientes_do_dia(sessoes_de_02_10(), DIA)}
    assert cands["João Vitor"].origem == "Delivery"
    assert cands["JOÃO PEDRO SILVA"].prato == "Picanha Cheff"
    assert cands["Fulano iFood"].ifood


def test_filtrar_motivos():
    cands = lista.clientes_do_dia(sessoes_de_02_10(), DIA)
    ficam, fora = lista.filtrar(cands, enviados_7_dias={"5511900000024"}, bloqueados={"5511900000026"})
    motivos = {r.candidato.nome_completo: r.motivo for r in fora}
    assert motivos == {
        "Mesa 4": lista.MOTIVO_BLOQUEIO,
        "João Vitor": lista.MOTIVO_7_DIAS,
        "Fulano iFood": lista.MOTIVO_IFOOD,
        "Sem Celular": lista.MOTIVO_SEM_TELEFONE,
        "Camila Repetida": lista.MOTIVO_REPETIDO,
    }
    assert [c.nome_completo for c in ficam] == ["JOÃO PEDRO SILVA", "Camila", "Madrugada"]


@pytest.mark.parametrize("n", [3, 4, 5, 7, 10, 31])
def test_versoes_equilibradas(n):
    tels = [f"55119000{i:05d}" for i in range(n)]
    for semente in range(20):
        v = lista.sortear_versoes(tels, {}, random.Random(semente))
        cont = Counter(v.values())
        assert max(cont.values()) <= n / 2
        assert max(cont.values()) - min(cont.get(x, 0) for x in "ABC") <= 1


def test_versoes_mantem_as_ja_sorteadas():
    tels = ["a", "b", "c", "d"]
    v = lista.sortear_versoes(tels, {"a": "B", "z": "C"}, random.Random(1))
    assert v["a"] == "B" and set(v) == set(tels)
