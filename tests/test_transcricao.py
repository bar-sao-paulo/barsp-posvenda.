"""Transcrição automática dos áudios (serviço e chave fictícios)."""
from datetime import timedelta

import pytest

from posvenda import automacao, respostas
from posvenda.transcricao import TranscricaoErro, Transcritor
from tests.test_automacao import (AGORA, JOAO, ClaudeFalso, WhatsAppFalso, _enviar_tudo, _resposta, analise,  # noqa: F401
                                  engine)


class Resp:
    def __init__(self, status=200, dados=None):
        self.status_code, self.dados = status, dados or {}

    def json(self):
        return self.dados


class Sessao:
    def __init__(self, resp):
        self.resp, self.chamadas = resp, []

    def post(self, url, timeout, headers, files, data):
        self.chamadas.append((url, headers, files, data))
        return self.resp


def test_transcritor_manda_o_audio_e_le_o_texto():
    s = Sessao(Resp(dados={"text": " A picanha veio fria. "}))
    t = Transcritor("chave-ficticia", session=s)
    assert t(b"OggS", "audio/ogg; codecs=opus") == "A picanha veio fria."
    url, headers, files, data = s.chamadas[0]
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert headers == {"Authorization": "Bearer chave-ficticia"}
    assert files["file"] == ("audio.ogg", b"OggS", "audio/ogg")
    assert data["language"] == "pt" and data["model"] == "whisper-1"
    assert "chave-ficticia" not in repr(t)


def test_erro_da_transcricao_nao_mostra_a_chave():
    s = Sessao(Resp(401, {"error": {"message": "Incorrect API key provided: sk-proj-abc123ficticia."}}))
    with pytest.raises(TranscricaoErro) as exc:
        Transcritor("sk-proj-abc123ficticia", session=s)(b"OggS", "audio/ogg")
    assert "abc123" not in str(exc.value) and "401" in str(exc.value)


def _audio(engine, wa_id="wamid.a1", midia="777001", horas=1):
    return respostas.registrar_recebida(engine, JOAO, "[áudio]", wa_id, AGORA - timedelta(hours=horas),
                                        midia_id=midia, midia_tipo="audio")


def test_agendador_transcreve_antes_de_analisar_e_so_uma_vez(engine):
    _enviar_tudo(engine)
    _audio(engine)
    wa = WhatsAppFalso()
    wa.baixar_midia = lambda midia_id: (b"OggS", "audio/ogg")
    pedidos = []

    def transcrever(conteudo, tipo):
        pedidos.append(tipo)
        return "Adorei o burger, tudo certo"
    ia = ClaudeFalso(analise(nota=9, estimada=True))
    automacao.rodar_uma_vez(engine, automacao.Config(), whatsapp=wa, classificar=ia, transcrever=transcrever,
                            agora=AGORA)
    assert pedidos == ["audio/ogg"]
    assert ia.chamadas[0][1][-1] == respostas.PREFIXO_TRANSCRITO + "Adorei o burger, tudo certo"
    r = _resposta(engine)
    assert r.status == respostas.PODE_ENVIAR and "áudio" not in r.motivo_revisao
    automacao.rodar_uma_vez(engine, automacao.Config(), whatsapp=wa, classificar=ia, transcrever=transcrever,
                            agora=AGORA)
    assert len(pedidos) == 1


def test_falha_na_transcricao_deixa_para_a_equipe(engine):
    _enviar_tudo(engine)
    _audio(engine)
    wa = WhatsAppFalso()

    def expirou(midia_id):
        raise RuntimeError("mídia expirada")
    wa.baixar_midia = expirou
    automacao.rodar_uma_vez(engine, automacao.Config(), whatsapp=wa, classificar=ClaudeFalso(),
                            transcrever=lambda c, t: "nunca chega aqui", agora=AGORA)
    r = _resposta(engine)
    assert r.status == respostas.REVISAR and "ouça e escreva" in r.motivo_revisao
    assert automacao.ultima_rotina(engine, "transcrever:")["resultado"].startswith("erro: mídia expirada")


def test_audio_antigo_nao_e_transcrito(engine):
    _enviar_tudo(engine)
    _audio(engine, horas=24 * 8)
    feitos = automacao.transcrever_audios(engine, lambda m: (b"OggS", "audio/ogg"), lambda c, t: "oi", AGORA)
    assert feitos == 0
