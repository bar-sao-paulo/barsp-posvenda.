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

## Etapa 2: WhatsApp automático (número novo na API oficial da Meta)

- **Envio:** com o WhatsApp configurado, a página Envio ganha "Enviar agora pelo WhatsApp". Com
  `ENVIO_AUTOMATICO=1`, o painel faz sozinho, todo dia a partir de `ENVIO_HORA` (padrão 11h): busca a lista de
  ontem e manda a pesquisa com os modelos aprovados (`pesquisa_a/b/c`, `{{1}}` = nome, `{{2}}` = prato). Uma vez
  por dia; quem pediu PARAR é pulado.
- **Respostas:** chegam pelo webhook `/webhook/whatsapp` (assinado pela Meta). Ficam ligadas ao cliente pelo
  telefone; o resto vai para "Fora da lista". "PARAR" tira o número da pesquisa e confirma ao cliente.
- **Análise:** 10 minutos depois da última mensagem do cliente, o Claude sugere nota, gravidade, resumo, resposta e
  ação. A página **Respostas** mostra da mais crítica para a menos crítica e baixa a planilha (CSV).
- **Quem responde:** 🔴 e tudo que tiver algum alerta (compensação, vocabulário proibido, falha da IA) fica em
  "Revisar antes de enviar" e avisa por e-mail. Com `RESPOSTA_AUTOMATICA=1`, só as sem alerta saem sozinhas.
  O WhatsApp só aceita texto livre até 24 h depois da última mensagem do cliente.

## Aba Google: respostas às avaliações

A cada 30 minutos o painel lê as avaliações do Perfil da Empresa no Google e o Claude escreve a resposta:
agradecimento nos elogios e, nas reclamações, pedido de desculpas terminando com o pedido de uma nova oportunidade
(nunca pede para o cliente explicar o que houve nem chama no privado). Nunca cupom,
desconto, brinde ou cortesia (a trava vale também para quem publica pela tela).

- **4 e 5 estrelas** sem nenhum alerta são publicadas sozinhas no Google.
- **1 a 3 estrelas**, avaliação sem estrelas, assunto grave (intoxicação, bicho, discriminação, briga, cobrança
  indevida…) ou texto que bate em alguma trava ficam em "Para revisar" e avisam por e-mail.
- Só as avaliações que chegam depois de ligar são respondidas. As antigas aparecem em "Antigas sem resposta",
  com o botão "Sugerir resposta" (essa sugestão nunca sai sozinha).
- Respondeu direto no Google? O painel vê na próxima leitura e não mexe mais.

O Google só libera a API do Perfil da Empresa depois de um pedido de acesso. Até lá a tela mostra "Cota zerada".

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
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | Token do usuário do sistema e ID do número (Meta) |
| `WHATSAPP_APP_SECRET` | Chave secreta do app da Meta (confere a assinatura do webhook) |
| `WHATSAPP_VERIFY_TOKEN` | Texto que você inventa e repete na tela do webhook da Meta |
| `WHATSAPP_MODELO_A/B/C` | Opcional; nomes dos modelos (padrão `pesquisa_a`, `pesquisa_b`, `pesquisa_c`) |
| `ANTHROPIC_API_KEY` | Chave da API do Claude (análise das respostas) |
| `ENVIO_AUTOMATICO`, `ENVIO_HORA` | `1` liga o envio diário; hora de São Paulo (padrão 11) |
| `RESPOSTA_AUTOMATICA` | `1` deixa sair sozinha a resposta sem nenhum alerta (🔴 nunca sai) |
| `ALERTA_EMAIL`, `RESEND_API_KEY`, `EMAIL_FROM`, `PAINEL_URL` | Opcionais; aviso por e-mail de crítica |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Cliente OAuth (tipo "Aplicativo da Web") do projeto no Google Cloud |
| `GOOGLE_REFRESH_TOKEN` | Autorização da conta dona do Perfil da Empresa, escopo `https://www.googleapis.com/auth/business.manage` |
| `GOOGLE_LOCAL` | Opcional; `accounts/…/locations/…`. Sem ele, o painel usa o único local da conta |
| `GOOGLE_RESPOSTA_AUTOMATICA` | `0` deixa tudo como sugestão; padrão ligado (elogios 4 e 5 estrelas saem sozinhos) |
| `PAINEL_API_TOKEN` | Opcional; texto aleatório longo que libera `GET /api/resumo` (só números, sem dados de cliente) para o painel financeiro |

## Testes

```bash
pip install -r requirements.txt
python -m pytest -q
```
