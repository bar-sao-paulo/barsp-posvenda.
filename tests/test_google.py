"""Aba Google: leitura das avaliações, travas, publicação automática dos elogios e tela."""
from datetime import datetime, timedelta, timezone

import pytest

from posvenda import automacao, avaliacoes as A
from posvenda.db import criar_engine
from posvenda.google_perfil import GoogleErro, PerfilGoogle, ler_avaliacao
from posvenda.web import criar_app

AGORA = datetime(2026, 10, 27, 16, 40, tzinfo=timezone.utc)  # 13h40 em São Paulo
ANTES = AGORA - timedelta(days=3)
DEPOIS = AGORA + timedelta(minutes=5)


def av(review_id, estrelas=5, comentario="Ótimo ambiente e chopp gelado", criada=DEPOIS, resposta="", autor="Ana Souza"):
    return {"review_id": review_id, "nome": f"accounts/1/locations/2/reviews/{review_id}", "autor": autor,
            "estrelas": estrelas, "comentario": comentario, "criada_em": criada, "atualizada_em": criada,
            "resposta_google": resposta}


class GoogleFalso:
    def __init__(self, lidas=(), falhar=None):
        self.lidas = list(lidas)
        self.publicadas = []
        self.falhar = falhar
        self.leituras = 0

    def avaliacoes(self, paginas=4):
        self.leituras += 1
        return iter(self.lidas)

    def responder(self, nome, texto):
        if self.falhar:
            raise GoogleErro(self.falhar)
        self.publicadas.append((nome, texto))


def respondedor(texto="Valeu demais, Ana! Que bom que curtiu o chopp. Volte sempre!\n— Equipe Bar São Paulo",
                precisa_dono=False):
    chamadas = []

    def responder(a):
        chamadas.append(a["review_id"])
        return A.Sugestao(resposta=texto, tipo="elogio", precisa_dono=precisa_dono,
                          motivo_dono="parece falsa" if precisa_dono else "")
    responder.chamadas = chamadas
    return responder


@pytest.fixture
def engine():
    return criar_engine("sqlite://")


def _todas(engine):
    return {r["review_id"]: r for r in A.listar(engine, "todas")}


# --------------------------------------------------------------------------- leitura
def test_ler_avaliacao_da_api():
    r = ler_avaliacao({
        "name": "accounts/1/locations/2/reviews/abc", "reviewId": "abc", "starRating": "FOUR",
        "comment": " Muito bom ", "createTime": "2026-10-25T20:00:00Z", "updateTime": "2026-10-25T20:05:00.123Z",
        "reviewer": {"displayName": "Ana Souza"}, "reviewReply": {"comment": "Obrigado!"}})
    assert r["estrelas"] == 4 and r["comentario"] == "Muito bom" and r["autor"] == "Ana Souza"
    assert r["resposta_google"] == "Obrigado!" and r["criada_em"].tzinfo is not None
    anonimo = ler_avaliacao({"name": "accounts/1/locations/2/reviews/x", "starRating": "FIVE",
                             "reviewer": {"displayName": "Um usuário", "isAnonymous": True}})
    assert anonimo["autor"] == "" and anonimo["review_id"] == "x" and anonimo["comentario"] == ""


def test_so_as_novas_entram_na_fila(engine):
    res = A.sincronizar(engine, [av("velha", criada=ANTES), av("nova"), av("respondida", resposta="Obrigado!")],
                        agora=AGORA)
    t = _todas(engine)
    assert t["velha"]["situacao"] == A.ANTIGA
    assert t["nova"]["situacao"] == A.AGUARDANDO
    assert t["respondida"]["situacao"] == A.JA_RESPONDIDA
    assert (res.novas, res.antigas) == (1, 1)
    # o corte fica gravado: numa leitura mais tarde, a "velha" continua antiga e uma nova entra na fila
    A.sincronizar(engine, [av("velha", criada=ANTES), av("outra", criada=AGORA + timedelta(hours=2))],
                  agora=AGORA + timedelta(hours=3))
    t = _todas(engine)
    assert t["velha"]["situacao"] == A.ANTIGA and t["outra"]["situacao"] == A.AGUARDANDO


def test_respondida_a_mao_no_google_sai_da_fila(engine):
    A.sincronizar(engine, [av("x", estrelas=2, comentario="demorou")], agora=AGORA)
    A.sincronizar(engine, [av("x", estrelas=2, comentario="demorou", resposta="Desculpa, Ana")], agora=AGORA)
    t = _todas(engine)["x"]
    assert t["situacao"] == A.JA_RESPONDIDA and t["resposta_google"] == "Desculpa, Ana"


def test_cliente_editou_a_avaliacao_refaz_a_sugestao(engine):
    A.sincronizar(engine, [av("x", estrelas=2, comentario="demorou")], agora=AGORA)
    A.responder_pendentes(engine, respondedor("Desculpa pela demora.\n— Equipe Bar São Paulo"),
                          responder_google=None, automatica=True, agora=AGORA)
    assert _todas(engine)["x"]["situacao"] == A.REVISAR
    A.sincronizar(engine, [av("x", estrelas=4, comentario="demorou, mas a comida estava ótima")], agora=AGORA)
    t = _todas(engine)["x"]
    assert t["situacao"] == A.AGUARDANDO and t["resposta_sugerida"] == "" and t["estrelas"] == 4


# --------------------------------------------------------------------------- travas e publicação
def test_elogio_sai_sozinho(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x", estrelas=5), av("y", estrelas=4, comentario="")], agora=AGORA)
    res = A.responder_pendentes(engine, respondedor(), responder_google=g.responder, automatica=True, agora=AGORA)
    assert res.publicadas == 2 and len(g.publicadas) == 2
    t = _todas(engine)["x"]
    assert t["situacao"] == A.PUBLICADA and t["publicada_por"] == "automático" and t["resposta_google"]


def test_automatica_desligada_deixa_como_sugestao(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x")], agora=AGORA)
    A.responder_pendentes(engine, respondedor(), responder_google=g.responder, automatica=False, agora=AGORA)
    assert g.publicadas == [] and _todas(engine)["x"]["situacao"] == A.REVISAR


@pytest.mark.parametrize("estrelas", [1, 2, 3, 0])
def test_reclamacao_e_sem_estrelas_esperam_a_equipe_e_avisam(engine, estrelas):
    g, avisos = GoogleFalso(), []
    A.sincronizar(engine, [av("x", estrelas=estrelas, comentario="Demorou muito")], agora=AGORA)
    A.responder_pendentes(engine, respondedor("Desculpa pela demora, Ana.\n— Equipe Bar São Paulo"),
                          responder_google=g.responder, automatica=True, alertar=avisos.append, agora=AGORA)
    t = _todas(engine)["x"]
    assert g.publicadas == [] and t["situacao"] == A.REVISAR and t["resposta_sugerida"]
    assert len(avisos) == 1 and t["alerta_em"] is not None


@pytest.mark.parametrize("texto", [
    "Valeu, Ana! Na próxima tem um cupom de 10% para você.\n— Equipe Bar São Paulo",
    "Obrigado! A sobremesa fica por conta da casa.",
    "Obrigado! Ganhe um desconto na próxima visita.",
    "Obrigado pela visita 🍻🍺",
    "",
])
def test_elogio_com_texto_proibido_nao_sai(engine, texto):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x")], agora=AGORA)
    A.responder_pendentes(engine, respondedor(texto), responder_google=g.responder, automatica=True, agora=AGORA)
    assert g.publicadas == [] and _todas(engine)["x"]["situacao"] == A.REVISAR


def test_assunto_grave_vai_para_o_dono_mesmo_com_5_estrelas(engine):
    g, avisos = GoogleFalso(), []
    A.sincronizar(engine, [av("x", estrelas=5, comentario="Comida boa, mas achei uma barata no banheiro")],
                  agora=AGORA)
    A.responder_pendentes(engine, respondedor(), responder_google=g.responder, automatica=True,
                          alertar=avisos.append, agora=AGORA)
    t = _todas(engine)["x"]
    assert g.publicadas == [] and "grave" in t["motivo_revisao"] and len(avisos) == 1


def test_ia_pede_o_dono(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x")], agora=AGORA)
    A.responder_pendentes(engine, respondedor(precisa_dono=True), responder_google=g.responder, automatica=True,
                          agora=AGORA)
    t = _todas(engine)["x"]
    assert g.publicadas == [] and "parece falsa" in t["motivo_revisao"]


def test_falha_do_google_volta_para_revisao(engine):
    g = GoogleFalso(falhar="Cota zerada no Google.")
    A.sincronizar(engine, [av("x")], agora=AGORA)
    res = A.responder_pendentes(engine, respondedor(), responder_google=g.responder, automatica=True, agora=AGORA)
    t = _todas(engine)["x"]
    assert res.erros == 1 and t["situacao"] == A.REVISAR and "Cota zerada" in t["motivo_revisao"]


def test_falha_da_ia_volta_para_revisao(engine):
    def quebrado(_):
        raise A.IAErro("A IA não respondeu (HTTP 529).")
    A.sincronizar(engine, [av("x")], agora=AGORA)
    A.responder_pendentes(engine, quebrado, responder_google=GoogleFalso().responder, automatica=True, agora=AGORA)
    assert "falha da IA" in _todas(engine)["x"]["motivo_revisao"]


def test_antiga_pedida_na_aba_nunca_sai_sozinha(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("velha", criada=ANTES), av("nova")], agora=AGORA)
    velha = _todas(engine)["velha"]["id"]
    A.pedir_sugestao(engine, velha)
    A.responder_pendentes(engine, respondedor(), responder_google=g.responder, automatica=True, agora=AGORA,
                          somente=[velha])
    t = _todas(engine)
    assert t["velha"]["situacao"] == A.REVISAR and "antiga" in t["velha"]["motivo_revisao"]
    assert t["nova"]["situacao"] == A.AGUARDANDO  # não foi mexida pelo pedido de uma só
    assert g.publicadas == []


def test_publicar_a_mao_barra_cupom(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x", estrelas=2)], agora=AGORA)
    i = _todas(engine)["x"]["id"]
    with pytest.raises(A.AvaliacaoErro, match="cupom"):
        A.publicar_a_mao(engine, i, "Desculpa! Fica um cupom de 20% para você.", g.responder, "emerson")
    assert g.publicadas == []
    A.publicar_a_mao(engine, i, "Desculpa pela demora, Ana. Fala com a gente no privado 🙏", g.responder, "emerson")
    t = _todas(engine)["x"]
    assert t["situacao"] == A.PUBLICADA and t["publicada_por"] == "emerson" and len(g.publicadas) == 1


def test_resumo_do_mes(engine):
    A.sincronizar(engine, [av("a", estrelas=5), av("b", estrelas=4), av("c", estrelas=0)], agora=AGORA)
    r = A.resumo_do_mes(engine, 2026, 10)
    assert r["avaliacoes"] == 3 and r["media"] == 4.5


# --------------------------------------------------------------------------- agendador
def test_rotina_le_a_cada_30_minutos(engine):
    g = GoogleFalso([av("x")])
    automacao.rotina_google(engine, g, respondedor(), automatica=True, agora=AGORA)
    automacao.rotina_google(engine, g, respondedor(), automatica=True, agora=AGORA + timedelta(minutes=10))
    assert g.leituras == 1 and len(g.publicadas) == 1
    automacao.rotina_google(engine, g, respondedor(), automatica=True, agora=AGORA + timedelta(minutes=25))
    assert g.leituras == 2  # 13h40 e 14h05 caem em blocos diferentes
    assert len(g.publicadas) == 1  # a mesma avaliação não é respondida duas vezes


def test_rotina_registra_erro_do_google(engine):
    class Quebrado(GoogleFalso):
        def avaliacoes(self, paginas=4):
            raise GoogleErro("Cota zerada no Google.")
    automacao.rotina_google(engine, Quebrado(), respondedor(), automatica=True, agora=AGORA)
    assert automacao.ultima_rotina(engine, "google:leitura:")["resultado"].startswith("erro: Cota zerada")


# --------------------------------------------------------------------------- cliente da API
class Resp:
    def __init__(self, status, dados):
        self.status_code, self._dados = status, dados
        self.content = b"x"
        self.text = str(dados)

    def json(self):
        return self._dados


class SessaoFalsa:
    def __init__(self, respostas):
        self.respostas = respostas
        self.pedidos = []

    def post(self, url, **kw):
        self.pedidos.append(("POST", url, kw))
        return Resp(200, {"access_token": "token-ficticio", "expires_in": 3600})

    def request(self, metodo, url, **kw):
        self.pedidos.append((metodo, url, kw))
        for chave, resp in self.respostas:
            if chave in url:
                return resp
        raise AssertionError(url)


def test_cliente_acha_o_unico_local_e_responde():
    s = SessaoFalsa([
        ("/v1/accounts/1/locations", Resp(200, {"locations": [{"name": "locations/2", "title": "Bar São Paulo"}]})),
        ("/v1/accounts", Resp(200, {"accounts": [{"name": "accounts/1"}]})),
        ("/reviews/abc/reply", Resp(200, {"comment": "ok"})),
        ("/reviews", Resp(200, {"reviews": [{"name": "accounts/1/locations/2/reviews/abc", "reviewId": "abc",
                                             "starRating": "FIVE", "comment": "Top"}]})),
    ])
    p = PerfilGoogle("id", "segredo", "refresh", sessao=s)
    lidas = list(p.avaliacoes())
    assert lidas[0]["estrelas"] == 5 and p.local() == "accounts/1/locations/2"
    p.responder("accounts/1/locations/2/reviews/abc", "Valeu!")
    metodo, url, kw = s.pedidos[-1]
    assert metodo == "PUT" and url.endswith("/v4/accounts/1/locations/2/reviews/abc/reply")
    assert kw["json"] == {"comment": "Valeu!"} and kw["headers"]["Authorization"] == "Bearer token-ficticio"
    assert sum(1 for m, *_ in s.pedidos if m == "POST") == 1  # o token é reaproveitado


def test_cliente_explica_cota_zerada():
    s = SessaoFalsa([("/reviews", Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota"}}))])
    p = PerfilGoogle("id", "segredo", "refresh", local="accounts/1/locations/2", sessao=s)
    with pytest.raises(GoogleErro, match="pedido de acesso"):
        list(p.avaliacoes())


def test_cliente_com_varios_locais_pede_google_local():
    s = SessaoFalsa([
        ("/v1/accounts/1/locations", Resp(200, {"locations": [{"name": "locations/2", "title": "A"},
                                                              {"name": "locations/3", "title": "B"}]})),
        ("/v1/accounts", Resp(200, {"accounts": [{"name": "accounts/1"}]})),
    ])
    with pytest.raises(GoogleErro, match="GOOGLE_LOCAL"):
        PerfilGoogle("id", "segredo", "refresh", sessao=s).local()


# --------------------------------------------------------------------------- tela
@pytest.fixture
def painel(engine):
    g = GoogleFalso()
    app = criar_app(engine=engine, buscar_sessoes=lambda *a: [], usuario="thais", senha="senha-ficticia",
                    whatsapp=None, classificar=None, google=g, responder_google=respondedor(),
                    buscar_clientes=None)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    with c.session_transaction() as s:  # o login troca o csrf
        csrf = s["csrf"]
    return c, g, csrf


def test_tela_google(engine, painel):
    c, g, csrf = painel
    A.sincronizar(engine, [av("x", estrelas=2, comentario="Demorou <b>muito</b>"), av("y", criada=ANTES)],
                  agora=AGORA)
    A.responder_pendentes(engine, respondedor("Desculpa pela demora.\n— Equipe Bar São Paulo"),
                          responder_google=None, automatica=True, agora=AGORA)
    html = c.get("/google").get_data(as_text=True)
    assert "Avaliações do Google" in html and 'href="/google"' in html
    assert "Demorou &lt;b&gt;muito&lt;/b&gt;" in html and "Publicar no Google" in html
    assert "Para revisar (1)" in html and "Antigas sem resposta (1)" in html
    i = _todas(engine)["x"]["id"]
    r = c.post(f"/google/{i}/publicar", data={"csrf": csrf, "texto": "Desculpa, Ana. Fala com a gente no privado."})
    assert r.status_code == 302 and g.publicadas[-1][1] == "Desculpa, Ana. Fala com a gente no privado."
    assert _todas(engine)["x"]["situacao"] == A.PUBLICADA


def test_tela_sugere_para_antiga(engine, painel):
    c, g, csrf = painel
    A.sincronizar(engine, [av("y", criada=ANTES)], agora=AGORA)
    i = _todas(engine)["y"]["id"]
    c.post(f"/google/{i}/sugerir", data={"csrf": csrf})
    t = _todas(engine)["y"]
    assert t["situacao"] == A.REVISAR and t["resposta_sugerida"] and g.publicadas == []


def test_ler_agora_publica_os_elogios(engine, painel):
    c, g, csrf = painel
    A.inicio(engine, AGORA)
    g.lidas = [av("z", criada=datetime.now(timezone.utc))]
    c.post("/google/atualizar", data={"csrf": csrf})
    assert len(g.publicadas) == 1 and _todas(engine)["z"]["situacao"] == A.PUBLICADA


def test_tela_sem_google_configurado(engine):
    app = criar_app(engine=engine, buscar_sessoes=lambda *a: [], usuario="thais", senha="senha-ficticia",
                    whatsapp=None, classificar=None, google=None, responder_google=None, buscar_clientes=None)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/entrar")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    c.post("/entrar", data={"usuario": "thais", "senha": "senha-ficticia", "csrf": csrf})
    assert "Google não configurado" in c.get("/google").get_data(as_text=True)


# --------------------------------------------------------------------------- regra de 29/10/2026
@pytest.mark.parametrize("texto", [
    "Obrigado pelo retorno, Ana. Queremos entender melhor o que aconteceu, fique à vontade para nos contar mais.",
    "Valeu! Se puder, conte mais detalhes sobre a sua visita.",
    "Desculpa pela demora. Fala com a gente no privado.",
    "Obrigado! Nos chame no direct.",
])
def test_nunca_pede_para_explicar(texto):
    assert "pede para o cliente explicar ou chamar no privado" in A.problemas_no_texto(texto)


def test_elogio_que_pede_explicacao_nao_sai_sozinho(engine):
    g = GoogleFalso()
    A.sincronizar(engine, [av("x", estrelas=4, comentario="Bom, mas demorou")], agora=AGORA)
    A.responder_pendentes(engine, respondedor("Valeu, Ana! Conte mais sobre a demora para a gente melhorar."),
                          responder_google=g.responder, automatica=True, agora=AGORA)
    assert g.publicadas == [] and "explicar" in _todas(engine)["x"]["motivo_revisao"]


@pytest.mark.parametrize("texto,ok", [
    ("Desculpa pela demora, Ana. Esperamos ter uma nova oportunidade de te receber.\n— Equipe Bar São Paulo", True),
    ("Sentimos muito. Queremos outra chance de fazer diferente!", True),
    ("Esperamos uma nova oportunidade. A equipe vai conversar sobre isso.\n— Equipe Bar São Paulo", False),
    ("Desculpa pela demora, Ana.\n— Equipe Bar São Paulo", False),
])
def test_reclamacao_termina_com_nova_oportunidade(texto, ok):
    assert A.termina_com_nova_oportunidade(texto) is ok
    motivos = A.motivos_para_revisar(av("x", estrelas=2), texto)
    assert ("reclamação não termina pedindo nova oportunidade" in motivos) is (not ok)


def test_refaz_sugestoes_sem_repetir_o_email(engine):
    avisos = []
    A.sincronizar(engine, [av("x", estrelas=2, comentario="Demorou")], agora=AGORA)
    velho = respondedor("Desculpa. Queremos entender melhor o que aconteceu.\n— Equipe Bar São Paulo")
    A.responder_pendentes(engine, velho, responder_google=None, automatica=True, alertar=avisos.append, agora=AGORA)
    automacao.rotina_google(engine, GoogleFalso(), respondedor(
        "Desculpa pela demora, Ana. Esperamos ter uma nova oportunidade de te atender.\n— Equipe Bar São Paulo"),
        automatica=True, alertar=avisos.append, agora=AGORA)
    t = _todas(engine)["x"]
    assert "nova oportunidade" in t["resposta_sugerida"] and t["situacao"] == A.REVISAR
    assert "explicar" not in t["motivo_revisao"] and len(avisos) == 1
