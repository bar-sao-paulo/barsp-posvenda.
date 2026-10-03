# Bar São Paulo — Pós-venda

Painel só do pós-venda, separado do painel de gestão. Etapa 1: todo dia o painel busca no Takeat quem veio
ao salão ou pediu no delivery no dia anterior, aplica as regras da pesquisa e deixa uma mensagem pronta por
cliente. A Thais toca em "Abrir no WhatsApp", envia e volta para o próximo.

## Como funciona

1. Ao abrir a página **Envio**, o painel busca as comandas de ontem no Takeat (`GET /v1/table-sessions`).
2. Tira da lista, com o motivo: pedido do iFood (o iFood não passa o telefone), sem celular válido,
   quem pediu para não receber (página **Não enviar**), quem recebeu a pesquisa nos últimos 7 dias e
   telefones repetidos.
3. Sorteia a versão A, B ou C de forma equilibrada e monta a mensagem com o primeiro nome e o prato
   principal (bebidas e molhos não contam; sem prato = "seu pedido"; sem nome = "Oi!").
4. A mensagem pode ser ajustada antes de abrir o WhatsApp. Ao tocar no botão, o cliente fica marcado como
   enviado (vale para a regra dos 7 dias). "Desfazer" tira a marca.
5. "Baixar Lista do envio (CSV)" gera o bloco `Cliente;Telefone;Origem;Prato/pedido;Versão` usado no
   relatório das respostas.

O telefone completo só aparece no link do WhatsApp e no CSV; na tela fica mascarado.

## Railway

Serviço web: `railway.json` → `gunicorn 'posvenda.web:criar_app()'`, verificação em `/saude`.
Use um **Postgres só do pós-venda** (não o do painel de gestão). As tabelas são criadas na partida.

Variáveis (só nomes; os valores ficam no Railway):

| Variável | O que é |
|---|---|
| `DATABASE_URL` | Postgres do pós-venda (referência ao serviço Postgres novo) |
| `TAKEAT_API_KEY` | Chave de API do Takeat só para o pós-venda, com o escopo `table-sessions:read` |
| `TAKEAT_BASE_URL` | Opcional; padrão `https://public-api.takeat.app` |
| `SECRET_KEY` | Texto aleatório longo (mantém o login entre reinícios) |
| `LOGIN_USUARIO`, `LOGIN_SENHA` | Acesso ao painel |

## Testes

```bash
pip install -r requirements.txt
python -m pytest -q
```
