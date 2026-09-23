# CLAUDE.md

Concepção, racional e roadmap estão em [`docs/concepcao.md`](docs/concepcao.md).
Superfície MCP em [`MCP_TOOLS.md`](MCP_TOOLS.md), contrato de ingestão em
[`spec/README.md`](spec/README.md). Leia antes de mudar qualquer coisa. Não
repita o conteúdo deles aqui.

Estado: v0, especificação e modelo de dados. Não há servidor: o que existe em
[`server/`](server/README.md) é um stub sobre fixtures, andaime para medir se o
agente chama as ferramentas na hora certa. O que vem depois está em
[Próximos passos](docs/concepcao.md#próximos-passos) — não antecipe etapa sem
combinar.

## Especificação

- Mudança em `spec/` exige um ADR novo em `docs/adr/`, escrito a partir de
  [`0000-template.md`](docs/adr/0000-template.md).
- Campo novo entra como opcional.
- Tornar obrigatório um campo existente é mudança de versão maior do contrato.

## ADRs

Nunca preencha, em ADR alguma:

- o campo **Decisor**;
- o campo **Expectativa** — nem resultado esperado, nem confiança;
- o **Status**, que fica em `Proposto`.

Isso é atestação humana, por princípio do próprio projeto
([ADR 0002](docs/adr/0002-mcp-first.md)). Deixe `*a preencher*` e siga.

## Ferramentas MCP

- São seis. Não crie a sétima sem ADR.
- Nenhuma ferramenta recebe confiança ou expectativa como parâmetro — nem para
  recusar: o que está no schema de entrada é convite para o agente preencher.
- O stub em `server/` responde as seis a partir de fixtures. Mudou descrição de
  ferramenta, mude nos dois lugares e diga no PR em que situação o agente
  deixava de chamá-la.

## Fontes externas

- Somente leitura ([ADR 0004](docs/adr/0004-somente-leitura.md)). Nunca escreva
  em GrowthBook, ABsmartly ou qualquer outra plataforma.
- Nunca compare, agregue ou ranqueie efeitos de fontes diferentes
  ([ADR 0003](docs/adr/0003-normalizar-afirmacao.md)).

## Banco

- Toda invariante garantida pelo banco tem teste em `db/test_invariants.sql`.
- Mudou ou criou invariante: mude ou crie o teste na mesma alteração.

## Estilo

- Identificadores de código em inglês.
- Documentação em português.

## Git

- Commits pequenos, mensagem descritiva.
- Mostre o diff antes de fazer push. Sempre.

## Rodar os testes

Os quatro abaixo são exatamente o que a CI roda
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

### Invariantes do banco

Precisa de um PostgreSQL 16 vazio. As invariantes rodam numa transação desfeita
ao final.

```bash
createdb decision_memory_test
psql -v ON_ERROR_STOP=1 -d decision_memory_test -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
dropdb decision_memory_test
```

Sem Postgres local, com Docker:

```bash
docker run --rm -d --name dm-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=decision_memory -p 5432:5432 postgres:16
until pg_isready -h localhost -U postgres -q; do sleep 1; done
PGHOST=localhost PGUSER=postgres PGPASSWORD=postgres PGDATABASE=decision_memory \
  psql -v ON_ERROR_STOP=1 -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
docker rm -f dm-pg
```

### Exemplos contra o contrato

Os extras de formato não são opcionais: sem eles `date`, `email` e `uri` viram
anotação e a validação passa vazia. O script aborta se isso acontecer.

```bash
pip install "jsonschema[format]>=4.21,<5"
python scripts/validate_examples.py
```

### Links relativos dos arquivos `.md`

Confere alvo e âncora. Ignora links externos e o que está dentro de bloco de
código.

```bash
python scripts/check_links.py
```

### Stub do servidor MCP

A suíte também valida o corpus de fixtures contra o contrato de ingestão.

```bash
pip install -r server/requirements.txt
python -m pytest server/tests -q
```
