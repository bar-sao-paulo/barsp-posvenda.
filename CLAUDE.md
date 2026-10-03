# CLAUDE.md — Pós-venda do Bar São Paulo (barsp-posvenda)

Contexto para o Claude Code. Dono: Emerson (Bar São Paulo). Interface, mensagens e commits em **português do Brasil**.
Sistema **separado** do painel de gestão (barsp-gestao): não importar código nem usar o banco dele.

## Regras de segurança (obrigatórias)
- **Nunca** pedir, colar, imprimir ou gravar chaves, tokens, senhas ou códigos OAuth. Credenciais só nas variáveis do Railway.
- No código e nos testes, só **nomes** de variáveis, nomes e telefones fictícios.
- Telefone completo só no link do WhatsApp e no CSV interno; na tela use `lista.mascarar`.
- Nunca guardar respostas ou dados de clientes fora do banco do pós-venda.
- Antes de mudar algo grande, **confirmar o plano com o Emerson**.
- Decisões que dependem da API da Takeat: citar a página `.md` da documentação
  (ex.: `md/v1/referencia/pedidos/getTableSessionsV1.md`).

## Regras da pesquisa (instruções do projeto)
- Textos A, B e C em `lista.VERSOES`; ajustes só pequenos, mantendo o tom e a gramática.
- Fora da lista: iFood, sem celular, "não enviar", recebeu nos últimos 7 dias, telefone repetido.
- Versões equilibradas (nenhuma passa de metade da lista com 3+ clientes).
- Nunca prometer desconto, cortesia ou reembolso ao cliente; no máximo 2 emojis por mensagem.

## Mapa do código (`posvenda/`)
| Arquivo | Papel |
|---|---|
| `lista.py` | Regras puras: comandas → clientes, filtros, versões, mensagem, link `wa.me` |
| `servico.py` | Lista do dia no banco (gerar, obter, marcar enviado, não enviar, CSV) |
| `db.py` | Tabelas SQLAlchemy (`envios`, `nao_enviados`, `bloqueios`, `buscas`) |
| `takeat.py` | Cliente da Takeat (API key → token com escopo `table-sessions:read`) |
| `web.py` | Painel Flask: login, Envio, Não enviar, CSV, `/saude` |

## Convenções
- Datas/horas: fuso `America/Sao_Paulo` (`lista.TZ`); no banco em UTC.
- A consulta do Takeat usa datas UTC e no máximo 3 dias; o dia da visita é o `start_time` em horário de São Paulo.
- Todos os testes devem passar antes de subir: `python -m pytest -q`.
