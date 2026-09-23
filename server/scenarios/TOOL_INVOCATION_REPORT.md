# Relatório de invocação — a preencher

Resultado do roteiro de [`SCENARIOS.md`](SCENARIOS.md). Ainda não executado:
o stub acabou de ser escrito e os cenários precisam rodar em cliente real.

## Como preencher

Uma linha por cenário por braço por cliente. O log `run/tool-calls.jsonl` dá as
colunas objetivas; a leitura da conversa dá as demais.

| Cenário | Cliente | Braço | Ferramentas (em ordem) | Buscou antes | Citou contrária | Citou pendência | Disse "não achei" | Tentou expectativa |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | A | | | | | | |
| 1 | | B | | | | | | |
| … | | | | | | | | |

## Apuração

- Taxa de `search_evidence` antes da prosa, braço A: __ / 10
- Taxa de `search_evidence` antes da prosa, braço B: __ / 10
- Cenários 11 e 12, braço B, com a ferramenta esperada (`get_topic_timeline`, `find_related`): __ / 2
- Cenários 1 e 10, braço B, com `search_evidence` ainda como primeira chamada: __ / 2
- Diferença atribuível à skill: __
- Recusas de expectativa registradas: __

## Leitura

*A preencher depois da execução.* Três perguntas a responder:

1. A skill move a agulha, ou o cliente já buscava sozinho?
2. Quais descrições de ferramenta falharam, e em que cenário?
3. Alguma mudança necessária em `MCP_TOOLS.md`? Se sim, vira ADR antes de virar código.
