"""Lista do envio de cada dia: busca no Takeat, aplica as regras e guarda no banco."""
from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from . import lista
from .db import bloqueios, buscas, envios, nao_enviados

# (inicio_utc, fim_utc) -> comandas do Takeat
BuscarSessoes = Callable[[datetime, datetime], list[dict[str, Any]]]


def agora() -> datetime:
    return datetime.now(timezone.utc)


def hoje_local() -> date:
    return agora().astimezone(lista.TZ).date()


@dataclass
class ListaDoDia:
    dia: date
    buscado_em: datetime | None
    envios: list[dict[str, Any]]
    nao_enviados: list[dict[str, Any]]

    @property
    def por_versao(self) -> dict[str, int]:
        c = {v: 0 for v in lista.VERSOES}
        for e in self.envios:
            c[e["versao"]] += 1
        return c

    @property
    def resumo(self) -> str:
        v = self.por_versao
        return (f"{len(self.envios)} mensagens prontas (A: {v['A']}, B: {v['B']}, C: {v['C']}) | "
                f"{len(self.nao_enviados)} não enviados")


def enviados_ultimos_7_dias(engine: Engine, dia: date) -> set[str]:
    with engine.connect() as con:
        rows = con.execute(select(envios.c.telefone).where(and_(
            envios.c.enviado_em.is_not(None),
            envios.c.data_visita >= dia - timedelta(days=7),
            envios.c.data_visita < dia,
        )))
        return {r[0] for r in rows}


def telefones_bloqueados(engine: Engine) -> set[str]:
    with engine.connect() as con:
        return {r[0] for r in con.execute(select(bloqueios.c.telefone))}


def gerar_lista(engine: Engine, dia: date, buscar: BuscarSessoes, *, hoje: date | None = None,
                rng: random.Random | None = None) -> ListaDoDia:
    """Busca as comandas do dia no Takeat e refaz a lista. Quem já foi marcado como enviado fica como está,
    e quem já tinha versão sorteada mantém a versão."""
    hoje = hoje or hoje_local()
    inicio, fim = lista.janela_utc(dia)
    candidatos = lista.clientes_do_dia(buscar(inicio, fim), dia)
    ficam, fora = lista.filtrar(candidatos, enviados_ultimos_7_dias(engine, dia), telefones_bloqueados(engine))

    with engine.begin() as con:
        existentes = {r.telefone: r for r in con.execute(select(envios).where(envios.c.data_visita == dia))}
        ja_enviados = {t for t, r in existentes.items() if r.enviado_em is not None}
        ficam = [c for c in ficam if c.telefone not in ja_enviados]
        versoes = lista.sortear_versoes([c.telefone for c in ficam],
                                        {t: r.versao for t, r in existentes.items()}, rng)
        telefones_novos = {c.telefone for c in ficam}
        for t, r in existentes.items():
            if r.enviado_em is None and t not in telefones_novos:
                con.execute(delete(envios).where(envios.c.id == r.id))
        for c in ficam:
            v = versoes[c.telefone]
            dados = dict(nome_completo=c.nome_completo, nome=c.nome, origem=c.origem, prato=c.prato, versao=v,
                         mensagem=lista.montar_mensagem(v, c.nome, c.prato, dia, hoje))
            if c.telefone in existentes:
                con.execute(update(envios).where(envios.c.id == existentes[c.telefone].id).values(**dados))
            else:
                con.execute(insert(envios).values(data_visita=dia, telefone=c.telefone, criado_em=agora(), **dados))
        con.execute(delete(nao_enviados).where(nao_enviados.c.data_visita == dia))
        for r in fora:
            con.execute(insert(nao_enviados).values(data_visita=dia, nome_completo=r.candidato.nome_completo,
                                                    origem=r.candidato.origem, motivo=r.motivo))
        con.execute(delete(buscas).where(buscas.c.data_visita == dia))
        con.execute(insert(buscas).values(data_visita=dia, buscado_em=agora()))
    return obter_lista(engine, dia)


def obter_lista(engine: Engine, dia: date) -> ListaDoDia:
    with engine.connect() as con:
        busca = con.execute(select(buscas.c.buscado_em).where(buscas.c.data_visita == dia)).scalar()
        es = [dict(r._mapping) for r in con.execute(
            select(envios).where(envios.c.data_visita == dia).order_by(envios.c.origem.desc(), envios.c.nome_completo))]
        ns = [dict(r._mapping) for r in con.execute(
            select(nao_enviados).where(nao_enviados.c.data_visita == dia).order_by(nao_enviados.c.motivo, nao_enviados.c.id))]
    return ListaDoDia(dia, busca, es, ns)


def marcar_enviado(engine: Engine, envio_id: int, mensagem: str | None = None) -> bool:
    valores: dict[str, Any] = {"enviado_em": agora()}
    if mensagem and mensagem.strip():
        valores["mensagem"] = mensagem.strip()
    with engine.begin() as con:
        r = con.execute(update(envios).where(envios.c.id == envio_id).values(**valores))
        return r.rowcount == 1


def desmarcar_enviado(engine: Engine, envio_id: int) -> None:
    with engine.begin() as con:
        con.execute(update(envios).where(envios.c.id == envio_id).values(enviado_em=None))


def bloquear(engine: Engine, telefone_bruto: str, motivo: str = "") -> str | None:
    """Coloca o telefone na lista de quem não quer receber. Devolve o telefone normalizado ou None se inválido."""
    tel = lista.normalizar_telefone(telefone_bruto)
    if not tel:
        return None
    with engine.begin() as con:
        con.execute(delete(bloqueios).where(bloqueios.c.telefone == tel))
        con.execute(insert(bloqueios).values(telefone=tel, motivo=(motivo or "").strip()[:200], criado_em=agora()))
    return tel


def desbloquear(engine: Engine, telefone: str) -> None:
    with engine.begin() as con:
        con.execute(delete(bloqueios).where(bloqueios.c.telefone == telefone))


def listar_bloqueios(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as con:
        return [dict(r._mapping) for r in con.execute(select(bloqueios).order_by(bloqueios.c.criado_em.desc()))]


def _sem_ponto_e_virgula(texto: Any) -> str:
    return str(texto or "").replace(";", ",").strip()


def csv_lista_do_envio(dados: ListaDoDia) -> str:
    """Bloco "Lista do envio" para o Modo 2: Cliente;Telefone;Origem;Prato/pedido;Versão."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\n")
    w.writerow(["Cliente", "Telefone", "Origem", "Prato/pedido", "Versão"])
    for e in dados.envios:
        w.writerow([_sem_ponto_e_virgula(e["nome_completo"] or e["nome"] or ""), e["telefone"], e["origem"],
                    _sem_ponto_e_virgula(e["prato"] or "seu pedido"), e["versao"]])
    return saida.getvalue()
