"""Perfil da Empresa no Google (Google Business Profile): leitura das avaliações e publicação da resposta.

Endpoints usados (documentação oficial do Google):
- Token: POST https://oauth2.googleapis.com/token (grant_type=refresh_token), escopo
  https://www.googleapis.com/auth/business.manage.
- Contas: GET https://mybusinessaccountmanagement.googleapis.com/v1/accounts
- Locais da conta: GET https://mybusinessbusinessinformation.googleapis.com/v1/{conta}/locations?readMask=name,title
- Avaliações: GET https://mybusiness.googleapis.com/v4/{conta}/{local}/reviews (accounts.locations.reviews.list)
- Resposta: PUT https://mybusiness.googleapis.com/v4/{avaliação}/reply (accounts.locations.reviews.updateReply);
  cria a resposta ou troca a que já existe. Só funciona em local verificado.

O Google só libera essas APIs depois do pedido de acesso (formulário do Business Profile API). Antes disso as
chamadas voltam com cota zero (HTTP 429) ou API desativada (HTTP 403).

Variáveis (só no Railway): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN e, opcional,
GOOGLE_LOCAL ("accounts/123/locations/456"); sem GOOGLE_LOCAL o painel usa o único local da conta.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

URL_TOKEN = "https://oauth2.googleapis.com/token"
URL_CONTAS = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
URL_INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
URL_V4 = "https://mybusiness.googleapis.com/v4"
ESCOPO = "https://www.googleapis.com/auth/business.manage"
TAMANHO_MAXIMO = 4000  # o Google aceita até 4096 bytes na resposta; folga para acentos

ESTRELAS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


class GoogleErro(RuntimeError):
    pass


@dataclass
class Local:
    nome: str    # "accounts/123/locations/456" (formato da API v4)
    titulo: str  # nome do estabelecimento no Google


def _data(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def ler_avaliacao(r: dict[str, Any]) -> dict[str, Any]:
    """Avaliação da API → dicionário do painel. Estrelas 0 = o Google não informou."""
    autor = r.get("reviewer") or {}
    resposta = r.get("reviewReply") or {}
    return {
        "review_id": str(r.get("reviewId") or r.get("name", "").rsplit("/", 1)[-1]),
        "nome": r.get("name", ""),
        "autor": "" if autor.get("isAnonymous") else (autor.get("displayName") or "").strip(),
        "estrelas": ESTRELAS.get(r.get("starRating", ""), 0),
        "comentario": (r.get("comment") or "").strip(),
        "criada_em": _data(r.get("createTime")),
        "atualizada_em": _data(r.get("updateTime")),
        "resposta_google": (resposta.get("comment") or "").strip(),
    }


def _mensagem_de_erro(r: requests.Response) -> str:
    try:
        erro = r.json().get("error", {})
    except ValueError:
        erro = {}
    if isinstance(erro, str):  # resposta do endpoint de token
        if erro == "invalid_grant":
            return ("A autorização do Google expirou ou foi revogada. Gere um GOOGLE_REFRESH_TOKEN novo e troque "
                    "no Railway.")
        return f"O Google recusou a autorização ({erro})."
    status = erro.get("status", "")
    texto = (erro.get("message") or "")[:200]
    if r.status_code == 429 or status == "RESOURCE_EXHAUSTED":
        return ("Cota zerada no Google. Isso é o normal enquanto o pedido de acesso à API do Perfil da Empresa "
                "não for aprovado.")
    if r.status_code == 403 and ("SERVICE_DISABLED" in r.text or "has not been used" in texto):
        return "A API do Perfil da Empresa não está ativada no projeto do Google Cloud."
    if r.status_code == 403:
        return f"O Google negou o acesso (HTTP 403). A conta autorizada precisa ser dona ou administradora do perfil. {texto}"
    if r.status_code == 401:
        return "A autorização do Google não vale mais. Gere um GOOGLE_REFRESH_TOKEN novo e troque no Railway."
    if r.status_code == 404:
        return "O Google não encontrou o local ou a avaliação. Confira o GOOGLE_LOCAL."
    return f"O Google respondeu com erro (HTTP {r.status_code}). {texto}".strip()


class PerfilGoogle:
    """Cliente mínimo do Perfil da Empresa. As credenciais vêm do ambiente e nunca vão para o log."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, local: str = "",
                 sessao: requests.Session | None = None):
        self._id, self._segredo, self._refresh = client_id, client_secret, refresh_token
        self._local = local.strip().strip("/")
        self._http = sessao or requests.Session()
        self._token: str | None = None
        self._expira = 0.0

    # ------------------------------------------------------------------ autorização
    def _bearer(self) -> str:
        if self._token and time.monotonic() < self._expira - 60:
            return self._token
        try:
            r = self._http.post(URL_TOKEN, timeout=(10, 30), data={
                "grant_type": "refresh_token", "client_id": self._id, "client_secret": self._segredo,
                "refresh_token": self._refresh})
        except requests.RequestException as exc:
            raise GoogleErro("Falha de rede ao pedir autorização ao Google.") from exc
        if r.status_code >= 400:
            raise GoogleErro(_mensagem_de_erro(r))
        d = r.json()
        self._token = d["access_token"]
        self._expira = time.monotonic() + float(d.get("expires_in", 3600))
        return self._token

    def _pedir(self, metodo: str, url: str, **kw) -> dict[str, Any]:
        try:
            r = self._http.request(metodo, url, timeout=(10, 30),
                                   headers={"Authorization": f"Bearer {self._bearer()}"}, **kw)
        except requests.RequestException as exc:
            raise GoogleErro("Falha de rede ao falar com o Google.") from exc
        if r.status_code >= 400:
            raise GoogleErro(_mensagem_de_erro(r))
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ local (estabelecimento)
    def locais(self) -> list[Local]:
        """Todos os locais que a conta autorizada administra."""
        achados: list[Local] = []
        contas = self._pedir("GET", URL_CONTAS).get("accounts", [])
        for conta in contas:
            token = ""
            while True:
                params = {"readMask": "name,title", "pageSize": 100}
                if token:
                    params["pageToken"] = token
                d = self._pedir("GET", f"{URL_INFO}/{conta['name']}/locations", params=params)
                for loc in d.get("locations", []):
                    achados.append(Local(nome=f"{conta['name']}/{loc['name']}", titulo=loc.get("title", "")))
                token = d.get("nextPageToken", "")
                if not token:
                    break
        return achados

    def local(self) -> str:
        """GOOGLE_LOCAL, ou o único local da conta. Mais de um local sem GOOGLE_LOCAL é erro."""
        if self._local:
            return self._local
        achados = self.locais()
        if len(achados) == 1:
            self._local = achados[0].nome
            return self._local
        if not achados:
            raise GoogleErro("A conta autorizada não administra nenhum Perfil da Empresa.")
        nomes = "; ".join(f"{l.titulo} = {l.nome}" for l in achados[:5])
        raise GoogleErro(f"A conta tem {len(achados)} locais. Coloque o certo em GOOGLE_LOCAL no Railway: {nomes}")

    # ------------------------------------------------------------------ avaliações
    def avaliacoes(self, paginas: int = 4) -> Iterator[dict[str, Any]]:
        """Avaliações da mais recente para a mais antiga (por atualização), até `paginas` páginas de 50."""
        local = self.local()
        token = ""
        for _ in range(max(1, paginas)):
            params: dict[str, Any] = {"pageSize": 50, "orderBy": "updateTime desc"}
            if token:
                params["pageToken"] = token
            d = self._pedir("GET", f"{URL_V4}/{local}/reviews", params=params)
            for r in d.get("reviews", []):
                yield ler_avaliacao(r)
            token = d.get("nextPageToken", "")
            if not token:
                return

    def responder(self, nome_avaliacao: str, texto: str) -> None:
        """Publica (ou troca) a resposta da empresa na avaliação."""
        if not nome_avaliacao.startswith("accounts/"):
            raise GoogleErro("Avaliação sem identificador do Google.")
        if len(texto.encode("utf-8")) > TAMANHO_MAXIMO:
            raise GoogleErro("A resposta passou do tamanho que o Google aceita. Encurte o texto.")
        self._pedir("PUT", f"{URL_V4}/{nome_avaliacao}/reply", json={"comment": texto})
