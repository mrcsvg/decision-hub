# ADR 0001 — Ingestão contract-first

- **Status:** Proposto
- **Data:** 2026-09-21
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível, com custo crescente

## Contexto

O registro precisa receber resultados de plataformas de experimentação comerciais — GrowthBook, ABsmartly, Statsig, Eppo — e de plataformas internas construídas pelas próprias empresas. As plataformas internas são o público de maior valor: têm rigor estatístico e nenhuma camada de conhecimento, porque o time que as construiu não prioriza repositório de aprendizado.

No modelo adapter-first, o projeto escreve e mantém uma integração por fornecedor. Cada mudança de API de terceiro quebra o projeto, e plataformas internas ficam de fora por definição.

## Decisão

O projeto publica um contrato de ingestão versionado ([`spec/experiment-record-v0.schema.json`](../../spec/experiment-record-v0.schema.json)) e trata esse contrato como interface primária. Três modos de entrada, nesta ordem de importância:

1. **Push** — endpoint idempotente que recebe registros válidos contra o contrato. Canônico.
2. **Warehouse** — pacote dbt com contrato de colunas sobre uma view, para quem já grava resultados no data warehouse.
3. **Pull** — adapters contra APIs de fornecedores, no máximo dois mantidos pelo projeto (GrowthBook e ABsmartly).

Todo adapter oficial é implementado sobre o modo push, sem caminho privilegiado. Se um adapter próprio precisar de atalho interno, o contrato está mal desenhado e deve ser corrigido.

O contrato tem três níveis de conformidade; só o nível 0 é obrigatório.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Adapter-first, uma integração por fornecedor | Custo de manutenção cresce com cada fonte e exclui plataformas internas |
| Agente MCP como única camada de integração | Não determinístico e não auditável; inadequado para sistema de registro |
| Adotar o formato de exportação de um fornecedor como padrão | Acopla o projeto a um fornecedor e herda semântica estatística que não se generaliza |

## Evidências

- O GrowthBook já consolida e busca experimentos dentro do próprio domínio (página Learnings), o que torna o nível de fornecedor o lugar errado para competir.
- Não existe padrão aberto para resultado de experimento; o OpenFeature cobre avaliação de flag, não desfecho.
- O modelo OpenTelemetry — especificação mais collector com receivers por fornecedor — mostra que contract-first escala em ecossistemas heterogêneos.

## Consequências

- A especificação passa a ser o artefato mais importante do repositório; mudança nela exige ADR.
- Plataformas internas se integram sem depender do projeto.
- O projeto precisa de testes de conformidade públicos para que produtores validem seus exportadores.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher* (sugestão de métrica: tempo até o primeiro exportador de plataforma interna funcionando)
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
