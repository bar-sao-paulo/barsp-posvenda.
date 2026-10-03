"""Banco do pós-venda (Postgres no Railway; SQLite em memória nos testes).

Guarda só o necessário para o envio: lista do dia, quem recebeu (regra dos 7 dias) e quem não quer receber.
"""
from __future__ import annotations

import os

from sqlalchemy import (Column, Date, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

# Uma linha por cliente que fica na lista do dia (data_visita = dia da visita/pedido).
envios = Table(
    "envios", metadata,
    Column("id", Integer, primary_key=True),
    Column("data_visita", Date, nullable=False, index=True),
    Column("telefone", String(20), nullable=False, index=True),
    Column("nome_completo", String(200), nullable=False, default=""),
    Column("nome", String(100)),
    Column("origem", String(20), nullable=False),
    Column("prato", String(200)),
    Column("versao", String(1), nullable=False),
    Column("mensagem", Text, nullable=False),
    Column("enviado_em", DateTime(timezone=True)),
    Column("criado_em", DateTime(timezone=True), nullable=False),
    UniqueConstraint("data_visita", "telefone", name="uq_envio_dia_telefone"),
)

# Quem saiu da lista do dia e por quê (refeito a cada busca no Takeat).
nao_enviados = Table(
    "nao_enviados", metadata,
    Column("id", Integer, primary_key=True),
    Column("data_visita", Date, nullable=False, index=True),
    Column("nome_completo", String(200), nullable=False, default=""),
    Column("origem", String(20), nullable=False),
    Column("motivo", String(120), nullable=False),
)

# Pediu para não receber a pesquisa.
bloqueios = Table(
    "bloqueios", metadata,
    Column("telefone", String(20), primary_key=True),
    Column("motivo", String(200), nullable=False, default=""),
    Column("criado_em", DateTime(timezone=True), nullable=False),
)

# Quando a lista de cada dia foi buscada no Takeat.
buscas = Table(
    "buscas", metadata,
    Column("data_visita", Date, primary_key=True),
    Column("buscado_em", DateTime(timezone=True), nullable=False),
)


def _url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def criar_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL") or "sqlite://"
    if url == "sqlite://":
        engine = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(_url(url), pool_pre_ping=True)
    metadata.create_all(engine)
    return engine
