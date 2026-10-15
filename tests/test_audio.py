"""Áudio (e foto) do cliente: ouvir no painel e escrever o que ele disse (dados fictícios)."""
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from posvenda import respostas
from posvenda.db import mensagens, respostas as t_respostas
from posvenda.whatsapp import WhatsApp, WhatsAppErro, ler_webhook
from tests.test_automacao import (AGORA, JOAO, ClaudeFalso, _enviar_tudo, _logado, _resposta, analise, app_wa,  # noqa: F401
                                  engine)


def _audio(engine, wa_id="wamid.a1", midia="777001"):
    return respostas.registrar_recebida(engine, JOAO, "[áudio]", wa_id, AGORA - timedelta(hours=1),
                                        midia_id=midia, midia_tipo="audio")


def test_webhook_guarda_o_id_do_audio_e_da_foto():
    rec, _ = ler_webhook({"entry": [{"changes": [{"value": {"messages": [
        {"from": JOAO, "id": "m1", "timestamp": "1790000000", "type": "audio", "audio": {"id": "555", "voice": True}},
        {"from": JOAO, "id": "m2", "timestamp": "1790000001", "type": "image", "image": {"id": "556", "caption": "olha"}},
        {"from": JOAO, "id": "m3", "timestamp": "1790000002", "type": "text", "text": {"body": "oi"}},
    ]}}]}]})
    assert [(r.texto, r.midia_id, r.midia_tipo) for r in rec] == [
        ("[áudio]", "555", "audio"), ("[foto] olha", "556", "image"), ("oi", "", "")]


def test_audio_sem_texto_vai_para_revisao_e_equipe_escreve(engine):
    _enviar_tudo(engine)
    _audio(engine)
    ia = ClaudeFalso(analise(nota=8, estimada=True))
    r = _resposta(engine)
    respostas.analisar(engine, r.id, ia)
    r = _resposta(engine)
    assert r.status == respostas.REVISAR and "áudio" in r.motivo_revisao

    respostas.escrever_audio(engine, r.id, "  A picanha  veio fria, mas o atendimento foi ótimo ", agora=AGORA)
    assert _resposta(engine).status == respostas.AGUARDANDO
    # o prazo de 24 h continua contando da mensagem do cliente, não da anotação
    assert not respostas.dentro_da_janela(engine, r.envio_id, AGORA - timedelta(hours=1) + timedelta(hours=24, minutes=1))
    ia2 = ClaudeFalso(analise(nota=7))
    respostas.analisar(engine, r.id, ia2)
    textos = ia2.chamadas[0][1]
    assert textos[-1] == respostas.PREFIXO_AUDIO + "A picanha veio fria, mas o atendimento foi ótimo"
    assert "áudio" not in (_resposta(engine).motivo_revisao or "")


def test_escrever_audio_vazio_ou_depois_de_respondido(engine):
    _enviar_tudo(engine)
    _audio(engine)
    r = _resposta(engine)
    with pytest.raises(respostas.RespostaErro):
        respostas.escrever_audio(engine, r.id, "   ")
    with engine.begin() as con:
        con.execute(update(t_respostas).values(status=respostas.RESPONDIDO))
    respostas.escrever_audio(engine, r.id, "gostei")
    assert _resposta(engine).status == respostas.RESPONDIDO  # já respondido: só anota, não reabre


def test_baixar_midia_na_meta():
    class Resp:
        def __init__(self, dados=None, conteudo=b"", status=200):
            self.dados, self.content, self.status_code, self.headers = dados, conteudo, status, {}

        def json(self):
            return self.dados

    class Sessao:
        def __init__(self):
            self.urls = []

        def get(self, url, timeout, headers, params=None):
            self.urls.append(url)
            assert headers == {"Authorization": "Bearer token-ficticio"}
            if url.endswith("/777001"):
                return Resp({"url": "https://lookaside.fbsbx.com/arquivo-ficticio", "mime_type": "audio/ogg"})
            return Resp(conteudo=b"OggS-ficticio")

    s = Sessao()
    wa = WhatsApp("token-ficticio", "123", session=s)
    assert wa.baixar_midia("777001") == (b"OggS-ficticio", "audio/ogg")
    assert s.urls[0] == "https://graph.facebook.com/v23.0/777001"
    with pytest.raises(WhatsAppErro):
        wa.baixar_midia("../123")


def test_painel_toca_o_audio_e_recebe_o_texto(app_wa, engine):
    app, wa = app_wa
    _enviar_tudo(engine)
    _audio(engine)
    respostas.analisar(engine, _resposta(engine).id, ClaudeFalso(analise(nota=8, estimada=True)))
    wa.baixar_midia = lambda midia_id: (b"OggS-ficticio", "audio/ogg")
    assert app.test_client().get("/midia/1").status_code == 302  # sem login não toca
    c, csrf = _logado(app)
    pagina = c.get("/respostas?data=2026-10-02").get_data(as_text=True)
    assert "<audio controls" in pagina and "Salvar o que o cliente disse" in pagina
    with engine.connect() as con:
        mid = con.execute(select(mensagens.c.id).where(mensagens.c.midia_id == "777001")).scalar()
    r = c.get(f"/midia/{mid}")
    assert r.status_code == 200 and r.data == b"OggS-ficticio" and r.mimetype == "audio/ogg"
    assert "no-store" in r.headers["Cache-Control"]
    assert c.get("/midia/99999").status_code == 404

    def expirou(midia_id):
        raise WhatsAppErro("expirou")
    wa.baixar_midia = expirou
    assert c.get(f"/midia/{mid}").status_code == 410
    rid = _resposta(engine).id
    c.post(f"/respostas/{rid}/audio", data={"csrf": csrf, "data": "2026-10-02", "texto": "Tudo ótimo"})
    assert _resposta(engine).status == respostas.AGUARDANDO
    pagina = c.get("/respostas?data=2026-10-02").get_data(as_text=True)
    assert "No áudio (escrito pela equipe):</span> Tudo ótimo" in pagina
    assert "Salvar o que o cliente disse" not in pagina
