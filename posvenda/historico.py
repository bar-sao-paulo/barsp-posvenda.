"""Histórico de compras do cliente: em quantos dias ele teve comanda no bar nos últimos 12 meses.

Fonte: GET /v1/table-sessions (md/v1/referencia/pedidos/getTableSessionsV1.md), a mesma consulta da lista.
A Takeat aceita no máximo três dias por consulta (datas em UTC), então os 12 meses são lidos uma vez, em blocos
de 3 dias, poucos por minuto, pelo agendador. Cada bloco já lido não é buscado de novo; a lista do dia também
grava as visitas do dia. Contamos dias (duas contas no mesmo dia = uma compra), pela chave do telefone.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable

from collections import Counter

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import lista
from .db import historico_blocos, visitas

log = logging.getLogger(__name__)

DIAS = 365
BLOCO = 3
_EPOCA = date(2000, 1, 1)  # blocos sempre alinhados na mesma grade, para não reler dias

BuscarSessoes = Callable[[datetime, datetime], list[dict[str, Any]]]


def inicio_do_bloco(dia: date) -> date:
    return dia - timedelta(days=(dia - _EPOCA).days % BLOCO)


def blocos_pendentes(engine: Engine, hoje: date) -> list[date]:
    """Blocos já encerrados (o último dia é antes de hoje) dentro dos 12 meses e ainda não lidos, do mais novo
    para o mais antigo."""
    ultimo = inicio_do_bloco(hoje - timedelta(days=1))
    if ultimo + timedelta(days=BLOCO - 1) >= hoje:
        ultimo -= timedelta(days=BLOCO)
    primeiro = inicio_do_bloco(hoje - timedelta(days=DIAS))
    with engine.connect() as con:
        lidos = {r[0] for r in con.execute(select(historico_blocos.c.inicio).where(
            historico_blocos.c.inicio >= primeiro))}
    saida, b = [], ultimo
    while b >= primeiro:
        if b not in lidos:
            saida.append(b)
        b -= timedelta(days=BLOCO)
    return saida


def gravar(engine: Engine, sessoes: Iterable[dict[str, Any]], de: date, ate: date) -> int:
    """Grava as visitas (e o prato principal de cada uma) das comandas entre `de` e `ate`. Devolve quantas eram
    novas. Visita já gravada sem prato ganha o prato."""
    sessoes = list(sessoes)
    achadas = lista.visitas_das_sessoes(sessoes, de, ate)
    if not achadas:
        return 0
    pratos = lista.pratos_das_sessoes(sessoes, de, ate)
    with engine.connect() as con:
        ja = {(r.chave, r.dia): r.prato for r in con.execute(select(visitas.c.chave, visitas.c.dia, visitas.c.prato)
                                                               .where(and_(visitas.c.dia >= de, visitas.c.dia <= ate)))}
    sem_prato = [(k, pratos[k]) for k, p in ja.items() if p is None and pratos.get(k)]
    if sem_prato:
        with engine.begin() as con:
            for (c, d), prato in sem_prato:
                con.execute(update(visitas).where(and_(visitas.c.chave == c, visitas.c.dia == d))
                            .values(prato=prato[:200]))
    novas = sorted(achadas - set(ja))
    if not novas:
        return 0
    linhas = [{"chave": c, "dia": d, "prato": (pratos.get((c, d)) or "")[:200] or None} for c, d in novas]
    try:
        with engine.begin() as con:
            con.execute(insert(visitas), linhas)
    except IntegrityError:  # outra rotina gravou parte ao mesmo tempo: grava uma a uma
        for linha in linhas:
            try:
                with engine.begin() as con:
                    con.execute(insert(visitas).values(**linha))
            except IntegrityError:
                pass
    return len(novas)


def reler_tudo(engine: Engine) -> int:
    """Esquece os blocos já lidos para o agendador reler os 12 meses (as visitas ficam; ganham o prato)."""
    with engine.begin() as con:
        return con.execute(delete(historico_blocos)).rowcount


def pratos_favoritos(engine: Engine, telefones: Iterable[str], hoje: date) -> dict[str, str]:
    """chave do telefone -> prato principal que o cliente mais pediu nos últimos 12 meses (empate: o mais recente)."""
    chaves = {lista.chave_telefone(t) for t in telefones if t}
    if not chaves:
        return {}
    por_chave: dict[str, list[tuple[date, str]]] = {}
    with engine.connect() as con:
        for r in con.execute(select(visitas.c.chave, visitas.c.dia, visitas.c.prato).where(and_(
                visitas.c.chave.in_(chaves), visitas.c.dia >= hoje - timedelta(days=DIAS),
                visitas.c.prato.is_not(None)))):
            por_chave.setdefault(r.chave, []).append((r.dia, r.prato))
    saida = {}
    for chave, lista_ in por_chave.items():
        conta = Counter(p for _, p in lista_)
        ultimo = {p: d for d, p in sorted(lista_)}
        saida[chave] = max(conta, key=lambda p: (conta[p], ultimo[p]))
    return saida


def janela_do_bloco(inicio: date) -> tuple[datetime, datetime]:
    """Do início do primeiro dia ao início do dia seguinte ao bloco, em horário de São Paulo (72 h, em UTC)."""
    de = datetime.combine(inicio, time(0), lista.TZ)
    ate = datetime.combine(inicio + timedelta(days=BLOCO), time(0), lista.TZ)
    return de.astimezone(timezone.utc), ate.astimezone(timezone.utc)


def carregar(engine: Engine, buscar: BuscarSessoes, hoje: date, *, limite: int = 4,
             agora: datetime | None = None) -> int:
    """Lê até `limite` blocos pendentes no Takeat. Devolve quantos blocos foram lidos."""
    lidos = 0
    for inicio in blocos_pendentes(engine, hoje)[:limite]:
        de, ate = janela_do_bloco(inicio)
        gravar(engine, buscar(de, ate), inicio, inicio + timedelta(days=BLOCO - 1))
        try:
            with engine.begin() as con:
                con.execute(insert(historico_blocos).values(inicio=inicio,
                                                            carregado_em=agora or datetime.now(timezone.utc)))
        except IntegrityError:
            pass
        lidos += 1
    return lidos


def completo(engine: Engine, hoje: date) -> bool:
    return not blocos_pendentes(engine, hoje)


def texto(n: int, primeira: date | None, carregando: bool = False) -> str:
    if n <= 0:
        return "histórico ainda carregando" if carregando else "sem compras registradas"
    if n == 1:
        return "1ª compra em 12 meses" + (" (histórico ainda carregando)" if carregando else "")
    return f"{n} compras em 12 meses (1ª em {primeira:%d/%m/%Y})"


def compras(engine: Engine, itens: list[tuple[str, date]], hoje: date | None = None) -> list[tuple[str, int]]:
    """Para cada (telefone, dia da visita): (texto, número de dias com compra nos 12 meses até aquele dia)."""
    if not itens:
        return []
    carregando = not completo(engine, hoje) if hoje else False
    chaves = {lista.chave_telefone(t) for t, _ in itens}
    menor = min(d for _, d in itens) - timedelta(days=DIAS)
    por_chave: dict[str, list[date]] = {}
    with engine.connect() as con:
        for r in con.execute(select(visitas.c.chave, visitas.c.dia).where(and_(
                visitas.c.chave.in_(chaves), visitas.c.dia >= menor))):
            por_chave.setdefault(r.chave, []).append(r.dia)
    saida = []
    for tel, dia in itens:
        dias = sorted(d for d in por_chave.get(lista.chave_telefone(tel), []) if dia - timedelta(days=DIAS) <= d <= dia)
        saida.append((texto(len(dias), dias[0] if dias else None, carregando), len(dias)))
    return saida
