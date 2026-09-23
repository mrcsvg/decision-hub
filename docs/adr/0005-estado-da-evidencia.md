# ADR 0005 — O estado da evidência vem da procedência

- **Status:** Proposto
- **Data:** 2026-09-23
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível

## Contexto

`search_evidence` devolve `state` em todo item ([`MCP_TOOLS.md`](../../MCP_TOOLS.md)), mas a tabela `evidence` não tem estado: ele mora em `decision` e `learning`. O stub contornou respondendo `attested` para tudo. Com o servidor real, `attach_evidence` passa a criar evidência por agente, e essa evidência precisa aparecer como não atestada.

## Decisão

O estado de uma evidência é derivado de `provenance`: `attested` se existe procedência dela com `attested_at` preenchido, `proposed` caso contrário.

Em `decision` e `learning`, a coluna `state` continua sendo a fonte. O modelo fica com duas regras, e isso é deliberado: a alternativa era duplicar a informação de atestação.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Coluna `state` em `evidence` | Duas fontes de verdade sobre atestação — a coluna e `provenance.attested_at` — que podem divergir |
| Tirar `state` da resposta de evidência | Esconde do agente que a evidência foi escrita por outro agente e ainda não foi confirmada |

## Evidências

- A divergência registrada em [`server/README.md`](../../server/README.md#divergências-em-relação-a-mcp_toolsmd).
- `provenance` já aceita `object_type = 'evidence'` e já tem `attested_by`/`attested_at` em [`db/schema.sql`](../../db/schema.sql).

## Consequências

- Evidência sem linha de procedência aparece `proposed`. A importação precisa gravar procedência atestada.
- Consultar o estado de evidência custa uma subconsulta em `provenance`, coberta por `provenance_object_idx`.
- O papel `dm_app` não tem INSERT em `provenance.attested_by`/`attested_at` ([`db/grants.sql`](../../db/grants.sql)), então o servidor não consegue atestar a evidência que ele mesmo cria — só uma pessoa, fora do MCP.
- Se `decision` e `learning` um dia migrarem para a mesma regra, este ADR é o ponto de partida.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher*
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
