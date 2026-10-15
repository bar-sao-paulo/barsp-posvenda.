"""Transcrição automática dos áudios dos clientes (o Claude não entende áudio).

Serviço: OpenAI, POST https://api.openai.com/v1/audio/transcriptions (multipart: arquivo, modelo, idioma).
Liga sozinho quando a variável OPENAI_API_KEY existe no Railway; sem ela, a equipe ouve no painel e escreve.
O áudio vai para a OpenAI só para virar texto; o painel não guarda o arquivo. O texto fica no banco do pós-venda.
"""
from __future__ import annotations

import re

import requests

URL = "https://api.openai.com/v1/audio/transcriptions"
MODELO_PADRAO = "whisper-1"
TIMEOUT = (10, 120)
_EXTENSOES = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/amr": "amr",
              "audio/wav": "wav", "audio/webm": "webm"}


class TranscricaoErro(RuntimeError):
    pass


class Transcritor:
    def __init__(self, chave: str, modelo: str = MODELO_PADRAO, session: requests.Session | None = None):
        self._chave = chave
        self._modelo = modelo
        self._http = session or requests.Session()

    def __repr__(self) -> str:  # nunca expor a chave
        return f"Transcritor(modelo={self._modelo!r})"

    def __call__(self, conteudo: bytes, tipo: str) -> str:
        base = (tipo or "").split(";")[0].strip().lower()
        nome = f"audio.{_EXTENSOES.get(base, 'ogg')}"
        try:
            resp = self._http.post(URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {self._chave}"},
                                   files={"file": (nome, conteudo, base or "audio/ogg")},
                                   data={"model": self._modelo, "language": "pt", "response_format": "json"})
        except requests.RequestException as exc:
            raise TranscricaoErro(f"Falha de rede ao transcrever: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            try:
                detalhe = (resp.json().get("error") or {}).get("message") or ""
            except ValueError:
                detalhe = ""
            detalhe = re.sub(r"sk-[\w*-]+", "sk-…", detalhe)  # a OpenAI repete parte da chave no erro
            raise TranscricaoErro(f"A transcrição recusou (HTTP {resp.status_code}): {detalhe[:200]}")
        try:
            return str(resp.json().get("text") or "").strip()
        except ValueError as exc:
            raise TranscricaoErro("A transcrição devolveu uma resposta inválida.") from exc
