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
from dataclasses import dataclass
from typing import Any, Callable

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


def selecionar(clientes: list[Cliente], topo: int | None = None, minimo: int = 0) -> list[Cliente]:
    """Do que mais pediu para o que menos pediu (empate: quem gastou mais); só quem tem `minimo` pedidos ou mais."""
    lista = sorted((c for c in clientes if c.pedidos >= minimo), key=lambda c: (-c.pedidos, -c.total_gasto, c.nome))
    return lista[:topo] if topo else lista


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
