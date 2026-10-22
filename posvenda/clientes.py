"""Clientes do Clube de Fidelidade do Takeat, do que mais pediu para o que menos pediu (tela Clientes).

Fonte: GET /v2/clube/clients (md/v1/referencia/clube-de-fidelidade/getClubeClientsV1PaginatedV2.md), escopo clube:read.
"Pedidos" = campo `visits` do Clube. A lista fica só na memória do painel por 1 hora (não é gravada no banco).
"""
from __future__ import annotations

import csv
import io
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from . import lista as _lista
from .db import mensagens_clientes

VALIDADE = 3600  # segundos


@dataclass
class Cliente:
    nome: str
    telefone: str  # só dígitos, com 55
    pedidos: int
    total_gasto: float
    favorito: str


def _numero(v: Any) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _telefone(v: Any) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return "55" + d if len(d) in (10, 11) else d


def normalizar(dados: list[dict[str, Any]]) -> list[Cliente]:
    return [Cliente(nome=" ".join(str(c.get("name") or "").split()), telefone=_telefone(c.get("phone")),
                    pedidos=int(c.get("visits") or 0), total_gasto=_numero(c.get("total_spent")),
                    favorito=" ".join(str(c.get("favorite_product") or "").split()))
            for c in dados]


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower()


def selecionar(clientes: list[Cliente], topo: int | None = None, minimo: int = 0, busca: str = "") -> list[Cliente]:
    """Do que mais pediu para o que menos pediu (empate: quem gastou mais); só quem tem `minimo` pedidos ou mais e,
    com `busca`, só quem tem esse pedaço no nome (sem acento) ou no telefone."""
    termo, digitos = _sem_acento(busca.strip()), re.sub(r"\D", "", busca)
    lista = sorted((c for c in clientes if c.pedidos >= minimo and (
        not termo or termo in _sem_acento(c.nome) or (len(digitos) >= 4 and digitos in c.telefone))),
        key=lambda c: (-c.pedidos, -c.total_gasto, c.nome))
    return lista[:topo] if topo else lista


def primeiro_nome(nome: str) -> str:
    return nome.split()[0].capitalize() if nome.split() else ""


class Cache:
    """Guarda a lista do Clube por VALIDADE segundos para não ler o Takeat a cada clique."""

    def __init__(self, buscar: Callable[[], list[dict[str, Any]]], relogio: Callable[[], float] = time.monotonic):
        self._buscar, self._relogio = buscar, relogio
        self._trava = threading.Lock()
        self._dados: list[Cliente] | None = None
        self._quando = 0.0

    def obter(self, atualizar: bool = False) -> list[Cliente]:
        with self._trava:
            if atualizar or self._dados is None or self._relogio() - self._quando > VALIDADE:
                self._dados = normalizar(self._buscar())
                self._quando = self._relogio()
            return self._dados

    def idade_minutos(self) -> int | None:
        return None if self._dados is None else int((self._relogio() - self._quando) // 60)


def planilha_csv(lista: list[Cliente]) -> str:
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\n")
    w.writerow(["Posição", "Nome", "Telefone", "Pedidos", "Total gasto (R$)", "Produto favorito"])
    for i, c in enumerate(lista, 1):
        w.writerow([i, c.nome, c.telefone, c.pedidos, f"{c.total_gasto:.2f}".replace(".", ","), c.favorito])
    return saida.getvalue()


# --------------------------------------------------------------------------- mensagens avulsas (separadas da pesquisa)
def registrar_mensagem(engine: Engine, telefone: str, nome: str, texto: str, usuario: str = "",
                       agora: datetime | None = None) -> None:
    """Guarda a mensagem que a equipe abriu no WhatsApp para um cliente da aba Clientes."""
    tel = re.sub(r"\D", "", telefone or "")
    texto = (texto or "").strip()
    if not 10 <= len(tel) <= 13 or not texto:
        raise ValueError("Telefone ou mensagem inválidos.")
    with engine.begin() as con:
        con.execute(insert(mensagens_clientes).values(
            chave=_lista.chave_telefone(tel), telefone=tel, nome=(nome or "").strip()[:200], texto=texto[:4000],
            enviado_em=agora or datetime.now(timezone.utc), usuario=(usuario or "")[:60]))


def mensagens_por_cliente(engine: Engine, telefones: list[str]) -> dict[str, list[dict[str, Any]]]:
    """chave do telefone -> mensagens já mandadas pela aba Clientes, da mais nova para a mais antiga."""
    chaves = {_lista.chave_telefone(t) for t in telefones if t}
    if not chaves:
        return {}
    saida: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as con:
        for r in con.execute(select(mensagens_clientes).where(mensagens_clientes.c.chave.in_(chaves))
                             .order_by(mensagens_clientes.c.enviado_em.desc(), mensagens_clientes.c.id.desc())):
            saida.setdefault(r.chave, []).append(dict(r._mapping))
    return saida


def ultimas_mensagens(engine: Engine, limite: int = 30) -> list[dict[str, Any]]:
    with engine.connect() as con:
        return [dict(r._mapping) for r in con.execute(
            select(mensagens_clientes).order_by(mensagens_clientes.c.enviado_em.desc(), mensagens_clientes.c.id.desc())
            .limit(limite))]
