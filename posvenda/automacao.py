"""Rotina automática do pós-venda (roda numa thread do próprio Painel, como no barsp-gestao).

A cada minuto:
- No horário do envio (ENVIO_HORA, padrão 11h), busca a lista de ontem no Takeat e manda a pesquisa pelo
  WhatsApp com os modelos aprovados na Meta (só com ENVIO_AUTOMATICO=1). Uma vez por dia (trava em `rotinas`).
- Analisa as respostas que chegaram (Claude), avisa as críticas 🔴 por e-mail e, com RESPOSTA_AUTOMATICA=1,
  responde sozinho só as que não precisam de revisão.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import lista, respostas, servico
from .db import envios, rotinas
from .whatsapp import WhatsAppErro

log = logging.getLogger(__name__)

ENVIO_HORA_PADRAO = 11
MODELOS_PADRAO = {"A": "pesquisa_a", "B": "pesquisa_b", "C": "pesquisa_c"}


def parametros_do_modelo(envio: dict[str, Any]) -> list[str]:
    """{{1}} = primeiro nome (ou "tudo bem", que fecha "Oi, tudo bem!") e {{2}} = o prato ("o burguer BSP")."""
    return [envio.get("nome") or "tudo bem", lista.frase_do_prato(envio.get("prato"))]


@dataclass
class ResultadoEnvio:
    enviados: int = 0
    falhas: int = 0
    pulados: int = 0
    erros: list[str] = field(default_factory=list)

    @property
    def resumo(self) -> str:
        texto = f"{self.enviados} enviadas"
        if self.falhas:
            texto += f", {self.falhas} com erro"
        if self.pulados:
            texto += f", {self.pulados} puladas (pediram para não receber)"
        return texto


def enviar_dia(engine: Engine, dia: date, enviar_modelo: Callable[[str, str, list[str]], str],
               modelos: dict[str, str] | None = None) -> ResultadoEnvio:
    """Manda a pesquisa para quem está na lista do dia e ainda não recebeu."""
    modelos = modelos or MODELOS_PADRAO
    res = ResultadoEnvio()
    bloqueados = servico.telefones_bloqueados(engine)
    for e in servico.obter_lista(engine, dia).envios:
        if e["enviado_em"] is not None:
            continue
        if e["telefone"] in bloqueados:  # pediu para parar depois que a lista foi montada
            res.pulados += 1
            continue
        try:
            wa_id = enviar_modelo(e["telefone"], modelos[e["versao"]], parametros_do_modelo(e))
        except WhatsAppErro as exc:
            res.falhas += 1
            res.erros.append(str(exc))
            with engine.begin() as con:
                con.execute(update(envios).where(envios.c.id == e["id"]).values(
                    wa_status="failed", erro=str(exc)[:300]))
            continue
        with engine.begin() as con:
            con.execute(update(envios).where(envios.c.id == e["id"]).values(
                enviado_em=servico.agora(), wa_message_id=wa_id or None, wa_status="sent", erro=None))
        res.enviados += 1
    return res


def travar(engine: Engine, chave: str) -> bool:
    """Grava a trava da rotina. False se outra execução já gravou (não roda de novo)."""
    try:
        with engine.begin() as con:
            con.execute(insert(rotinas).values(chave=chave, rodou_em=servico.agora(), resultado=""))
        return True
    except IntegrityError:
        return False


def registrar(engine: Engine, chave: str, resultado: str) -> None:
    with engine.begin() as con:
        con.execute(update(rotinas).where(rotinas.c.chave == chave).values(resultado=resultado[:300]))


def ultima_rotina(engine: Engine, prefixo: str) -> dict[str, Any] | None:
    with engine.connect() as con:
        r = con.execute(select(rotinas).where(rotinas.c.chave.like(f"{prefixo}%"))
                        .order_by(rotinas.c.rodou_em.desc())).first()
    return dict(r._mapping) if r else None


def envio_do_dia(engine: Engine, agora_local: datetime, buscar, enviar_modelo, *, hora: int = ENVIO_HORA_PADRAO,
                 modelos: dict[str, str] | None = None) -> ResultadoEnvio | None:
    """Envio automático de ontem, a partir de `hora` (horário de São Paulo). Roda uma vez por dia."""
    if agora_local.hour < hora:
        return None
    hoje = agora_local.date()
    chave = f"envio:{hoje.isoformat()}"
    if not travar(engine, chave):
        return None
    dia = hoje - timedelta(days=1)
    try:
        servico.gerar_lista(engine, dia, buscar, hoje=hoje)
        res = enviar_dia(engine, dia, enviar_modelo, modelos)
    except Exception as exc:
        log.exception("Envio automático falhou")
        registrar(engine, chave, f"erro: {exc}")
        raise
    registrar(engine, chave, res.resumo)
    log.info("Envio automático de %s: %s", dia, res.resumo)
    return res


# --------------------------------------------------------------------------- aviso das críticas
def alerta_por_email(destino: str, chave_resend: str, remetente: str, painel_url: str = "") -> Callable[[dict], None]:
    """Avisa por e-mail (Resend) que chegou uma crítica 🔴. Sem telefone completo e sem o texto do cliente."""
    def alertar(res: dict[str, Any]) -> None:
        e = res["envio"]
        nome = e.get("nome") or "Cliente sem nome"
        link = f"{painel_url.rstrip('/')}/respostas?data={e['data_visita']}" if painel_url else ""
        corpo = (f"<p>Chegou uma resposta crítica 🔴 na pesquisa de pós-venda.</p>"
                 f"<p><b>{nome}</b> ({e.get('origem')}, {lista.mascarar(e['telefone'])}) · nota {res.get('nota')}<br>"
                 f"{res.get('resumo', '')}</p><p>Ação sugerida: {res.get('acao_sugerida', '')}</p>"
                 + (f'<p><a href="{link}">Abrir no painel</a></p>' if link else "")
                 + "<p>Ela não foi respondida automaticamente. Revise antes de enviar.</p>")
        r = requests.post("https://api.resend.com/emails", timeout=(10, 30),
                          headers={"Authorization": f"Bearer {chave_resend}"},
                          json={"from": remetente, "to": [x.strip() for x in destino.split(",") if x.strip()],
                                "subject": f"Pós-venda: crítica de {nome}", "html": corpo})
        if r.status_code >= 400:
            raise RuntimeError(f"Resend recusou o e-mail (HTTP {r.status_code}).")
    return alertar


# --------------------------------------------------------------------------- agendador
@dataclass
class Config:
    envio_automatico: bool = False
    resposta_automatica: bool = False
    hora: int = ENVIO_HORA_PADRAO
    modelos: dict[str, str] = field(default_factory=lambda: dict(MODELOS_PADRAO))

    @classmethod
    def do_ambiente(cls) -> "Config":
        try:
            hora = int(os.environ.get("ENVIO_HORA", ENVIO_HORA_PADRAO))
        except ValueError:
            hora = ENVIO_HORA_PADRAO
        return cls(
            envio_automatico=os.environ.get("ENVIO_AUTOMATICO") == "1",
            resposta_automatica=os.environ.get("RESPOSTA_AUTOMATICA") == "1",
            hora=hora,
            modelos={v: os.environ.get(f"WHATSAPP_MODELO_{v}", padrao) for v, padrao in MODELOS_PADRAO.items()},
        )


def rodar_uma_vez(engine: Engine, cfg: Config, *, buscar=None, whatsapp=None, classificar=None, alertar=None,
                  agora: datetime | None = None) -> None:
    agora = agora or servico.agora()
    if cfg.envio_automatico and buscar is not None and whatsapp is not None:
        try:
            envio_do_dia(engine, agora.astimezone(lista.TZ), buscar, whatsapp.enviar_modelo, hora=cfg.hora,
                         modelos=cfg.modelos)
        except Exception:
            pass  # já registrado; tenta de novo só amanhã para não mandar duas vezes
    respostas.processar_pendentes(engine, classificar, agora=agora,
                                  enviar_texto=whatsapp.enviar_texto if whatsapp else None,
                                  resposta_automatica=cfg.resposta_automatica, alertar=alertar)


def iniciar_agendador(engine: Engine, cfg: Config, **deps) -> threading.Thread:
    def laco() -> None:
        while True:
            try:
                rodar_uma_vez(engine, cfg, **deps)
            except Exception:
                log.exception("Rotina automática falhou")
            time.sleep(60)

    t = threading.Thread(target=laco, name="posvenda-agendador", daemon=True)
    t.start()
    log.info("Agendador do pós-venda ligado (envio automático: %s, resposta automática: %s, %sh).",
             cfg.envio_automatico, cfg.resposta_automatica, cfg.hora)
    return t
