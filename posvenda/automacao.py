"""Rotina automática do pós-venda (roda numa thread do próprio Painel, como no barsp-gestao).

A cada minuto:
- No horário do envio (ENVIO_HORA, padrão 11h), busca a lista de ontem no Takeat e manda a pesquisa pelo
  WhatsApp com os modelos aprovados na Meta (só com ENVIO_AUTOMATICO=1). Uma vez por dia (trava em `rotinas`).
- Analisa as respostas que chegaram (Claude), avisa as críticas 🔴 por e-mail e, com RESPOSTA_AUTOMATICA=1,
  responde sozinho só as que não precisam de revisão.
- Transcreve os áudios dos clientes (com OPENAI_API_KEY), antes da análise.
- Lê aos poucos o histórico de comandas dos últimos 12 meses (número de compras de cada cliente).
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

from . import historico, lista, respostas, servico
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


# --------------------------------------------------------------------------- transcrição dos áudios
def transcrever_audios(engine: Engine, baixar: Callable[[str], tuple[bytes, str]],
                       transcrever: Callable[[bytes, str], str], agora: datetime, *, limite: int = 3) -> int:
    """Transcreve os áudios novos (últimos 7 dias). Cada áudio é tentado uma vez (trava em `rotinas`); se falhar,
    continua aparecendo para a equipe ouvir e escrever. Devolve quantos foram transcritos."""
    feitos = 0
    with engine.connect() as con:
        tentados = {r[0] for r in con.execute(select(rotinas.c.chave).where(rotinas.c.chave.like("transcrever:%")))}
    for a in respostas.audios_para_transcrever(engine, agora - timedelta(days=7)):
        if feitos >= limite:
            break
        chave = f"transcrever:{a['id']}"
        if chave in tentados or not travar(engine, chave):
            continue
        try:
            conteudo, tipo = baixar(a["midia_id"])
            texto = transcrever(conteudo, tipo)
            if not texto.strip():
                registrar(engine, chave, "áudio sem fala")
                continue
            respostas.escrever_audio(engine, a["resposta_id"], texto, agora=agora, direcao="transcrito")
        except Exception as exc:
            log.warning("Transcrição do áudio %s falhou: %s", a["id"], exc)
            registrar(engine, chave, f"erro: {exc}")
            continue
        registrar(engine, chave, "ok")
        feitos += 1
    return feitos


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
                  transcrever=None, agora: datetime | None = None) -> None:
    agora = agora or servico.agora()
    if cfg.envio_automatico and buscar is not None and whatsapp is not None:
        try:
            envio_do_dia(engine, agora.astimezone(lista.TZ), buscar, whatsapp.enviar_modelo, hora=cfg.hora,
                         modelos=cfg.modelos)
        except Exception:
            pass  # já registrado; tenta de novo só amanhã para não mandar duas vezes
    if classificar is not None and travar(engine, "reavaliar-nota-8:v1"):
        n = respostas.reavaliar_estimadas(engine)  # regra nova (14/10/2026): elogio curto e sugestão = 9
        registrar(engine, "reavaliar-nota-8:v1", f"{n} resposta(s) devolvida(s) para análise")
    if travar(engine, "revisar-audios:v1"):  # áudios analisados antes da trava do áudio (15/10/2026)
        n = respostas.revisar_audios_antigos(engine)
        registrar(engine, "revisar-audios:v1", f"{n} resposta(s) com áudio sem texto foram para revisão")
    if travar(engine, "revisar-visitas:v1"):  # regra de 17/10/2026: nunca citar quantas vezes o cliente veio
        n = respostas.revisar_respostas_com_visitas(engine)
        registrar(engine, "revisar-visitas:v1", f"{n} sugestão(ões) citando visitas foram para revisão")
    if buscar is not None and travar(engine, "reler-pratos:v1"):  # 19/10/2026: prato favorito na tela Clientes
        n = historico.reler_tudo(engine)
        registrar(engine, "reler-pratos:v1", f"{n} bloco(s) do histórico serão relidos para gravar os pratos")
    if buscar is not None and travar(engine, "reler-valores:v1"):  # 22/10/2026: total gasto de quem não é do Clube
        n = historico.reler_tudo(engine)
        registrar(engine, "reler-valores:v1", f"{n} bloco(s) do histórico serão relidos para gravar os valores")
    if buscar is not None and travar(engine, "reler-contatos:v1"):  # 22/10/2026: todos os clientes na tela Clientes
        n = historico.reler_tudo(engine)
        registrar(engine, "reler-contatos:v1", f"{n} bloco(s) do histórico serão relidos para gravar os contatos")
    if transcrever is not None and whatsapp is not None:  # antes da análise, para a IA já ler o que foi dito
        try:
            transcrever_audios(engine, whatsapp.baixar_midia, transcrever, agora)
        except Exception:
            log.exception("Transcrição dos áudios falhou")
    respostas.processar_pendentes(engine, classificar, agora=agora,
                                  enviar_texto=whatsapp.enviar_texto if whatsapp else None,
                                  resposta_automatica=cfg.resposta_automatica, alertar=alertar)
    if buscar is not None:  # histórico de compras: poucos blocos de 3 dias por minuto até fechar 12 meses
        try:
            servico.preencher_itens(engine, buscar, agora.astimezone(lista.TZ).date())  # itens das listas antigas
            historico.carregar(engine, buscar, agora.astimezone(lista.TZ).date(), agora=agora)
        except Exception as exc:
            log.warning("Histórico de compras: falha ao ler o Takeat (%s); tento de novo no próximo minuto.", exc)


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
