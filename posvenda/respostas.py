"""Modo 2 (Relatório) automático: respostas que chegam pelo WhatsApp viram a planilha do dia.

Regras (instruções do projeto Pós-venda):
- Cada resposta é ligada ao cliente pelo telefone; número que não está na lista fica em "Fora da lista".
- Gravidade: 🔴 Crítico = nota 0 a 5 | 🟡 Atenção = 6 a 8 | 🟢 Positivo = 9 e 10.
- Resposta curta ou ambígua: nota estimada 8 (🟡).
- 🔴 nunca é respondida de forma automática: sai como "Revisar antes de enviar".
- Nunca prometer compensação, nem usar o vocabulário proibido; no máximo 2 emojis.
- Ordem da planilha: menor nota primeiro; no empate, nota real antes da estimada e, depois, quem tem mais visitas.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import lista
from .db import envios, mensagens, respostas
from .ia import Analise, IAErro

log = logging.getLogger(__name__)

AGUARDANDO = "Aguardando análise"
REVISAR = "Revisar antes de enviar"
PODE_ENVIAR = "Pode enviar"
RESPONDIDO = "Respondido"
IGNORADO = "Ignorado"

CRITICO, ATENCAO, POSITIVO = "🔴 Crítico", "🟡 Atenção", "🟢 Positivo"

JANELA_RESPOSTA = timedelta(hours=24)   # regra do WhatsApp para texto livre
DIAS_PARA_LIGAR = 7                     # resposta liga ao envio feito nos últimos 7 dias
ESPERA_PADRAO = timedelta(minutes=10)   # espera o cliente terminar de escrever antes de analisar

PALAVRAS_PARAR = {"parar", "pare", "sair", "stop", "cancelar", "descadastrar", "nao quero receber"}
CONFIRMACAO_PARAR = "Pronto, você não vai mais receber nossas pesquisas. Obrigada pelo aviso!"

_PROIBIDAS = ("prezado cliente", "lamentamos o transtorno", "infelizmente", "conforme", "informamos que")
_COMPENSACAO = re.compile(r"\b(desconto|cortesia|brinde|reembols\w*|estorn\w*|gr[aá]tis|de gra[cç]a|"
                          r"devolu[cç][aã]o|devolver (o|seu) dinheiro|voucher|cupom)\b", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_AMBIGUAS = {"ok", "okay", "blz", "beleza", "de boa", "td bem", "tudo bem", "show", "top", "certo", "ta", "sim"}

Classificar = Callable[[dict[str, Any], list[str], str], Analise]


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def chave_telefone(telefone: str) -> str:
    """DDD + 8 últimos dígitos. O WhatsApp às vezes manda celular brasileiro sem o 9 (55 11 8765-4321)."""
    d = re.sub(r"\D", "", telefone or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    return d[:2] + d[-8:] if len(d) in (10, 11) else d


def gravidade(nota: int | None) -> str:
    if nota is None:
        return ""
    return CRITICO if nota <= 5 else ATENCAO if nota <= 8 else POSITIVO


def eh_ambigua(textos: list[str]) -> bool:
    junto = " ".join(textos).strip()
    sem_emoji = _EMOJI.sub("", junto)
    limpo = re.sub(r"[^\w\s]", "", lista.sem_acento(sem_emoji)).strip()
    return not limpo or limpo in _AMBIGUAS


def eh_pedido_para_parar(texto: str) -> bool:
    limpo = re.sub(r"[^\w\s]", "", lista.sem_acento(texto or "")).strip()
    return limpo in PALAVRAS_PARAR


def problemas_na_resposta(texto: str) -> list[str]:
    """Motivos para a resposta sugerida não sair sozinha."""
    motivos = []
    baixo = lista.sem_acento(texto or "")
    if not baixo.strip():
        motivos.append("resposta vazia")
    if any(lista.sem_acento(p) in baixo for p in _PROIBIDAS):
        motivos.append("usa vocabulário proibido")
    if _COMPENSACAO.search(texto or ""):
        motivos.append("fala em compensação")
    if len(_EMOJI.findall(texto or "")) > 2:
        motivos.append("mais de 2 emojis")
    return motivos


# --------------------------------------------------------------------------- mensagens recebidas
def envio_do_telefone(engine: Engine, telefone: str, quando: datetime) -> dict[str, Any] | None:
    """Envio mais recente para esse telefone nos últimos 7 dias (ligação só pelo telefone)."""
    chave = chave_telefone(telefone)
    with engine.connect() as con:
        rows = con.execute(select(envios).where(and_(
            envios.c.enviado_em.is_not(None),
            envios.c.enviado_em >= quando - timedelta(days=DIAS_PARA_LIGAR),
        )).order_by(envios.c.enviado_em.desc()))
        for r in rows:
            if chave_telefone(r.telefone) == chave:
                return dict(r._mapping)
    return None


@dataclass
class Resultado:
    acao: str  # "duplicada", "fora_da_lista", "parar", "resposta"
    envio_id: int | None = None


def registrar_recebida(engine: Engine, telefone: str, texto: str, wa_id: str, quando: datetime,
                       enviar_texto: Callable[[str, str], str] | None = None,
                       bloquear: Callable[[str], Any] | None = None) -> Resultado:
    envio = envio_do_telefone(engine, telefone, quando)
    envio_id = envio["id"] if envio else None
    try:
        with engine.begin() as con:
            con.execute(insert(mensagens).values(telefone=telefone, envio_id=envio_id, direcao="entrada",
                                                 texto=texto, wa_id=wa_id or None, criado_em=quando))
    except IntegrityError:
        return Resultado("duplicada", envio_id)  # o WhatsApp pode reenviar o mesmo evento

    if eh_pedido_para_parar(texto):
        if bloquear:
            bloquear(envio["telefone"] if envio else telefone)
        if enviar_texto:
            try:
                wa = enviar_texto(telefone, CONFIRMACAO_PARAR)
                _gravar_saida(engine, telefone, envio_id, CONFIRMACAO_PARAR, wa)
            except Exception:
                log.exception("Falha ao confirmar o PARAR")
        if envio:
            _salvar_resposta(engine, envio_id, quando, status=IGNORADO, resumo="Pediu para não receber mais a pesquisa",
                             motivo="Cliente respondeu PARAR")
        return Resultado("parar", envio_id)

    if not envio:
        return Resultado("fora_da_lista")
    _salvar_resposta(engine, envio_id, quando, status=AGUARDANDO)
    return Resultado("resposta", envio_id)


def _salvar_resposta(engine: Engine, envio_id: int, quando: datetime, *, status: str, resumo: str = "",
                     motivo: str = "") -> None:
    with engine.begin() as con:
        atual = con.execute(select(respostas).where(respostas.c.envio_id == envio_id)).first()
        if atual is None:
            con.execute(insert(respostas).values(envio_id=envio_id, status=status, resumo=resumo,
                                                 motivo_revisao=motivo, ultima_mensagem_em=quando))
        elif atual.status != IGNORADO or status == IGNORADO:
            con.execute(update(respostas).where(respostas.c.id == atual.id).values(
                status=status, ultima_mensagem_em=quando,
                **({"resumo": resumo, "motivo_revisao": motivo} if resumo or motivo else {})))


def _gravar_saida(engine: Engine, telefone: str, envio_id: int | None, texto: str, wa_id: str) -> None:
    with engine.begin() as con:
        con.execute(insert(mensagens).values(telefone=telefone, envio_id=envio_id, direcao="saida", texto=texto,
                                             wa_id=wa_id or None, criado_em=agora_utc()))


def atualizar_situacao(engine: Engine, wa_id: str, status: str, erro: str = "") -> None:
    """Situação do envio da pesquisa (sent/delivered/read/failed) vinda do webhook."""
    if not wa_id:
        return
    valores: dict[str, Any] = {"wa_status": status[:20]}
    if status == "failed":
        valores["erro"] = (erro or "O WhatsApp não entregou a mensagem.")[:300]
    with engine.begin() as con:
        con.execute(update(envios).where(envios.c.wa_message_id == wa_id).values(**valores))


# --------------------------------------------------------------------------- análise e resposta
def historico_cliente(engine: Engine, envio: dict[str, Any]) -> tuple[str, int]:
    """Visitas registradas no pós-venda para o mesmo telefone (o Takeat não dá o histórico ainda)."""
    chave = chave_telefone(envio["telefone"])
    with engine.connect() as con:
        datas = sorted({r.data_visita for r in con.execute(
            select(envios.c.telefone, envios.c.data_visita).where(envios.c.enviado_em.is_not(None)))
            if chave_telefone(r.telefone) == chave})
    if len(datas) <= 1:
        return "sem histórico", len(datas)
    return f"{len(datas)} visitas desde {datas[0]:%d/%m/%Y}", len(datas)


def textos_do_cliente(engine: Engine, envio_id: int) -> list[str]:
    with engine.connect() as con:
        return [r.texto for r in con.execute(select(mensagens.c.texto).where(and_(
            mensagens.c.envio_id == envio_id, mensagens.c.direcao == "entrada")).order_by(mensagens.c.criado_em))]


def analisar(engine: Engine, resposta_id: int, classificar: Classificar) -> dict[str, Any]:
    with engine.connect() as con:
        r = con.execute(select(respostas).where(respostas.c.id == resposta_id)).first()
        envio = dict(con.execute(select(envios).where(envios.c.id == r.envio_id)).first()._mapping)
    textos = textos_do_cliente(engine, envio["id"])
    historico, _ = historico_cliente(engine, envio)
    motivos: list[str] = []
    if r.respondida_em is not None:
        motivos.append("cliente escreveu de novo depois da nossa resposta")
    try:
        a = classificar(envio, textos, historico)
    except IAErro as exc:
        valores = dict(status=REVISAR, motivo_revisao=f"A IA não conseguiu analisar: {exc}", historico=historico,
                       classificada_em=agora_utc())
    else:
        nota = a.nota
        if a.nota_estimada and eh_ambigua(textos):
            nota = 8
        g = gravidade(nota)
        resumo = a.resumo.replace(";", ",")
        acao = a.acao_sugerida.replace(";", ",")
        if a.ressalva:
            if a.ressalva.lower() not in resumo.lower():
                resumo = f"{resumo} (ressalva: {a.ressalva})"
            if a.ressalva.lower() not in acao.lower():
                acao = f"{acao} Ressalva do cliente: {a.ressalva}."
        motivos += problemas_na_resposta(a.resposta_sugerida)
        if a.pediu_compensacao:
            motivos.append("cliente pediu compensação")
        if g == CRITICO:
            motivos.insert(0, "crítica 🔴")
        valores = dict(nota=nota, nota_estimada=a.nota_estimada, gravidade=g, resumo=resumo[:300], historico=historico,
                       resposta_sugerida=a.resposta_sugerida.replace(";", ","), acao_sugerida=acao,
                       status=REVISAR if motivos else PODE_ENVIAR, motivo_revisao=", ".join(motivos)[:300],
                       classificada_em=agora_utc())
    with engine.begin() as con:
        con.execute(update(respostas).where(respostas.c.id == resposta_id).values(**valores))
    return {**valores, "id": resposta_id, "envio": envio}


def dentro_da_janela(engine: Engine, envio_id: int, agora: datetime) -> bool:
    with engine.connect() as con:
        ultima = con.execute(select(mensagens.c.criado_em).where(and_(
            mensagens.c.envio_id == envio_id, mensagens.c.direcao == "entrada")).order_by(
            mensagens.c.criado_em.desc())).scalar()
    return ultima is not None and agora - _utc(ultima) < JANELA_RESPOSTA


class RespostaErro(RuntimeError):
    pass


def enviar_resposta(engine: Engine, resposta_id: int, texto: str, enviar_texto: Callable[[str, str], str],
                    agora: datetime | None = None) -> None:
    agora = agora or agora_utc()
    texto = (texto or "").strip()
    if not texto:
        raise RespostaErro("A resposta está vazia.")
    with engine.connect() as con:
        r = con.execute(select(respostas, envios.c.telefone).join(envios, envios.c.id == respostas.c.envio_id)
                        .where(respostas.c.id == resposta_id)).first()
    if r is None:
        raise RespostaErro("Resposta não encontrada.")
    if not dentro_da_janela(engine, r.envio_id, agora):
        raise RespostaErro("Já passaram 24 horas desde a última mensagem do cliente. O WhatsApp só deixa "
                           "mandar texto livre dentro desse prazo.")
    wa = enviar_texto(r.telefone, texto)
    _gravar_saida(engine, r.telefone, r.envio_id, texto, wa)
    with engine.begin() as con:
        con.execute(update(respostas).where(respostas.c.id == resposta_id).values(
            status=RESPONDIDO, respondida_em=agora, resposta_sugerida=texto))


def ignorar(engine: Engine, resposta_id: int) -> None:
    with engine.begin() as con:
        con.execute(update(respostas).where(respostas.c.id == resposta_id).values(status=IGNORADO))


def processar_pendentes(engine: Engine, classificar: Classificar | None, *, agora: datetime | None = None,
                        enviar_texto: Callable[[str, str], str] | None = None, resposta_automatica: bool = False,
                        alertar: Callable[[dict[str, Any]], None] | None = None,
                        espera: timedelta = ESPERA_PADRAO) -> int:
    """Analisa as respostas paradas há `espera` (o cliente terminou de escrever). Responde sozinho só 🟢/🟡
    sem nenhum motivo de revisão, e só com resposta_automatica ligada. Devolve quantas analisou."""
    if classificar is None:
        return 0
    agora = agora or agora_utc()
    with engine.connect() as con:
        ids = [r.id for r in con.execute(select(respostas.c.id, respostas.c.ultima_mensagem_em).where(
            respostas.c.status == AGUARDANDO)) if _utc(r.ultima_mensagem_em) <= agora - espera]
    for rid in ids:
        res = analisar(engine, rid, classificar)
        if res["status"] == REVISAR and res.get("gravidade") == CRITICO and alertar:
            try:
                alertar(res)
                with engine.begin() as con:
                    con.execute(update(respostas).where(respostas.c.id == rid).values(alerta_em=agora))
            except Exception:
                log.exception("Falha ao avisar a crítica")
        if res["status"] == PODE_ENVIAR and resposta_automatica and enviar_texto:
            try:
                enviar_resposta(engine, rid, res["resposta_sugerida"], enviar_texto, agora)
            except Exception as exc:
                log.warning("Resposta automática não saiu: %s", exc)
                with engine.begin() as con:
                    con.execute(update(respostas).where(respostas.c.id == rid).values(
                        status=REVISAR, motivo_revisao=f"envio automático falhou: {exc}"[:300]))
    return len(ids)


# --------------------------------------------------------------------------- planilha
COLUNAS = ["Data", "Cliente", "Telefone", "Origem", "Prato/pedido", "Versão", "Nota", "Nota estimada", "Gravidade",
           "Resumo", "Histórico", "Resposta sugerida", "Ação sugerida", "Status"]


def listar(engine: Engine, de: date, ate: date) -> list[dict[str, Any]]:
    """Respostas das visitas entre `de` e `ate`, da mais crítica para a menos crítica."""
    with engine.connect() as con:
        rows = [dict(r._mapping) for r in con.execute(
            select(respostas, envios.c.data_visita, envios.c.nome_completo, envios.c.nome, envios.c.telefone,
                   envios.c.origem, envios.c.prato, envios.c.versao)
            .join(envios, envios.c.id == respostas.c.envio_id)
            .where(and_(envios.c.data_visita >= de, envios.c.data_visita <= ate)))]
    for r in rows:
        r["visitas"] = historico_cliente(engine, r)[1]
        r["textos"] = textos_do_cliente(engine, r["envio_id"])
    rows.sort(key=lambda r: (r["nota"] if r["nota"] is not None else -1, bool(r["nota_estimada"]), -r["visitas"]))
    return rows


def fora_da_lista(engine: Engine, de: datetime, ate: datetime) -> list[dict[str, Any]]:
    with engine.connect() as con:
        return [dict(r._mapping) for r in con.execute(select(mensagens).where(and_(
            mensagens.c.envio_id.is_(None), mensagens.c.direcao == "entrada",
            mensagens.c.criado_em >= de, mensagens.c.criado_em < ate)).order_by(mensagens.c.criado_em))]


def contagem(rows: list[dict[str, Any]]) -> str:
    c = {CRITICO: 0, ATENCAO: 0, POSITIVO: 0}
    for r in rows:
        if r["gravidade"] in c:
            c[r["gravidade"]] += 1
    return f"🔴 {c[CRITICO]} críticos | 🟡 {c[ATENCAO]} atenção | 🟢 {c[POSITIVO]} positivos"


def _campo(v: Any) -> str:
    return str(v if v is not None else "").replace(";", ",").replace("\n", " ").strip()


def planilha_csv(rows: list[dict[str, Any]]) -> str:
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
    w.writerow(COLUNAS)
    for r in rows:
        w.writerow([_campo(x) for x in (
            f"{r['data_visita']:%d/%m/%Y}", r["nome_completo"] or r["nome"] or "", r["telefone"], r["origem"],
            r["prato"] or "seu pedido", r["versao"] or "—", r["nota"] if r["nota"] is not None else "",
            ("sim" if r["nota_estimada"] else "não") if r["nota"] is not None else "", r["gravidade"] or "",
            r["resumo"], r["historico"] or "sem histórico", r["resposta_sugerida"], r["acao_sugerida"], r["status"],
        )])
    return saida.getvalue()
