"""Comandas falsas no formato de GET /v1/table-sessions (nomes e telefones fictícios)."""


def pedido(nome, preco, cancelado=False):
    return {"product": {"name": nome}, "price": f"{preco:.2f}", "canceled_at": "2026-10-02T20:00:00Z" if cancelado else None}


def conta(nome, telefone, *itens):
    return {"buyer": {"name": nome, "phone": telefone} if nome or telefone else None,
            "order_baskets": [{"canceled_at": None, "orders": list(itens)}]}


def sessao(inicio, *contas, delivery=False, canal=None, status="finished", tipo=None):
    return {"start_time": inicio, "status": status, "is_delivery": delivery, "sales_channel": canal,
            "delivery_canceled_at": None, "table": {"table_type": tipo or ("delivery" if delivery else "table")},
            "bills": list(contas)}


def sessoes_de_02_10():
    return [
        sessao("2026-10-02T15:10:00Z",
               conta("JOÃO PEDRO SILVA", "(11) 90000-0023", pedido("Chopp Brahma", 14.9),
                     pedido("Picanha Cheff 1 Pessoa", 89.9), pedido("Fritas Tradicionais", 29.9))),
        sessao("2026-10-02T16:00:00Z",
               conta("Camila", "(11) 90000-0025", pedido("Salada Caesar", 39.9)),
               conta("Mesa 4", "(11) 90000-0026", pedido("Pepsi Lata", 7.0))),
        sessao("2026-10-02T22:30:00Z", conta("João Vitor", "(11) 90000-0024", pedido("Burger da Casa", 42.0)),
               delivery=True, canal="TAKEAT"),
        sessao("2026-10-02T23:00:00Z", conta("Fulano iFood", None, pedido("Burger da Casa", 42.0)),
               delivery=True, canal="IFOOD"),
        sessao("2026-10-02T23:10:00Z", conta("Sem Celular", "(11) 3333-4444", pedido("Risoto de Camarão", 70.0))),
        sessao("2026-10-02T23:20:00Z", conta("Camila Repetida", "(11) 90000-0025", pedido("Brownie", 19.0))),
        sessao("2026-10-02T23:30:00Z", conta("Cancelado", "(11) 90000-0030", pedido("Picanha", 90.0)),
               status="canceled"),
        # 01/10 às 23h em São Paulo (02/10 02:00 UTC): é do dia anterior, fica de fora
        sessao("2026-10-02T02:00:00Z", conta("Ontem à Noite", "(11) 90000-0031", pedido("Picanha", 90.0))),
        # 02/10 às 23h50 em São Paulo (03/10 02:50 UTC): ainda é dia 02
        sessao("2026-10-03T02:50:00Z", conta("Madrugada", "(11) 90000-0032", pedido("Isca de Frango Crocante", 45.0))),
    ]
