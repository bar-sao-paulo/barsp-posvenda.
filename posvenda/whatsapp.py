"""WhatsApp Cloud API (Meta): envio de modelos e textos, e leitura do webhook.

Referências (developers.facebook.com/docs/whatsapp/cloud-api):
- POST /{phone-number-id}/register (messaging_product + pin de 6 dígitos): registra o número na Cloud API; sem isso
  o WhatsApp Manager mostra "Pendente". GET /{phone-number-id}?fields=...: situação do número.
- POST/GET /{waba-id}/subscribed_apps: liga o app à conta do WhatsApp (WABA). Sem isso a Meta não manda as
  mensagens recebidas para o webhook, mesmo com o webhook verificado.
- POST /{phone-number-id}/messages, type=template (primeira mensagem, modelo aprovado) ou type=text
  (só dentro das 24 h depois da última mensagem do cliente).
- Webhook: GET com hub.mode/hub.verify_token/hub.challenge para a verificação; POST com
  entry[].changes[].value.messages[] (mensagens recebidas) e .statuses[] (sent/delivered/read/failed),
  assinado no cabeçalho X-Hub-Signature-256 (HMAC-SHA256 do corpo com o App Secret).
Nenhum token vai para logs ou mensagens de erro.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

TIMEOUT = (10, 30)


class WhatsAppErro(RuntimeError):
    def __init__(self, mensagem: str, codigo: int | None = None):
        super().__init__(mensagem)
        self.codigo = codigo


class WhatsApp:
    def __init__(self, token: str, phone_number_id: str, versao: str = "v23.0",
                 session: requests.Session | None = None):
        self._token = token
        self._raiz = f"https://graph.facebook.com/{versao}"
        self._base = f"{self._raiz}/{phone_number_id}"
        self._http = session or requests.Session()

    def __repr__(self) -> str:  # nunca expor o token
        return "WhatsApp(...)"

    def _pedir(self, metodo: str, caminho: str, payload: dict[str, Any] | None = None,
               params: dict[str, str] | None = None, url: str | None = None) -> dict[str, Any]:
        cabecalho = {"Authorization": f"Bearer {self._token}"}
        url = url or self._base + caminho
        try:
            if metodo == "GET":
                resp = self._http.get(url, params=params, timeout=TIMEOUT, headers=cabecalho)
            else:
                resp = self._http.post(url, json=payload, timeout=TIMEOUT, headers=cabecalho)
        except requests.RequestException as exc:
            raise WhatsAppErro(f"Falha de rede ao falar com o WhatsApp: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            try:
                erro = resp.json().get("error", {})
            except ValueError:
                erro = {}
            codigo = erro.get("code")
            detalhe = (erro.get("error_data") or {}).get("details") or erro.get("message") or ""
            raise WhatsAppErro(f"WhatsApp recusou (HTTP {resp.status_code}, código {codigo}): {detalhe[:200]}", codigo)
        try:
            dados = resp.json()
        except ValueError:
            return {}
        return dados if isinstance(dados, dict) else {}

    def _post(self, payload: dict[str, Any]) -> str:
        try:
            return str(self._pedir("POST", "/messages", payload)["messages"][0]["id"])
        except (KeyError, IndexError, TypeError):
            return ""

    def situacao_do_numero(self) -> dict[str, Any]:
        """Nome, número, status e avaliação do nome de exibição, como a Meta devolve."""
        return self._pedir("GET", "", params={"fields": "display_phone_number,verified_name,name_status,status,"
                                                        "code_verification_status,quality_rating"})

    def registrar_numero(self, pin: str) -> None:
        """Registra o número na Cloud API com o PIN de 6 dígitos (verificação em duas etapas). O PIN não é guardado."""
        if not (len(pin) == 6 and pin.isdigit()):
            raise WhatsAppErro("O PIN precisa ter exatamente 6 números.")
        self._pedir("POST", "/register", {"messaging_product": "whatsapp", "pin": pin})

    def _url_da_conta(self, waba_id: str) -> str:
        if not waba_id.isdigit():
            raise WhatsAppErro("A identificação da conta do WhatsApp precisa ter só números.")
        return f"{self._raiz}/{waba_id}/subscribed_apps"

    def apps_ligados(self, waba_id: str) -> list[str]:
        """Nomes dos apps que recebem as mensagens desta conta do WhatsApp."""
        dados = self._pedir("GET", "", url=self._url_da_conta(waba_id)).get("data") or []
        return [str((d.get("whatsapp_business_api_data") or {}).get("name") or d.get("name") or "app sem nome")
                for d in dados if isinstance(d, dict)]

    def ligar_app(self, waba_id: str) -> None:
        """Liga este app à conta do WhatsApp, para a Meta mandar as mensagens recebidas ao webhook."""
        self._pedir("POST", "", url=self._url_da_conta(waba_id))

    def enviar_modelo(self, telefone: str, modelo: str, parametros: list[str], idioma: str = "pt_BR") -> str:
        return self._post({
            "messaging_product": "whatsapp", "to": telefone, "type": "template",
            "template": {"name": modelo, "language": {"code": idioma},
                         "components": [{"type": "body",
                                         "parameters": [{"type": "text", "text": p} for p in parametros]}]},
        })

    def enviar_texto(self, telefone: str, texto: str) -> str:
        return self._post({"messaging_product": "whatsapp", "to": telefone, "type": "text",
                           "text": {"body": texto, "preview_url": False}})


def assinatura_valida(app_secret: str, corpo: bytes, cabecalho: str | None) -> bool:
    if not app_secret or not cabecalho or not cabecalho.startswith("sha256="):
        return False
    esperado = hmac.new(app_secret.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecalho[len("sha256="):])


@dataclass
class Recebida:
    telefone: str
    texto: str
    wa_id: str
    quando: datetime


@dataclass
class Situacao:
    wa_id: str
    status: str
    erro: str


_TIPOS_SEM_TEXTO = {
    "image": "[foto]", "audio": "[áudio]", "video": "[vídeo]", "sticker": "[figurinha]",
    "document": "[documento]", "location": "[localização]", "contacts": "[contato]",
}


def _texto(m: dict[str, Any]) -> str:
    tipo = m.get("type")
    if tipo == "text":
        return (m.get("text") or {}).get("body", "")
    if tipo == "button":
        return (m.get("button") or {}).get("text", "")
    if tipo == "interactive":
        i = m.get("interactive") or {}
        return ((i.get("button_reply") or i.get("list_reply") or {}).get("title", ""))
    if tipo == "reaction":
        return (m.get("reaction") or {}).get("emoji", "")
    legenda = ((m.get(tipo) or {}) if isinstance(m.get(tipo), dict) else {}).get("caption", "")
    return " ".join(x for x in (_TIPOS_SEM_TEXTO.get(tipo or "", f"[{tipo}]"), legenda) if x)


def ler_webhook(payload: dict[str, Any]) -> tuple[list[Recebida], list[Situacao]]:
    recebidas: list[Recebida] = []
    situacoes: list[Situacao] = []
    for entrada in payload.get("entry") or []:
        for mudanca in entrada.get("changes") or []:
            valor = mudanca.get("value") or {}
            for m in valor.get("messages") or []:
                try:
                    quando = datetime.fromtimestamp(int(m.get("timestamp", "0")), tz=timezone.utc)
                except (TypeError, ValueError):
                    quando = datetime.now(timezone.utc)
                recebidas.append(Recebida(str(m.get("from", "")), _texto(m).strip(), str(m.get("id", "")), quando))
            for s in valor.get("statuses") or []:
                erros = s.get("errors") or []
                detalhe = ""
                if erros:
                    e = erros[0]
                    detalhe = f"{e.get('code', '')} {(e.get('error_data') or {}).get('details') or e.get('title', '')}".strip()
                situacoes.append(Situacao(str(s.get("id", "")), str(s.get("status", "")), detalhe[:300]))
    return recebidas, situacoes
