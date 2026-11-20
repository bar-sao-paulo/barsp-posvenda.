"""Resumo do pós-venda, só de consulta e sem dados de cliente, para o painel financeiro do Bar São Paulo.

GET /api/resumo?de=AAAA-MM-DD&ate=AAAA-MM-DD
  Cabeçalho: Authorization: Bearer <PAINEL_API_TOKEN>
  Devolve, por mês da visita: pesquisas enviadas, respostas, nota média, quantas de cada gravidade e quantas
  estão em "Revisar antes de enviar". Só números: nenhum nome, telefone, texto ou resumo sai daqui.

GET /api/reputacao?de=AAAA-MM-DD&ate=AAAA-MM-DD (mesmo cabeçalho)
  Para a área Reputação do Painel de gestão, que lê na hora e não grava nada: os números acima com o NPS de cada mês,
  quantas respostas estão em revisão agora e as avaliações do Google (públicas): média, estrelas, situação e as
  últimas avaliações (estrelas, data, comentário e a resposta publicada), **sem o nome do autor**.
  Nenhum dado da pesquisa por WhatsApp (nome, telefone, texto, resumo) sai daqui.

Sem a variável PAINEL_API_TOKEN (mínimo 32 caracteres) o endereço não existe (404). O token só é comparado.
"""
from __future__ import annotations

import hmac
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from flask import abort, jsonify, request
from sqlalchemy import and_, func, select

from . import avaliacoes as G
from .db import avaliacoes_google, envios, respostas
from .lista import TZ
from .respostas import ATENCAO, CRITICO, POSITIVO, REVISAR

ULTIMAS_GOOGLE = 60

MAX_DIAS = 400


def resumo(engine, de: date, ate: date, com_nps: bool = False) -> list[dict]:
    meses: dict[str, dict] = defaultdict(lambda: dict(enviadas=0, respostas=0, soma_notas=0, com_nota=0,
                                                      notas_estimadas=0, criticas=0, atencao=0, positivas=0,
                                                      revisar=0, promotores=0, detratores=0))
    with engine.connect() as con:
        for dia, enviado in con.execute(select(envios.c.data_visita, envios.c.enviado_em).where(and_(
                envios.c.data_visita >= de, envios.c.data_visita <= ate))):
            if enviado is not None:
                meses[f"{dia.year}-{dia.month:02d}"]["enviadas"] += 1
        q = select(envios.c.data_visita, respostas.c.nota, respostas.c.nota_estimada, respostas.c.gravidade,
                   respostas.c.status).select_from(respostas.join(envios, respostas.c.envio_id == envios.c.id)) \
            .where(and_(envios.c.data_visita >= de, envios.c.data_visita <= ate))
        for dia, nota, estimada, grav, status in con.execute(q):
            m = meses[f"{dia.year}-{dia.month:02d}"]
            m["respostas"] += 1
            if nota is not None:
                m["soma_notas"] += nota
                m["com_nota"] += 1
                m["notas_estimadas"] += 1 if estimada else 0
            m["criticas"] += grav == CRITICO
            m["atencao"] += grav == ATENCAO
            m["positivas"] += grav == POSITIVO
            m["revisar"] += status == REVISAR
            if nota is not None:
                m["promotores"] += nota >= 9
                m["detratores"] += nota <= 6
    saida = []
    for chave in sorted(meses):
        m = meses[chave]
        soma, n = m.pop("soma_notas"), m.pop("com_nota")
        prom, det = m.pop("promotores"), m.pop("detratores")
        extra = dict(nps=round(100 * (prom - det) / n), promotores=prom, detratores=det) if com_nps and n else {}
        saida.append(dict(mes=chave, **m, **extra, com_nota=n, nota_media=round(soma / n, 2) if n else None,
                          taxa_resposta=round(m["respostas"] / m["enviadas"], 4) if m["enviadas"] else None))
    return saida


def _sp(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ)


def google(engine, de: date, ate: date) -> dict:
    """Avaliações do Google: média e estrelas de todas, por mês (dentro de de..ate), situação e as últimas."""
    with engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(select(
            avaliacoes_google.c.id, avaliacoes_google.c.estrelas, avaliacoes_google.c.comentario,
            avaliacoes_google.c.criada_em, avaliacoes_google.c.resposta_google, avaliacoes_google.c.situacao,
            avaliacoes_google.c.publicada_em, avaliacoes_google.c.publicada_por))]
    linhas.sort(key=lambda r: _sp(r["criada_em"]) or datetime.min.replace(tzinfo=TZ), reverse=True)
    estrelas = {str(i): 0 for i in range(1, 6)}
    situacoes: dict[str, int] = defaultdict(int)
    meses: dict[str, dict] = defaultdict(lambda: dict(avaliacoes=0, soma=0, com_estrelas=0, respondidas=0))
    soma = com = respondidas = 0
    for r in linhas:
        situacoes[r["situacao"]] += 1
        e = r["estrelas"] or 0
        respondida = bool((r["resposta_google"] or "").strip())
        respondidas += respondida
        if 1 <= e <= 5:
            estrelas[str(e)] += 1
            soma, com = soma + e, com + 1
        quando = _sp(r["criada_em"])
        if quando and de <= quando.date() <= ate:
            m = meses[f"{quando.year}-{quando.month:02d}"]
            m["avaliacoes"] += 1
            m["respondidas"] += respondida
            if 1 <= e <= 5:
                m["soma"] += e
                m["com_estrelas"] += 1
    por_mes = []
    for chave in sorted(meses):
        m = meses[chave]
        s, n = m.pop("soma"), m.pop("com_estrelas")
        por_mes.append(dict(mes=chave, **m, media=round(s / n, 2) if n else None))
    ultimas = []
    for r in linhas[:ULTIMAS_GOOGLE]:
        quando = _sp(r["criada_em"])
        ultimas.append(dict(id=r["id"], estrelas=r["estrelas"] or 0, comentario=r["comentario"] or "",
                            data=quando.date().isoformat() if quando else None, situacao=r["situacao"],
                            resposta=r["resposta_google"] or "",
                            automatica=r["situacao"] == G.PUBLICADA and r["publicada_por"] == "automático"))
    return dict(total=len(linhas), media=round(soma / com, 2) if com else None, estrelas=estrelas,
                respondidas=respondidas, situacoes=dict(situacoes),
                para_revisar=sum(situacoes.get(s, 0) for s in G.ABERTAS), meses=por_mes, ultimas=ultimas)


def _autorizar() -> None:
    token = os.environ.get("PAINEL_API_TOKEN", "")
    if len(token) < 32:
        abort(404)
    enviado = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(enviado.encode(), token.encode()):
        abort(401)


def _periodo(hoje: Callable[[], date]) -> tuple[date, date]:
    try:
        ate = date.fromisoformat(request.args["ate"]) if request.args.get("ate") else hoje()
        de = date.fromisoformat(request.args["de"]) if request.args.get("de") else \
            (ate.replace(day=1) - timedelta(days=150)).replace(day=1)
    except ValueError:
        abort(400)
    if de > ate or (ate - de).days > MAX_DIAS:
        abort(400)
    return de, ate


def registrar(app, *, engine, hoje: Callable[[], date]) -> None:
    @app.get("/api/resumo")
    def api_resumo():
        _autorizar()
        de, ate = _periodo(hoje)
        return jsonify(dict(de=de.isoformat(), ate=ate.isoformat(), meses=resumo(engine, de, ate),
                            fonte="Pós-venda Bar São Paulo (pesquisa por WhatsApp)"))

    @app.get("/api/reputacao")
    def api_reputacao():
        _autorizar()
        de, ate = _periodo(hoje)
        with engine.connect() as con:
            em_revisao = con.execute(select(func.count()).select_from(respostas)
                                     .where(respostas.c.status == REVISAR)).scalar() or 0
        return jsonify(dict(de=de.isoformat(), ate=ate.isoformat(),
                            whatsapp=dict(meses=resumo(engine, de, ate, com_nps=True), em_revisao=em_revisao),
                            google=google(engine, de, ate),
                            fonte="Pós-venda Bar São Paulo (pesquisa por WhatsApp e avaliações do Google)"))
