# CLAUDE.md — Pós-venda do Bar São Paulo (barsp-posvenda)

Contexto para o Claude Code. Dono: Emerson (Bar São Paulo). Interface, mensagens e commits em **português do Brasil**.
Sistema **separado** do painel de gestão (barsp-gestao): não importar código nem usar o banco dele.

## Regras de segurança (obrigatórias)
- **Nunca** pedir, colar, imprimir ou gravar chaves, tokens, senhas ou códigos OAuth. Credenciais só nas variáveis do Railway.
- No código e nos testes, só **nomes** de variáveis, nomes e telefones fictícios.
- Telefone completo só no link do WhatsApp, no CSV interno e no botão "Ver telefone" (busca `/telefone/<id>` ao clicar,
  decidido pelo Emerson em 15/10/2026); na tela use `lista.mascarar`.
- Nunca guardar respostas ou dados de clientes fora do banco do pós-venda.
- Antes de mudar algo grande, **confirmar o plano com o Emerson**.
- Decisões que dependem da API da Takeat: citar a página `.md` da documentação
  (ex.: `md/v1/referencia/pedidos/getTableSessionsV1.md`).

## Regras da pesquisa (instruções do projeto)
- Textos A, B e C em `lista.VERSOES`; ajustes só pequenos, mantendo o tom e a gramática.
- Fora da lista: iFood, sem celular, "não enviar", recebeu nos últimos 7 dias, telefone repetido.
- Versões equilibradas (nenhuma passa de metade da lista com 3+ clientes).
- Nunca prometer desconto, cortesia ou reembolso ao cliente; no máximo 2 emojis por mensagem.
- Nunca dizer ao cliente quantas vezes ele veio ou comprou ("terceira visita", "cliente fiel"): o número costuma estar
  errado (Emerson, 17/10/2026). O histórico vai só para a ação da equipe; a trava `_VISITAS` manda para revisão.
- Respostas: 🔴 0–5, 🟡 6–8, 🟢 9–10; elogio curto sem crítica ("ótimo", "tudo certo", 👍, ❤️) ou sugestão sem crítica = 9 estimada; neutra/ambígua ("ok", "oi", resposta automática) = 8 estimada; ressalva vai para Resumo e Ação.
- 🔴 nunca sai sozinha ("Revisar antes de enviar"). Resposta ligada só pelo telefone (DDD + 8 últimos dígitos);
  número que não está na lista fica em "Fora da lista". "PARAR" bloqueia o telefone.
- Áudio: o Claude não ouve. O id da mídia fica em `mensagens.midia_id` (o arquivo não é guardado; o painel busca na Meta
  ao tocar); áudio sem texto (`direcao="equipe"` escrito pela equipe ou `"transcrito"`) vai para "Revisar antes de enviar".
  Com `OPENAI_API_KEY`, o agendador transcreve cada áudio uma vez (`transcricao.py`, trava `transcrever:<id>`); o áudio
  vai para a OpenAI só para virar texto (decidido pelo Emerson em 15/10/2026).
- O Claude sugere nota/resumo/resposta/ação (`ia.py`); as travas (gravidade, revisão, vocabulário proibido,
  compensação, emojis, janela de 24 h) ficam no código (`respostas.py`), não no modelo.

## Mapa do código (`posvenda/`)
| Arquivo | Papel |
|---|---|
| `lista.py` | Regras puras: comandas → clientes (prato principal e todos os itens do dia), filtros, versões, mensagem, link `wa.me` |
| `servico.py` | Lista do dia no banco (gerar, obter, marcar enviado, não enviar, CSV); `preencher_itens` completa os itens das listas antigas |
| `db.py` | Tabelas SQLAlchemy (`envios`, `nao_enviados`, `bloqueios`, `buscas`, `mensagens`, `respostas`, `rotinas`); colunas novas em `_COLUNAS_NOVAS` |
| `takeat.py` | Cliente da Takeat (API key → token com escopo `table-sessions:read`) |
| `whatsapp.py` | WhatsApp Cloud API (modelo, texto, situação e registro do número, download de áudio/foto) e leitura/assinatura do webhook |
| `ia.py` | Análise da resposta com o Claude (`claude-opus-5-5`, saída em JSON) |
| `respostas.py` | Mensagens recebidas, PARAR, análise, travas, envio da resposta, planilha |
| `historico.py` | Compras do cliente nos últimos 12 meses (dias com comanda, tabela `visitas`), lidas do Takeat em blocos de 3 dias (`historico_blocos`) pelo agendador |
| `transcricao.py` | Transcrição dos áudios (OpenAI `audio/transcriptions`, liga com `OPENAI_API_KEY`; modelo em `TRANSCRICAO_MODELO`) |
| `automacao.py` | Envio automático do dia (trava em `rotinas`), aviso de crítica por e-mail, leitura do histórico, agendador (thread) |
| `api_resumo.py` | `GET /api/resumo` (só leitura, `Authorization: Bearer PAINEL_API_TOKEN`): números do pós-venda por mês (enviadas, respostas, nota média, gravidades), sem nenhum dado de cliente, para o painel financeiro |
| `web.py` | Painel Flask: login, Envio, Respostas, Não enviar, WhatsApp (situação e registro do número com PIN, que não é gravado), CSV, webhook `/webhook/whatsapp`, `/saude` |

## Convenções
- Datas/horas: fuso `America/Sao_Paulo` (`lista.TZ`); no banco em UTC.
- A consulta do Takeat usa datas UTC e no máximo 3 dias; o dia da visita é o `start_time` em horário de São Paulo.
- Todos os testes devem passar antes de subir: `python -m pytest -q`
  (e `POSVENDA_TEST_PG=postgresql+psycopg://... python -m pytest -q tests/test_automacao.py` contra Postgres).
- `ENVIO_AUTOMATICO` e `RESPOSTA_AUTOMATICA` ficam desligados até o Emerson pedir.
