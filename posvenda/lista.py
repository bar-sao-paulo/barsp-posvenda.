"""Modo 1 (Envio): da lista de comandas do Takeat às mensagens da pesquisa.

Regras (instruções do projeto Pós-venda):
- Tira da lista: quem recebeu a pesquisa nos últimos 7 dias, quem pediu para não receber, quem está sem
  telefone válido e telefones repetidos (um envio por pessoa). Cada retirada fica listada com o motivo.
- Pedidos do iFood ficam de fora: o iFood não passa o telefone do cliente.
- Versão A, B ou C sorteada de forma equilibrada (nenhuma passa de metade da lista com 3 ou mais clientes).
- [Nome] = primeiro nome; [prato/pedido] = prato principal com artigo; sem prato = "seu pedido";
  sem nome = "Oi! Aqui é a Thais…".

Campos da comanda (md/v1/referencia/pedidos/getTableSessionsV1.md):
sessions[].start_time (UTC), status, is_delivery, sales_channel, table.table_type,
bills[].buyer.{name, phone "(99) 99999-9999"}, bills[].order_baskets[].orders[].{product.name, price, canceled_at}.
"""
from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

VERSOES = {
    "A": "{oi} Aqui é a Thais, do Bar São Paulo :) Como foi sua experiência com {prato} {quando}? "
         "Sua opinião é muito importante para nós, e é rapidinho!",
    "B": "{oi} Aqui é a Thais, do Bar São Paulo :) De 0 a 10, qual nota você dá para sua experiência com "
         "{prato} {quando}? Se quiser contar algum detalhe, fico no aguardo!",
    "C": "{oi} Aqui é a Thais, do Bar São Paulo :) Passando para saber como foi sua experiência com {prato} "
         "{quando}. Tem alguma coisa que a gente poderia fazer ainda melhor? Sua opinião é muito importante para nós!",
}

MOTIVO_IFOOD = "Pedido do iFood (o iFood não passa o telefone)"
MOTIVO_SEM_TELEFONE = "Sem telefone válido"
MOTIVO_BLOQUEIO = "Pediu para não receber"
MOTIVO_7_DIAS = "Recebeu a pesquisa nos últimos 7 dias"
MOTIVO_REPETIDO = "Telefone repetido (fica um envio por pessoa)"

# Itens que não são o "prato" da visita (bebidas, molhos etc.), comparados sem acento e em minúsculas.
_NAO_PRATO = re.compile(
    r"\b(chopp?|chope|suco|sucos|refri|refrigerante|lata|agua|aguas|cerveja|long neck|vinho|taca|dose|"
    r"cafe|expresso|h2o|soda|drink|caipi\w*|jarra|molho|pimenta|gelo|energetico|coca|pepsi|guarana|"
    r"schweppes|tonica|kinder|limonada|cha|whisky|vodka|gin|amarula|caneca|garrafa)\b"
)
_PORCAO = re.compile(r"\b\d+\s*(a\s*\d+\s*)?pessoas?\b|\bexecutiv[oa]\b", re.IGNORECASE)
_NOMES_GENERICOS = {"cliente", "mesa", "balcao", "consumidor", "delivery", "ifood", "teste", "sem", "nao", "comanda"}
_SINGULAR_COM_S = {"gas", "pires", "bis", "atlas"}


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower()


def normalizar_telefone(bruto: str | None) -> str | None:
    """'(11) 98765-4321' -> '5511987654321'. Só celular com DDD (WhatsApp); o resto é inválido."""
    digitos = re.sub(r"\D", "", bruto or "")
    if len(digitos) == 13 and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) != 11 or digitos[0] == "0" or digitos[2] != "9":
        return None
    if len(set(digitos[3:])) == 1:  # 99999-9999, 00000-0000: número de preenchimento
        return None
    return "55" + digitos


def chave_telefone(telefone: str) -> str:
    """DDD + 8 últimos dígitos. O WhatsApp às vezes manda celular brasileiro sem o 9 (55 11 8765-4321)."""
    d = re.sub(r"\D", "", telefone or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    return d[:2] + d[-8:] if len(d) in (10, 11) else d


def formatar_telefone(telefone: str) -> str:
    """'5511987654321' -> '(11) 98765-4321' (só para quem clicou em "Ver telefone")."""
    t = telefone[2:] if telefone.startswith("55") and len(telefone) in (12, 13) else telefone
    return f"({t[:2]}) {t[2:-4]}-{t[-4:]}" if len(t) >= 10 else telefone


def mascarar(telefone: str) -> str:
    """Telefone para a tela: só os 4 últimos dígitos (o completo fica na planilha interna)."""
    t = telefone[2:] if telefone.startswith("55") else telefone
    return f"({t[:2]}) •••••-{t[-4:]}" if len(t) >= 6 else "—"


def primeiro_nome(nome: str | None) -> str | None:
    partes = (nome or "").strip().split()
    if not partes:
        return None
    p = partes[0].strip(".,;:-")
    if len(p) < 2 or any(c.isdigit() for c in p) or sem_acento(p) in _NOMES_GENERICOS:
        return None
    return p[:1].upper() + p[1:].lower()


def limpar_prato(nome: str) -> str:
    """'Parmegiana de Filé Mignon 2 a 3 Pessoas' -> 'Parmegiana de Filé Mignon'."""
    nome = re.sub(r"\bc/\s*", "com ", nome or "", flags=re.IGNORECASE)
    nome = re.sub(r"\bs/\s*", "sem ", nome, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", _PORCAO.sub("", nome)).strip(" -")


def eh_prato(nome: str) -> bool:
    return bool(nome) and not _NAO_PRATO.search(sem_acento(nome))


def artigo(prato: str) -> str:
    """Artigo pela primeira palavra: 'a picanha', 'o risoto', 'as fritas', 'os pastéis'."""
    palavra = sem_acento(prato.split()[0]) if prato.split() else ""
    plural = palavra.endswith("s") and palavra not in _SINGULAR_COM_S
    base = palavra[:-1] if plural else palavra
    if palavra.endswith(("coes", "oes")) and plural:
        base = palavra[:-3] + "ao"
    feminino = base.endswith(("a", "cao", "gem", "dade"))
    if plural:
        return "as" if feminino else "os"
    return "a" if feminino else "o"


def frase_do_prato(prato: str | None) -> str:
    if not prato:
        return "seu pedido"
    # minúsculas, menos siglas curtas ("Burguer BSP" -> "o burguer BSP")
    palavras = [p if p.isupper() and 2 <= len(p) <= 4 else p.lower() for p in prato.split()]
    return f"{artigo(prato)} {' '.join(palavras)}"


def quando(dia: date, hoje: date) -> str:
    return "ontem" if dia == hoje - timedelta(days=1) else f"no dia {dia:%d/%m}"


def montar_mensagem(versao: str, nome: str | None, prato: str | None, dia: date, hoje: date) -> str:
    oi = f"Oi, {nome}!" if nome else "Oi!"
    return VERSOES[versao].format(oi=oi, prato=frase_do_prato(prato), quando=quando(dia, hoje))


def link_whatsapp(telefone: str, mensagem: str) -> str:
    return f"https://wa.me/{telefone}?text={quote(mensagem, safe='')}"


# --------------------------------------------------------------------------- comandas -> clientes
@dataclass
class Candidato:
    nome_completo: str
    nome: str | None
    telefone: str | None
    origem: str  # "Salão" ou "Delivery"
    prato: str | None
    ifood: bool
    itens: str = ""  # tudo o que a pessoa consumiu no dia ("2x Chopp Brahma · 1x Picanha Cheff")
    local: str = ""  # "Mesa 12" no salão, "Pedido #2163" no delivery
    turno: str = ""  # "Almoço" ou "Janta"


def janela_utc(dia: date) -> tuple[datetime, datetime]:
    """Janela da consulta (máx. 3 dias, datas em UTC): o dia local com 12 h de folga de cada lado.
    O filtro fino é feito depois pelo start_time em horário de São Paulo."""
    inicio = datetime.combine(dia, time(0), TZ) - timedelta(hours=12)
    fim = datetime.combine(dia + timedelta(days=1), time(0), TZ) + timedelta(hours=12)
    return inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)


def _data_local(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date()


CORTE_DO_TURNO = time(17, 0)  # mesma regra do caixa diário: abertura antes das 17h = almoço


def turno(iso: str | None) -> str:
    """'Almoço' se a comanda abriu antes das 17h (horário de São Paulo), senão 'Janta'."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return "Almoço" if dt.astimezone(TZ).time() < CORTE_DO_TURNO else "Janta"


def _preco(valor: Any) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def prato_principal(bill: dict[str, Any]) -> str | None:
    """Item de comida mais caro da conta (bebidas e molhos ficam de fora)."""
    melhor, melhor_preco = None, -1.0
    for cesta in bill.get("order_baskets") or []:
        if cesta.get("canceled_at"):
            continue
        for pedido in cesta.get("orders") or []:
            if pedido.get("canceled_at"):
                continue
            nome = limpar_prato(((pedido.get("product") or {}).get("name")) or "")
            if not eh_prato(nome):
                continue
            preco = _preco(pedido.get("price"))
            if preco > melhor_preco:
                melhor, melhor_preco = nome, preco
    return melhor


def _quantidade(valor: Any) -> int:
    try:
        return max(int(valor), 1)
    except (TypeError, ValueError):
        return 1


def itens_da_conta(bill: dict[str, Any]) -> dict[str, int]:
    """Itens não cancelados da conta, somando a quantidade (`amount`) de cada produto, na ordem do pedido."""
    itens: dict[str, int] = {}
    for cesta in bill.get("order_baskets") or []:
        if cesta.get("canceled_at"):
            continue
        for pedido in cesta.get("orders") or []:
            if pedido.get("canceled_at"):
                continue
            nome = re.sub(r"\s+", " ", ((pedido.get("product") or {}).get("name")) or "").strip()
            if nome:
                itens[nome] = itens.get(nome, 0) + _quantidade(pedido.get("amount"))
    return itens


def texto_dos_itens(itens: dict[str, int]) -> str:
    return " · ".join(f"{n}x {nome}" for nome, n in itens.items())


_TIPOS_DE_MESA = {"table": "Mesa", "command": "Comanda", "comanda": "Comanda", "balcony": "Balcão", "balcao": "Balcão"}


def local_da_sessao(s: dict[str, Any], delivery: bool) -> str:
    """'Mesa 12' no salão (table.table_number) e 'Pedido #2163' no delivery (session_number), como no Takeat."""
    if delivery:
        n = s.get("session_number")
        return f"Pedido #{n}" if n else ""
    mesa = s.get("table") or {}
    n = mesa.get("table_number")
    if n in (None, ""):
        return ""
    return f"{_TIPOS_DE_MESA.get(str(mesa.get('table_type') or '').lower(), 'Mesa')} {n}"


def clientes_do_dia(sessoes: Iterable[dict[str, Any]], dia: date) -> list[Candidato]:
    """Um candidato por conta (bill) das comandas abertas no dia (horário de São Paulo)."""
    saida: list[Candidato] = []
    por_telefone: dict[str | None, list[dict[str, int]]] = {}
    for s in sorted(sessoes, key=lambda x: x.get("start_time") or ""):
        if (s.get("status") or "").lower() == "canceled" or s.get("delivery_canceled_at"):
            continue
        if _data_local(s.get("start_time")) != dia:
            continue
        tipo = ((s.get("table") or {}).get("table_type") or "").lower()
        origem = "Delivery" if s.get("is_delivery") or tipo == "delivery" else "Salão"
        ifood = (s.get("sales_channel") or "").upper() == "IFOOD"
        for bill in s.get("bills") or []:
            buyer = bill.get("buyer") or {}
            nome_completo = (buyer.get("name") or "").strip()
            saida.append(Candidato(
                nome_completo=nome_completo,
                nome=primeiro_nome(nome_completo),
                telefone=normalizar_telefone(buyer.get("phone")),
                origem=origem,
                prato=prato_principal(bill),
                ifood=ifood,
                itens=texto_dos_itens(itens_da_conta(bill)),
                local=local_da_sessao(s, origem == "Delivery"),
                turno=turno(s.get("start_time")),
            ))
            por_telefone.setdefault(saida[-1].telefone, []).append(itens_da_conta(bill))
    # Mesmo telefone em várias contas do dia: o cartão mostra tudo o que a pessoa consumiu.
    for c in saida:
        contas = por_telefone.get(c.telefone) if c.telefone else None
        if contas and len(contas) > 1:
            soma: dict[str, int] = {}
            for itens in contas:
                for nome, n in itens.items():
                    soma[nome] = soma.get(nome, 0) + n
            c.itens = texto_dos_itens(soma)
    return saida


def visitas_das_sessoes(sessoes: Iterable[dict[str, Any]], de: date, ate: date) -> set[tuple[str, date]]:
    """(chave do telefone, dia) de cada conta com celular nas comandas abertas entre `de` e `ate`
    (horário de São Paulo). Comanda cancelada fica de fora; várias contas no mesmo dia contam uma vez."""
    saida: set[tuple[str, date]] = set()
    for s in sessoes:
        if (s.get("status") or "").lower() == "canceled" or s.get("delivery_canceled_at"):
            continue
        dia = _data_local(s.get("start_time"))
        if dia is None or not (de <= dia <= ate):
            continue
        for bill in s.get("bills") or []:
            tel = normalizar_telefone((bill.get("buyer") or {}).get("phone"))
            if tel:
                saida.add((chave_telefone(tel), dia))
    return saida


def contatos_das_sessoes(sessoes: Iterable[dict[str, Any]], de: date, ate: date) -> dict[str, tuple[str, str, date]]:
    """chave do telefone -> (telefone, nome, dia) da comanda mais recente com celular entre `de` e `ate`
    (tela Clientes: quem não está no Clube). iFood fica de fora; nome vazio só se nenhuma comanda tiver nome."""
    saida: dict[str, tuple[str, str, date]] = {}
    for s in sorted(sessoes, key=lambda x: x.get("start_time") or ""):
        if (s.get("status") or "").lower() == "canceled" or s.get("delivery_canceled_at"):
            continue
        if (s.get("sales_channel") or "").upper() == "IFOOD":
            continue
        dia = _data_local(s.get("start_time"))
        if dia is None or not (de <= dia <= ate):
            continue
        for bill in s.get("bills") or []:
            buyer = bill.get("buyer") or {}
            tel = normalizar_telefone(buyer.get("phone"))
            if not tel:
                continue
            nome = " ".join((buyer.get("name") or "").split())
            if not primeiro_nome(nome):  # "Mesa 4", "Cliente": não é nome de gente
                nome = ""
            k = chave_telefone(tel)
            saida[k] = (tel, nome or (saida[k][1] if k in saida else ""), dia)
    return saida


def pratos_das_sessoes(sessoes: Iterable[dict[str, Any]], de: date, ate: date) -> dict[tuple[str, date], str]:
    """(chave do telefone, dia) -> prato principal do dia (o mais caro entre as contas do mesmo telefone)."""
    saida: dict[tuple[str, date], tuple[str, float]] = {}
    for s in sessoes:
        if (s.get("status") or "").lower() == "canceled" or s.get("delivery_canceled_at"):
            continue
        dia = _data_local(s.get("start_time"))
        if dia is None or not (de <= dia <= ate):
            continue
        for bill in s.get("bills") or []:
            tel = normalizar_telefone((bill.get("buyer") or {}).get("phone"))
            prato = prato_principal(bill)
            if not tel or not prato:
                continue
            preco = max((_preco(p.get("price")) for c in bill.get("order_baskets") or [] for p in c.get("orders") or []
                         if limpar_prato(((p.get("product") or {}).get("name")) or "") == prato), default=0.0)
            k = (chave_telefone(tel), dia)
            if k not in saida or preco > saida[k][1]:
                saida[k] = (prato, preco)
    return {k: v[0] for k, v in saida.items()}


# --------------------------------------------------------------------------- filtros e versões
@dataclass
class Envio:
    candidato: Candidato
    versao: str


@dataclass
class Retirado:
    candidato: Candidato
    motivo: str


def filtrar(candidatos: list[Candidato], enviados_7_dias: set[str], bloqueados: set[str]) -> tuple[list[Candidato], list[Retirado]]:
    ficam: list[Candidato] = []
    fora: list[Retirado] = []
    vistos: set[str] = set()
    for c in candidatos:
        if c.ifood:
            motivo = MOTIVO_IFOOD
        elif not c.telefone:
            motivo = MOTIVO_SEM_TELEFONE
        elif c.telefone in bloqueados:
            motivo = MOTIVO_BLOQUEIO
        elif c.telefone in enviados_7_dias:
            motivo = MOTIVO_7_DIAS
        elif c.telefone in vistos:
            motivo = MOTIVO_REPETIDO
        else:
            vistos.add(c.telefone)
            ficam.append(c)
            continue
        fora.append(Retirado(c, motivo))
    return ficam, fora


def sortear_versoes(telefones: list[str], ja_definidas: dict[str, str], rng: random.Random | None = None) -> dict[str, str]:
    """Mantém a versão de quem já tinha uma e sorteia as novas pela versão menos usada (empate: sorteio).
    Assim as contagens ficam com diferença de no máximo 1 e nenhuma passa de metade da lista."""
    rng = rng or random.Random()
    resultado = {t: ja_definidas[t] for t in telefones if t in ja_definidas}
    contagem = {v: 0 for v in VERSOES}
    for v in resultado.values():
        contagem[v] += 1
    novos = [t for t in telefones if t not in resultado]
    rng.shuffle(novos)
    for t in novos:
        menor = min(contagem.values())
        v = rng.choice([k for k, n in contagem.items() if n == menor])
        resultado[t] = v
        contagem[v] += 1
    return resultado
