# decision-memory

> Nome provisório.

Registro open source de decisões e aprendizagens de produto. Consolida evidência de experimentos, pesquisas e análises, guarda o que se esperava antes do desfecho e cobra a revisão.

## Por que existe

Organizações guardam o *o quê* de suas decisões e perdem o *porquê*: o contexto da época, as alternativas descartadas, a evidência pesada e o resultado esperado. A mesma questão é redecidida anos depois, do zero, e ninguém consegue responder se o time costuma acertar.

Existem boas ferramentas para cada tipo de evidência isolado — plataformas de experimentação, repositórios de pesquisa, ferramentas de discovery. Nenhuma registra a decisão que atravessa todos eles.

## O que é

- Um registro de decisões com contexto, alternativas descartadas e responsável
- Evidência de qualquer origem, referenciada e não copiada, com o papel que teve na decisão
- Lições aprendidas como entidade própria, reutilizáveis entre projetos
- Expectativa congelada antes do desfecho e revisão agendada
- Um contrato aberto para ingerir resultados de qualquer plataforma de experimentação, inclusive internas

## O que não é

- Uma plataforma de experimentação — não executa, não analisa, não orquestra
- Uma camada que torna efeitos comparáveis entre plataformas ([ADR 0003](docs/adr/0003-normalizar-afirmacao.md))
- Um sistema que escreve de volta nas fontes ([ADR 0004](docs/adr/0004-somente-leitura.md))

## Como funciona

**Interface MCP-first.** Quem escreve e consulta o registro é o agente que já está no fluxo de trabalho — redigindo a proposta, revisando o PR, resumindo a reunião. Seis ferramentas, especificadas em [`MCP_TOOLS.md`](MCP_TOOLS.md). Registros criados por agente nascem como proposta e são atestados por uma pessoa.

**Ingestão contract-first.** Plataformas entregam resultados por um contrato versionado, com três níveis de conformidade. O nível mínimo é o bastante para começar. Especificação em [`spec/`](spec/).

## Estrutura

```
├── README.md
├── MCP_TOOLS.md                       superfície MCP: ferramentas, lembretes, estados
├── docs/
│   ├── concepcao.md                   documento de concepção completo
│   └── adr/                           decisões de arquitetura do próprio projeto
├── spec/
│   ├── experiment-record-v0.schema.json
│   └── examples/
└── db/
    ├── schema.sql                     modelo lógico em PostgreSQL
    └── test_invariants.sql
```

## Rodar o schema

```bash
createdb decision_memory
psql -v ON_ERROR_STOP=1 -d decision_memory -f db/schema.sql
psql -v ON_ERROR_STOP=1 -d decision_memory -f db/test_invariants.sql
```

Requer PostgreSQL 14 ou superior com a extensão `btree_gist`. Os testes rodam numa transação desfeita ao final e verificam as invariantes que o banco garante sozinho: expectativa append-only e anterior ao desfecho, vínculo organizacional na data da decisão, vigências sem sobreposição e ingestão idempotente.

## Decisões de arquitetura

Este projeto registra as próprias decisões no formato que propõe. Todas estão como **Proposto** até serem atestadas por quem decide, e a expectativa de cada uma é preenchida por pessoa, nunca por agente.

| ADR | Decisão | Status |
| --- | --- | --- |
| [0001](docs/adr/0001-contract-first.md) | Ingestão contract-first | Proposto |
| [0002](docs/adr/0002-mcp-first.md) | Interface MCP-first | Proposto |
| [0003](docs/adr/0003-normalizar-afirmacao.md) | Normalizar a afirmação, não a estimativa | Proposto |
| [0004](docs/adr/0004-somente-leitura.md) | Somente leitura sobre as fontes | Proposto |

## Estado

v0 — especificação e modelo de dados. Sem servidor implementado ainda. Próximos passos em [`docs/concepcao.md`](docs/concepcao.md#próximos-passos).

## Licença

[MIT](LICENSE).
