from datetime import date, datetime, timezone

from sqlalchemy import insert

from posvenda.db import criar_engine, envios, respostas
from posvenda.respostas import ATENCAO, CRITICO, POSITIVO, REVISAR, RESPONDIDO
from posvenda.web import criar_app

TOKEN = "token-ficticio-" + "x" * 32
AGORA = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def _app(engine):
    app = criar_app(engine=engine, buscar_sessoes=lambda i, f: [], usuario="thais", senha="senha-ficticia",
                    hoje=lambda: date(2026, 10, 14), whatsapp=None, classificar=None)
    app.config["TESTING"] = True
    return app.test_client()


def _envio(con, dia, enviado=True, nota=None, grav=None, status=None, estimada=False, tel="5511900000000"):
    eid = con.execute(insert(envios).values(data_visita=dia, telefone=tel, nome_completo="Cliente Ficticio",
                                            nome="Cliente", origem="salao", prato="Prato", versao="A",
                                            mensagem="Oi", enviado_em=AGORA if enviado else None,
                                            criado_em=AGORA)).inserted_primary_key[0]
    if status:
        con.execute(insert(respostas).values(envio_id=eid, nota=nota, nota_estimada=estimada, gravidade=grav,
                                             resumo="texto do cliente", status=status, ultima_mensagem_em=AGORA))


def test_sem_token_nao_existe(monkeypatch):
    monkeypatch.delenv("PAINEL_API_TOKEN", raising=False)
    assert _app(criar_engine("sqlite://")).get("/api/resumo").status_code == 404


def test_token_errado(monkeypatch):
    monkeypatch.setenv("PAINEL_API_TOKEN", TOKEN)
    c = _app(criar_engine("sqlite://"))
    assert c.get("/api/resumo").status_code == 401
    assert c.get("/api/resumo", headers={"Authorization": "Bearer errado"}).status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/api/resumo?de=2026-10-02&ate=2026-10-01", headers=h).status_code == 400


def test_resumo_por_mes_sem_dados_de_cliente(monkeypatch):
    monkeypatch.setenv("PAINEL_API_TOKEN", TOKEN)
    eng = criar_engine("sqlite://")
    with eng.begin() as con:
        _envio(con, date(2026, 9, 30), nota=10, grav=POSITIVO, status=RESPONDIDO)
        _envio(con, date(2026, 10, 1), nota=4, grav=CRITICO, status=REVISAR)
        _envio(con, date(2026, 10, 2), nota=8, grav=ATENCAO, status=RESPONDIDO, estimada=True)
        _envio(con, date(2026, 10, 2), tel="5511900000001")
        _envio(con, date(2026, 10, 3), enviado=False)
    r = _app(eng).get("/api/resumo?de=2026-09-01&ate=2026-10-31", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    corpo = r.get_data(as_text=True)
    assert "Cliente" not in corpo and "5511" not in corpo and "texto do cliente" not in corpo
    set_, out = r.get_json()["meses"]
    assert set_["mes"] == "2026-09" and set_["enviadas"] == 1 and set_["nota_media"] == 10
    assert out["enviadas"] == 3 and out["respostas"] == 2 and out["nota_media"] == 6
    assert out["criticas"] == 1 and out["atencao"] == 1 and out["positivas"] == 0
    assert out["revisar"] == 1 and out["notas_estimadas"] == 1 and out["taxa_resposta"] == round(2 / 3, 4)
