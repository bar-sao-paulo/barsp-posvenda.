"""Resumo do pós-venda, só de consulta e sem dados de cliente, para o painel financeiro do Bar São Paulo.

GET /api/resumo?de=AAAA-MM-DD&ate=AAAA-MM-DD
  Cabeçalho: Authorization: Bearer <PAINEL_API_TOKEN>
  Devolve, por mês da visita: pesquisas enviadas, respostas, nota média, quantas de cada gravidade e quantas
  estão em "Revisar antes de enviar". Só números: nenhum nome, telefone, texto ou resumo sai daqui.

Sem a variável PAINEL_API_TOKEN (mínimo 32 caracteres) o endereço não existe (404). O token só é comparado.
"""
from __future__ import annotations

import hmac
import os
from collections import defaultdict
from datetime import date, timedelta
from typing import Callable

from flask import abort, jsonify, request
from sqlalchemy import and_, select

from .db import envios, respostas
from .respostas import ATENCAO, CRITICO, POSITIVO, REVISAR

MAX_DIAS = 400


def resumo(engine, de: date, ate: date) -> list[dict]:
    meses: dict[str, dict] = defaultdict(lambda: dict(enviadas=0, respostas=0, soma_notas=0, com_nota=0,
                                                      notas_estimadas=0, criticas=0, atencao=0, positivas=0,
                                                      revisar=0))
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
    saida = []
    for chave in sorted(meses):
        m = meses[chave]
        soma, n = m.pop("soma_notas"), m.pop("com_nota")
        saida.append(dict(mes=chave, **m, com_nota=n, nota_media=round(soma / n, 2) if n else None,
                          taxa_resposta=round(m["respostas"] / m["enviadas"], 4) if m["enviadas"] else None))
    return saida


def registrar(app, *, engine, hoje: Callable[[], date]) -> None:
    @app.get("/api/resumo")
    def api_resumo():
        token = os.environ.get("PAINEL_API_TOKEN", "")
        if len(token) < 32:
            abort(404)
        enviado = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(enviado.encode(), token.encode()):
            abort(401)
        try:
            ate = date.fromisoformat(request.args["ate"]) if request.args.get("ate") else hoje()
            de = date.fromisoformat(request.args["de"]) if request.args.get("de") else \
                (ate.replace(day=1) - timedelta(days=150)).replace(day=1)
        except ValueError:
            abort(400)
        if de > ate or (ate - de).days > MAX_DIAS:
            abort(400)
        return jsonify(dict(de=de.isoformat(), ate=ate.isoformat(), meses=resumo(engine, de, ate),
                            fonte="Pós-venda Bar São Paulo (pesquisa por WhatsApp)"))
