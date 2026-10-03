"""Cliente mínimo da Nova API V1.0 da Takeat (adaptado do barsp-gestao, sem dependência dele).

Contratos usados (fonte: docs.takeat.app):
- POST /oauth/token, grant_type=api_key        -> md/v1/referencia/autenticacao/exchangeApiKey.md
- GET  /v1/table-sessions?start_date&end_date  -> md/v1/referencia/pedidos/getTableSessionsV1.md
  (escopo table-sessions:read; datas em UTC; intervalo máximo de três dias; resposta = lista)
- Tratamento de 401/403/429                     -> md/v1/primeiros-passos.md, md/v1/api-key-tokens.md

Decisão: cada uso troca a API key por um access token novo, restrito ao escopo necessário, e não
persiste o refresh token. Nenhum segredo vai para disco ou para logs.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (10, 30)  # conexão, leitura (segundos)


class TakeatError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, key: str | None = None):
        super().__init__(message)
        self.status = status
        self.key = key


class TakeatAuthError(TakeatError):
    """Chave inválida, expirada ou revogada (401 na troca de token)."""


class TakeatForbidden(TakeatError):
    """403: falta escopo. Não adianta repetir; ajuste as permissões da chave."""


@dataclass
class _Token:
    access_token: str
    expires_at: float


class TakeatClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://public-api.takeat.app",
        scopes: tuple[str, ...] = (),
        session: requests.Session | None = None,
        max_retries: int = 4,
        sleep=time.sleep,
    ):
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._scopes = scopes
        self._http = session or requests.Session()
        self._max_retries = max_retries
        self._sleep = sleep
        self._token: _Token | None = None

    def __repr__(self) -> str:  # nunca expor a chave em logs
        return f"TakeatClient(base_url={self._base!r}, scopes={self._scopes!r})"

    # ------------------------------------------------------------------ auth
    def _exchange(self) -> None:
        data = {"grant_type": "api_key", "api_key": self._api_key}
        if self._scopes:
            data["scope"] = " ".join(self._scopes)
        resp = self._send("POST", "/oauth/token", data=data, auth=False)
        if resp.status_code == 401:
            raise TakeatAuthError(
                "API key recusada (inválida, expirada ou revogada). Gere outra no Takeat.",
                401,
                _oauth_error(resp),
            )
        if resp.status_code == 400:
            raise TakeatError(
                f"Troca de token recusada: {_oauth_error(resp)}. Verifique os escopos da chave.",
                400,
                _oauth_error(resp),
            )
        _raise_for_status(resp)
        body = resp.json()
        ttl = int(body.get("expires_in", 900))
        # margem de 60 s para não usar um token prestes a expirar
        self._token = _Token(body["access_token"], time.monotonic() + max(ttl - 60, 30))
        log.info("Token emitido com escopos: %s", body.get("scope", ""))

    def _access_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token.expires_at:
            self._exchange()
        assert self._token is not None
        return self._token.access_token

    # ------------------------------------------------------------------ http
    def _send(self, method: str, path: str, *, auth: bool = True, **kw) -> requests.Response:
        """Envia com retry para 429 (Retry-After) e 5xx/erros de rede, com backoff e jitter."""
        url = f"{self._base}{path}"
        for attempt in range(self._max_retries + 1):
            headers = kw.pop("headers", {}) or {}
            if auth:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            try:
                resp = self._http.request(method, url, headers=headers, timeout=DEFAULT_TIMEOUT, **kw)
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise TakeatError(f"Falha de rede em {method} {path}: {type(exc).__name__}") from exc
                self._backoff(attempt, None)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    return resp
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp
        raise AssertionError("inalcançável")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            wait = float(retry_after)
        else:
            wait = min(2 ** attempt, 30) + random.uniform(0, 1)
        log.warning("Aguardando %.1fs antes de repetir (tentativa %d)", wait, attempt + 1)
        self._sleep(wait)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._send("GET", path, params=params)
        if resp.status_code == 401:
            # token pode ter sido revogado/expirado: renova uma vez e repete uma vez
            self._token = None
            resp = self._send("GET", path, params=params)
        if resp.status_code == 403:
            raise TakeatForbidden(
                f"Sem permissão para {path}: a chave precisa do escopo correspondente.", 403, _api_key(resp)
            )
        _raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------ recursos
    def listar_sessoes(self, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
        """Comandas (mesa, delivery, balcão) entre duas datas UTC. Escopo: table-sessions:read."""
        params = {"start_date": _iso_utc(inicio), "end_date": _iso_utc(fim)}
        dados = self.get("/v1/table-sessions", params=params)
        if not isinstance(dados, list):
            raise TakeatError("Resposta inesperada do Takeat em /v1/table-sessions (esperava uma lista).")
        return dados


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _oauth_error(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", "erro_desconhecido"))
    except ValueError:
        return "erro_desconhecido"


def _api_key(resp: requests.Response) -> str | None:
    try:
        return resp.json().get("key")
    except ValueError:
        return None


def _raise_for_status(resp: requests.Response) -> None:
    if resp.status_code >= 400:
        raise TakeatError(
            f"Takeat respondeu HTTP {resp.status_code} em {resp.request.method} {resp.request.path_url.split('?')[0]}",
            resp.status_code,
            _api_key(resp),
        )
