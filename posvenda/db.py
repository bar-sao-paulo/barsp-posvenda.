"""Banco do pós-venda (Postgres no Railway; SQLite em memória nos testes).

Guarda só o necessário para o envio: lista do dia, quem recebeu (regra dos 7 dias) e quem não quer receber.
"""
from __future__ import annotations

import os

from sqlalchemy import (Boolean, Column, Date, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, inspect, text)
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
    # Envio pela API do WhatsApp (etapa 2): id da mensagem, situação (sent/delivered/read/failed) e erro.
    Column("wa_message_id", String(100)),
    Column("wa_status", String(20)),
    Column("erro", String(300)),
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


# Toda mensagem recebida ou enviada pela API do WhatsApp (envio_id vazio = número fora da lista).
mensagens = Table(
    "mensagens", metadata,
    Column("id", Integer, primary_key=True),
    Column("telefone", String(20), nullable=False, index=True),
    Column("envio_id", Integer, ForeignKey("envios.id", ondelete="SET NULL"), index=True),
    Column("direcao", String(10), nullable=False),  # "entrada" ou "saida"
    Column("texto", Text, nullable=False, default=""),
    Column("wa_id", String(100), unique=True),
    Column("criado_em", DateTime(timezone=True), nullable=False),
)

# Uma linha por cliente que respondeu a pesquisa: análise da IA e o que a equipe fez.
respostas = Table(
    "respostas", metadata,
    Column("id", Integer, primary_key=True),
    Column("envio_id", Integer, ForeignKey("envios.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("nota", Integer),
    Column("nota_estimada", Boolean, nullable=False, default=False),
    Column("gravidade", String(20)),
    Column("resumo", String(300), nullable=False, default=""),
    Column("historico", String(200), nullable=False, default=""),
    Column("resposta_sugerida", Text, nullable=False, default=""),
    Column("acao_sugerida", Text, nullable=False, default=""),
    Column("status", String(40), nullable=False),
    Column("motivo_revisao", String(300), nullable=False, default=""),
    Column("ultima_mensagem_em", DateTime(timezone=True), nullable=False),
    Column("classificada_em", DateTime(timezone=True)),
    Column("respondida_em", DateTime(timezone=True)),
    Column("alerta_em", DateTime(timezone=True)),
)

# Trava das rotinas automáticas (ex.: "envio:2026-10-02"), para não rodar duas vezes.
rotinas = Table(
    "rotinas", metadata,
    Column("chave", String(60), primary_key=True),
    Column("rodou_em", DateTime(timezone=True), nullable=False),
    Column("resultado", String(300), nullable=False, default=""),
)

# Colunas criadas depois que a tabela já existia no Railway (migração simples por ALTER TABLE).
_COLUNAS_NOVAS = {
    "envios": {"wa_message_id": "VARCHAR(100)", "wa_status": "VARCHAR(20)", "erro": "VARCHAR(300)"},
}


def _migrar(engine: Engine) -> None:
    insp = inspect(engine)
    for tabela, colunas in _COLUNAS_NOVAS.items():
        existentes = {c["name"] for c in insp.get_columns(tabela)}
        with engine.begin() as con:
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    con.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))


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
    _migrar(engine)
    return engine
