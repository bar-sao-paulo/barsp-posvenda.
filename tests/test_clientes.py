"""Tela Clientes: Clube de Fidelidade do Takeat (dados e chaves fictícios)."""
from datetime import date

from posvenda import clientes, servico
from posvenda.db import criar_engine
from posvenda.takeat import TakeatClient, TakeatForbidden
from posvenda.web import criar_app

CLUBE = [
    {"name": "Ana  Fictícia ", "phone": "(11)90000-0001", "visits": 3, "total_spent": "150.00", "favorite_product": "Picanha"},
    {"name": "Bruno Fictício", "phone": "(11) 90000-0002", "visits": 12, "total_spent": "900.50", "favorite_product": None},
    {"name": "Carla Fictícia", "phone": "(11) 90000-0003", "visits": 12, "total_spent": "1200.00", "favorite_product": "Brownie"},
    {"name": "Davi Fictício", "phone": "", "visits": 1, "total_spent": "40", "favorite_product": None},
]


class Resp:
    def __init__(self, status, corpo):
        self.status_code, self._corpo, self.headers = status, corpo, {}

    def json(self):
        return self._corpo


class SessaoFalsa:
    def __init__(self, paginas):
        self.paginas, self.pedidos = paginas, []

    def request(self, method, url, headers=None, timeout=None, params=None, data=None):
        if url.endswith("/oauth/token"):
            return Resp(200, {"access_token": "token-ficticio", "expires_in": 900, "scope": data["scope"]})
        self.pedidos.append(dict(params))
        return self.paginas[params["page"] - 1]


def test_cliente_takeat_segue_a_paginacao_do_clube():
    paginas = [Resp(200, {"data": CLUBE[:2], "pagination": {"has_more": True, "next": {"page": 2, "per_page": 200}}}),
               Resp(200, {"data": CLUBE[2:], "pagination": {"has_more": False, "next": None}})]
    s = SessaoFalsa(paginas)
    c = TakeatClient("chave-ficticia", scopes=("clube:read",), session=s, sleep=lambda x: None)
    assert len(c.listar_clientes_clube()) == 4
    assert s.pedidos == [{"page": 1, "per_page": 200}, {"page": 2, "per_page": 200}]


def test_selecionar_do_que_mais_pediu():
    todos = clientes.normalizar(CLUBE)
    assert todos[0].nome == "Ana Fictícia" and todos[0].telefone == "5511900000001"
    assert [c.nome for c in clientes.selecionar(todos, 2)] == ["Carla Fictícia", "Bruno Fictício"]
    assert [c.pedidos for c in clientes.selecionar(todos, minimo=3)] == [12, 12, 3]
    assert len(clientes.selecionar(todos)) == 4


def test_cache_le_o_takeat_uma_vez_por_hora():
    chamadas, agora = [], [0.0]
    cache = clientes.Cache(lambda: chamadas.append(1) or CLUBE, relogio=lambda: agora[0])
    cache.obter(); cache.obter()
    agora[0] = clientes.VALIDADE + 1
    cache.obter()
    cache.obter(atualizar=True)
    assert len(chamadas) == 3


def _app(buscar):
    engine = criar_engine("sqlite://")
    app = criar_app(engine=engine, buscar_sessoes=lambda i, f: [], usuario="thais", senha="senha-ficticia",
                    hoje=lambda: date(2026, 10, 19), whatsapp=None, classificar=None, buscar_clientes=buscar)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    return c, engine


def test_pagina_clientes_e_planilha():
    c, engine = _app(lambda: CLUBE)
    servico.bloquear(engine, "5511900000002", "pediu")
    html = c.get("/clientes?topo=20&minimo=3").get_data(as_text=True)
    assert "3 de 4 clientes do Clube" in html and "Carla Fictícia" in html and "Davi" not in html
    assert html.index("Carla") < html.index("Bruno") < html.index("Ana")
    from posvenda import historico
    from tests.dados import conta, pedido, sessao
    historico.gravar(engine, [sessao("2026-10-10T15:00:00Z", conta("Carla", "(11) 90000-0003",
                                                                   pedido("Mignon Acebolado", 99.0), pedido("Chopp", 15.0)))],
                     date(2026, 10, 10), date(2026, 10, 10))
    html = c.get("/clientes?topo=20&minimo=3").get_data(as_text=True)
    assert "Último pedido" in html and "10/10/2026" in html
    assert "Prato favorito" in html and "Mignon Acebolado" in html and "Brownie" not in html
    assert "R$ 1200,00" in html and "não enviar" in html and "(11) 90000-0003" in html
    assert 'data-tel="5511900000003" data-nome="Carla"' in html and 'data-tel="5511900000002"' not in html  # bloqueado
    busca = c.get("/clientes?topo=todos&busca=fictícia+carla").get_data(as_text=True)
    assert "0 de 4" in busca
    busca = c.get("/clientes?topo=todos&busca=CARLA").get_data(as_text=True)
    assert "1 de 4" in busca and "Carla Fictícia" in busca
    assert "1 de 4" in c.get("/clientes?topo=todos&busca=0000-0001").get_data(as_text=True)
    csv = c.get("/clientes/planilha.csv?topo=todos&minimo=0").get_data(as_text=True).splitlines()
    assert csv[1] == "1;Carla Fictícia;5511900000003;12;1200,00;Brownie" and len(csv) == 5


def test_chave_sem_permissao_do_clube_explica_o_que_fazer():
    def sem_escopo():
        raise TakeatForbidden("Sem permissão", 403)
    c, _ = _app(sem_escopo)
    assert "clube:read" in c.get("/clientes").get_data(as_text=True)


def test_mensagens_da_aba_clientes_ficam_separadas_da_pesquisa():
    from posvenda.db import mensagens
    from sqlalchemy import select, func
    c, engine = _app(lambda: CLUBE)
    with c.session_transaction() as s:
        csrf = s["csrf"]
    r = c.post("/clientes/mensagem", data={"csrf": csrf, "telefone": "5511900000003", "nome": "Carla Fictícia",
                                           "texto": "Oi, Carla! Temos novidade."})
    assert r.get_json() == {"ok": True}
    assert c.post("/clientes/mensagem", data={"csrf": csrf, "telefone": "12", "texto": "x"}).status_code == 400
    assert c.post("/clientes/mensagem", data={"telefone": "5511900000003", "texto": "x"}).status_code == 400  # sem CSRF
    html = c.get("/clientes?topo=todos").get_data(as_text=True)
    assert "✉ 1" in html and "Oi, Carla! Temos novidade." in html and "Mensagens enviadas pela aba Clientes" in html
    assert 'data-completo="Carla Fictícia"' in html
    with engine.connect() as con:  # nada vai para as mensagens da pesquisa
        assert con.execute(select(func.count()).select_from(mensagens)).scalar() == 0
