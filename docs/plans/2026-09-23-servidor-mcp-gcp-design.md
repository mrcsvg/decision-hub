# Servidor MCP no GCP — design

Data: 2026-09-23. Validado em conversa, seção por seção.

Adianta, por acordo explícito, o servidor real antes do experimento de invocação
previsto em [Próximos passos](../concepcao.md#próximos-passos). O stub em
[`server/`](../../server/README.md) continua existindo para esse experimento.

## Escopo deste corte

- As seis ferramentas de [`MCP_TOOLS.md`](../../MCP_TOOLS.md), leitura e escrita, sobre o Cloud SQL.
- Acesso por IAM do Google Cloud, pelo Claude Code, via `gcloud run services proxy`.
- Dados: o corpus fictício de `server/fixtures/`, carregado por script idempotente.

Fora do corte: OAuth 2.1, URL pública, conector no claude.ai, superfície de
atestação, adapters de plataforma, dados reais.

## Decisões tomadas

| # | Decisão | Alternativas descartadas |
| --- | --- | --- |
| 1 | Acesso por IAM (Cloud Run fechado + proxy do `gcloud`) | OAuth 2.1 agora; OAuth preparado mas desligado |
| 2 | Dados iniciais: corpus de fixtures | Dados reais já; fixtures com loader genérico |
| 3 | As seis ferramentas implementadas | Só leitura; seis com escrita devolvendo erro |
| 4 | Estado da evidência derivado de `provenance.attested_at` | Coluna `state` em `evidence`; remover `state` da resposta |
| 5 | Atestação fora do corte; `attest_url` nulo | CLI de atestação; página web mínima |
| 6 | E-mail autenticado fora de `person`: lê, não escreve | Criar pessoa automaticamente; principal de serviço fixo |
| 7 | Pacote novo `server/decision_memory/`, ao lado do stub | Evoluir o stub; TypeScript |

A decisão 4 fecha uma questão em aberto e vira ADR 0005.

## 1. Arquitetura e deploy

```
Claude Code ──http──▶ gcloud run services proxy (localhost:8080)
                          │ injeta o ID token da conta Google
                          ▼
              Cloud Run "decision-memory"  (southamerica-east1, --no-allow-unauthenticated)
                          │ socket unix /cloudsql/…, usuário dm_app
                          ▼
              Cloud SQL "decision-memory"  (Postgres 16, já existe)
```

- **Serviço:** `server/decision_memory/`, empacotado por `server/Dockerfile`, publicado
  com `gcloud run deploy --source`. `min-instances 0`, `max-instances 2`. SDK `mcp`
  (FastMCP), Streamable HTTP sem estado em `/mcp`. `psycopg` 3.
- **Conta de serviço:** `dm-server@ufpr-ppgcd`, com `roles/cloudsql.client` e acesso
  somente ao segredo `dm-app-password` no Secret Manager.
- **Quem chama:** `roles/run.invoker` concedido por pessoa, no serviço.
- **Identidade:** middleware lê `Authorization: Bearer`, valida o ID token com
  `google-auth` e extrai `email`. **A verificar:** que o Cloud Run repassa o header
  ao container. Sem identidade, falha fechado: leitura funciona, escrita é recusada.
- **Cliente:** `gcloud run services proxy` + `claude mcp add --transport http
  decision-memory http://localhost:8080/mcp` + a skill de `server/skill/SKILL.md`.

## 2. Banco

**`db/grants.sql`**, aplicado depois do `schema.sql`, cria o papel `dm_app` (`LOGIN`,
sem senha no arquivo):

| Permissão | Tabelas |
| --- | --- |
| `SELECT` | todas |
| `INSERT` (tabela) | `alternative`, `decision_evidence`, `decision_learning`, `evidence_learning`, `tag`, `decision_tag`, `evidence_tag`, `learning_tag`, `idempotency_key` |
| `INSERT` (colunas) | `provenance` (`object_type`, `object_id`, `author_kind`, `principal_person_id`, `model`, `source_ref`); `decision` (`slug`, `title`, `context`, `description`, `door`, `decided_on`, `decider_person_id`, `project_id`); `learning` (`summary`, `recorded_on`); `evidence` (`kind`, `title`, `summary`, `url`, `source_system`, `external_id`, `strength`) |
| nenhuma escrita | `expectation`, `review`, `person`, `assignment`, `project`, `job_title`, `org_area`, `indicator`, `measurement` |
| `UPDATE`, `DELETE`, `TRUNCATE` | nenhuma |
| `CREATE` no schema `public` | nenhum (`REVOKE CREATE ON SCHEMA public FROM PUBLIC`, que só faz diferença no PostgreSQL 14) |

O servidor só acrescenta. "Nenhuma ferramenta escreve expectativa" passa a ser
garantido pelo banco. O INSERT por coluna impede que o servidor ateste o que
escreve (ADR 0002): `attested_by`/`attested_at`, `state` e o vínculo na data da
decisão ficam de fora, assim como as colunas de importação da evidência. Que
`author_kind` seja `agent`, e não `human` ou `import`, continua a cargo do servidor.

**Invariantes novas** em `db/test_invariants.sql`, sob `SET ROLE dm_app`: inserir em
`expectation` e `review`, `UPDATE` em `decision`, `DELETE` em `evidence` e INSERT
com colunas de atestação (`provenance.attested_at`, `state = 'attested'` em
`decision` e `learning`) falham com `insufficient_privilege`. Mais o teste da `idempotency_key`.

**Tabela `idempotency_key`**, aditiva: chave `(principal_person_id, key)`, mais
`object_type`, `object_id`, `created_at`.

**Estado da evidência:** `attested` se a procedência tem `attested_at`, `proposed`
caso contrário. Em `decision` e `learning` a coluna `state` continua sendo a fonte;
a assimetria fica explícita no ADR 0005.

**Carga — `scripts/load_fixtures.py`**, com `dm_admin` pelo proxy:

- Idempotente: evidência por `source_system='fixtures'` + `external_id`; decisão por
  `slug`; lição por `provenance.source_ref = 'fixtures:<id>'`.
- Pessoas: `fixtures/people.json` mais `--people pessoas.csv` (nome, e-mail), fora do
  repositório.
- Procedência `author_kind = import`; registros atestados recebem `attested_by`
  (`--attested-by <email>`) e `attested_at`. `dec-push-diario` continua proposto.
- Expectativas das fixtures entram com `recorded_by` = decisor, pelo `dm_admin`.

## 3. Ferramentas

- **`search_evidence`:** `websearch_to_tsquery('simple', query)` sobre as colunas
  `search`; ordem só por `ts_rank`, desempate por `id`; nunca por força ou efeito
  ([ADR 0003](../adr/0003-normalizar-afirmacao.md)). Limitação: `simple` não trata
  acento nem radical.
- **`get_decision`:** por `id` ou `slug`; expectativa só se `attested`.
- **Escritas:** uma transação por chamada. Resolve o principal; resolve
  `decider_email` e `project` (inexistente → mensagem do `MCP_TOOLS.md`, nada
  gravado); insere `proposed` com procedência `author_kind = agent`. `attest_url` nulo.
- **`provenance.model`:** o MCP não informa o modelo. Grava o `clientInfo` do cliente
  quando houver, `unknown` quando não.
- **`list_pending_reviews`:** `owner_email` padrão = pessoa autenticada.
- **Todas:** `structuredContent` `{data, pending}` + texto curto, `outputSchema`
  declarado; parâmetro de confiança ou expectativa recusado; `pending` calculado a
  cada resposta, vazio mas nunca ausente; descrições idênticas às do `MCP_TOOLS.md`.

## 4. Erros, testes, CI

- Erros de validação com as mensagens do `MCP_TOOLS.md`. Erro de banco: mensagem
  genérica com id de correlação; detalhe no log JSON. `insufficient_privilege` é bug.
- `server/tests_real/` contra Postgres real, conectado como `dm_app`, com as fixtures
  carregadas. Portados do stub: seis ferramentas, sem confiança nos schemas, ordem
  independente de efeito, proposta sem expectativa, `pending` presente, descrições.
  Novos: identidade ausente, pessoa desconhecida, idempotência, reaproveitamento de
  evidência, rollback, evidência de agente aparece `proposed`.
- Identidade testada com verificador injetado. Nenhum teste fala com o GCP.
- CI: job novo com `services: postgres:16`; `test_invariants.sql` roda depois do
  `grants.sql`; seção "Rodar os testes" do `CLAUDE.md` atualizada.
- Sem cobertura automática: deploy, header de identidade, socket unix. Roteiro manual
  no README do servidor, com teste de fumaça (`search_evidence "checkout"` traz
  `ev-checkout-pagina-unica`).

## Divergências em relação ao `MCP_TOOLS.md`

Registradas no README do servidor:

- IAM em vez de OAuth 2.1.
- `attest_url` nulo; atestação fora do corte.
- `provenance.model` vem do `clientInfo`, não do modelo.

## Correções durante o plano

O que a implementação mudou em relação ao texto acima. O detalhe de cada ponto está no
[README do servidor](../../server/decision_memory/README.md).

- **Identidade:** o Cloud Run entrega o token sem assinatura; não há revalidação com
  `google-auth` nem verificador injetado. O servidor lê as claims e confere `aud` e `iss`
  (Google). Por isso o serviço fechado é requisito de segurança, verificado no deploy. Com
  `X-Serverless-Authorization` presente, a identidade vem só dele, o único header que o Cloud
  Run confere quando vêm os dois. `DM_EXPECTED_AUDIENCE` é obrigatório no Cloud Run e vai já no
  primeiro deploy.
- **`provenance.model`:** o `clientInfo` não chega em modo sem estado. Fica `unknown`, e o
  `User-Agent` vai para `source_ref`.
- **Busca:** `websearch_to_tsquery` exige todos os termos e perguntas em linguagem natural não
  achariam nada. Termos em OR, no máximo 32, com a regra de relevância do stub portada. Acento
  e caixa são ignorados dobrando o texto com `translate()` na consulta, o que deixa o índice
  GIN de `search` sem uso; radical continua não tratado.
- **Carga:** `python -m decision_memory.seed`, ids uuid5 das fixtures, em vez de
  `scripts/load_fixtures.py`.
- **Banco:** `INSERT` por coluna em `provenance`, `decision`, `learning` e `evidence` (tabela
  da seção 2). Todo comando SQL e a espera por conexão do pool têm teto de 5 s.
- **`attach_evidence`:** agente não cria evidência com `source_system`/`external_id`, de
  qualquer tipo; identidade de origem entra só pela ingestão. Par já existente é reaproveitado.
- **`propose_decision`:** devolve `reused`; com `idempotency_key` já usada, volta o registro
  gravado e o conteúdo da chamada nova não é aplicado.
- **Entrada:** tags de escrita seguem o padrão do contrato (`^[a-z0-9][a-z0-9 _./-]*$`); texto
  com `\x00` é recusado em qualquer ferramenta.
- **Transporte:** só POST em `/mcp`; GET, DELETE e os demais respondem 405 com `Allow: POST`.
