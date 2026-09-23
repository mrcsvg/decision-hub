# Servidor MCP

As seis ferramentas de [`MCP_TOOLS.md`](../../MCP_TOOLS.md) sobre o Postgres. Design em
[`docs/plans/2026-09-23-servidor-mcp-gcp-design.md`](../../docs/plans/2026-09-23-servidor-mcp-gcp-design.md).

Não confundir com o [stub](../README.md), que responde a partir de fixtures e existe para o
experimento de invocação.

## Como a pessoa usa

Pré-requisitos: ter `roles/run.invoker` no serviço e, para escrever, estar cadastrada em
`person` (sem cadastro, a leitura funciona e a escrita é recusada).

```bash
gcloud auth login
gcloud run services proxy decision-memory --project ufpr-ppgcd \
  --region southamerica-east1 --port 8080
# em outro terminal, uma vez:
claude mcp add --transport http decision-memory http://localhost:8080/mcp
```

E instale a skill de [`server/skill/SKILL.md`](../skill/SKILL.md): servidor sem skill é
chamado por acidente.

## Variáveis

| Variável | Para quê |
| --- | --- |
| `DM_DATABASE_URL` | Conexão do `dm_app`. No Cloud Run: `postgresql://dm_app@/decision_memory?host=/cloudsql/<instância>` |
| `DM_DATABASE_PASSWORD` | Senha do `dm_app`, injetada do Secret Manager |
| `DM_EXPECTED_AUDIENCE` | URL(s) do serviço, separadas por vírgula; o `aud` do token precisa bater com uma delas ou com o cliente OAuth do gcloud (`32555940559.apps.googleusercontent.com`, ver "Verificação manual"). Obrigatória no Cloud Run: sem ela o servidor não sobe. Fora do Cloud Run, vazia desliga a checagem de `aud` |
| `DM_TODAY` | Só para testes: data de referência das revisões vencidas |

O servidor escuta em `$PORT` (o Cloud Run define), ou 8080. Cada comando SQL tem teto de 5 s,
e a espera por uma das 4 conexões do pool também.

Para rodar a imagem localmente contra o banco da suíte de testes (carregado por
`pytest server/tests_real`):

```bash
docker build -t decision-memory:dev server
docker run --rm -p 8081:8080 -e DM_TODAY=2026-09-22 \
  -e DM_DATABASE_URL=postgresql://dm_app:dm_app_test@host.docker.internal:5432/decision_memory_test \
  decision-memory:dev
```

## Deploy

Uma vez por projeto. Com o [Cloud SQL Auth Proxy](../../README.md#instância-no-cloud-sql) na
porta 5433 e a senha do `dm_admin` em `PGPASSWORD`.

**Antes do passo 2, uma decisão em aberto.** O passo 2 carrega no banco compartilhado o corpus
de `server/fixtures/`, que é fictício, e marca como atestado por `--attested-by` o que as
fixtures trazem atestado. Com um e-mail real ali, o banco passa a ter dados inventados
atestados por uma pessoa de verdade. Decidir, antes de rodar: carregar ou não as fixtures
nesse banco, e em nome de quem.

```bash
PROJECT=ufpr-ppgcd
REGION=southamerica-east1
INSTANCE=$PROJECT:$REGION:decision-memory
SA=dm-server@$PROJECT.iam.gserviceaccount.com
ADMIN="host=127.0.0.1 port=5433 dbname=decision_memory user=dm_admin"

# 1. Banco: tabela nova, depois o papel do servidor (grants.sql de novo, para a tabela
#    nova chegar ao dm_app), e a senha dele
psql "$ADMIN" -v ON_ERROR_STOP=1 \
  -f db/migrations/2026-09-23-idempotency-key.sql -f db/grants.sql
APP_PASSWORD=$(openssl rand -base64 32)
psql "$ADMIN" -c "ALTER ROLE dm_app PASSWORD '$APP_PASSWORD'"

# 2. Dados: fixtures + pessoas reais (CSV name,email, fora do git). Ver a decisão acima.
#    --attested-by precisa estar entre as pessoas cadastradas (fixtures ou CSV).
PYTHONPATH=server .venv/bin/python -m decision_memory.seed --database-url "$ADMIN" \
  --attested-by voce@exemplo.com --people pessoas.csv

# 3. Segredo e conta de serviço
printf '%s' "$APP_PASSWORD" | gcloud secrets create dm-app-password --data-file=- --project $PROJECT
gcloud iam service-accounts create dm-server --project $PROJECT
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role roles/cloudsql.client
gcloud secrets add-iam-policy-binding dm-app-password --project $PROJECT \
  --member serviceAccount:$SA --role roles/secretmanager.secretAccessor

# 4. Serviço, FECHADO. O servidor não sobe no Cloud Run sem DM_EXPECTED_AUDIENCE, então
#    o público vai já no primeiro deploy: a URL determinística do serviço.
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
AUDIENCE=https://decision-memory-$PROJECT_NUMBER.$REGION.run.app
#    --concurrency 8: o pool tem 4 conexões e espera no máximo 5 s por uma; com as 80
#    requisições por instância do padrão, a fila estouraria o prazo antes de escalar.
gcloud run deploy decision-memory --source server --project $PROJECT --region $REGION \
  --service-account $SA --no-allow-unauthenticated \
  --add-cloudsql-instances $INSTANCE --min-instances 0 --max-instances 2 \
  --concurrency 8 \
  --set-env-vars "DM_DATABASE_URL=postgresql://dm_app@/decision_memory?host=/cloudsql/$INSTANCE,DM_EXPECTED_AUDIENCE=$AUDIENCE" \
  --set-secrets DM_DATABASE_PASSWORD=dm-app-password:latest
#    Conferir com as URLs que o Cloud Run de fato atribuiu; se houver outras, aceitar todas.
URLS=$(gcloud run services describe decision-memory --project $PROJECT --region $REGION \
  --format 'value(status.url,metadata.annotations."run.googleapis.com/urls")' | tr -d '[]"' | tr '\t' ',')
[ "$URLS" = "$AUDIENCE" ] || \
  gcloud run services update decision-memory --project $PROJECT --region $REGION \
    --update-env-vars "^;^DM_EXPECTED_AUDIENCE=$URLS"

# 5. Conferir que está fechado. A identidade depende disso.
gcloud run services get-iam-policy decision-memory --project $PROJECT --region $REGION \
  | grep -q allUsers && echo "ABERTO — remova allUsers antes de usar" || echo "fechado"

# 6. Acesso por pessoa
gcloud run services add-iam-policy-binding decision-memory --project $PROJECT \
  --region $REGION --member user:pessoa@exemplo.com --role roles/run.invoker
```

**Por que o serviço tem de ficar fechado:** o Cloud Run valida o ID token e o entrega ao
container sem assinatura. O servidor confia nessa validação: lê as claims e confere só `aud`
(contra `DM_EXPECTED_AUDIENCE` e o cliente do gcloud) e `iss` (o Google). Com o serviço aberto, qualquer um forja o
e-mail no token e escreve em nome de outra pessoa. Mesmo fechado, quando vêm
`X-Serverless-Authorization` e `Authorization` juntos o Cloud Run só confere o primeiro; por
isso o servidor, nesse caso, lê a identidade só dele, e nunca cai para o `Authorization`.

## Verificação manual depois do deploy

Não há teste automático contra o GCP.

O token que o proxy injeta é o da sua conta de usuário no gcloud, e o `aud` dele **não** é a
URL do serviço: é `32555940559.apps.googleusercontent.com`, o id público do cliente OAuth do
gcloud (o mesmo de `gcloud auth print-identity-token`; `iss` é `https://accounts.google.com`).
O Cloud Run aceita esse token e confere o público na borda; o servidor aceita esse `aud` além
das URLs de `DM_EXPECTED_AUDIENCE`, como defesa em profundidade. Token de conta de serviço,
emitido com `--audiences`, traz a URL do serviço.

Com o proxy do `gcloud run services proxy` no ar:

```bash
curl -s localhost:8080/mcp \
  -H 'accept: application/json, text/event-stream' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_evidence","arguments":{"query":"checkout"}}}'
```

1. A resposta traz "Checkout em página única" — leitura e conexão por socket funcionam.
2. No Claude Code, peça para registrar uma decisão de teste. Deve voltar `state: proposed`.
   Se voltar "Não consegui identificar sua conta", o token não chegou ou foi recusado (`aud`
   fora de `DM_EXPECTED_AUDIENCE` e do cliente do gcloud, ou `iss` que não é o Google). Se
   voltar "Sua conta não está cadastrada", falta o e-mail em `person`.
3. Em `get_decision` da decisão de teste, `provenance.principal` é o seu nome.

## Divergências em relação a `MCP_TOOLS.md`

- **Transporte e identidade.** IAM do Google Cloud via `gcloud run services proxy`, não OAuth
  2.1. Sem URL pública e sem conector no claude.ai.
- **`attest_url` nulo.** Não há superfície de atestação; o que o agente escreve fica
  `proposed`. O loop decisão → expectativa → revisão não fecha neste corte.
- **`provenance.model` é sempre `unknown`.** O MCP não informa o modelo e o `clientInfo` não
  chega em modo sem estado. O `User-Agent` do cliente vai para `source_ref`
  (`mcp; client=<User-Agent>`).
- **Estado da evidência** derivado da procedência ([ADR 0005](../../docs/adr/0005-estado-da-evidencia.md)).
- **`attach_evidence`: evidência criada por agente não recebe `source_system`/`external_id`;
  identidade de origem vem só da ingestão.** Um par inédito é recusado, de qualquer `kind`;
  um par que já existe é reaproveitado normalmente.
- **`attach_evidence` é anotada idempotente no `MCP_TOOLS.md`, mas evidência nova sem
  identidade de origem é criada de novo a cada chamada.** O vínculo com a decisão não se
  repete; a evidência, sim.
- **`propose_decision` devolve também `reused`.** Com `idempotency_key` já usada, volta o
  registro gravado (estado e `missing` dele), e o conteúdo da chamada nova não é aplicado.
- **Tags na escrita** saem em minúsculas e sem acento, e o que não casar com o padrão do
  contrato de ingestão, `^[a-z0-9][a-z0-9 _./-]*$`, é recusado sem gravar nada.
- **`list_pending_reviews`**: `unattested` não é filtrado por `owner_email`.
- **Busca**: dicionário `simple`, sem radical — "conversões" não encontra "conversão".
  Acento e caixa são ignorados, como no stub, dobrando o texto na hora da consulta; por isso o
  índice GIN de `search` não é usado. Quando o volume pedir, `unaccent` numa coluna gerada com
  índice, via ADR.
- **Busca**: URL e caminho de arquivo viram tokens inteiros no parser do Postgres
  (`checkout.exemplo.com/v2` gera `checkout.exemplo.com/v2`, `checkout.exemplo.com` e `/v2`,
  nunca `checkout`), enquanto o stub partia na pontuação; buscar por uma palavra de dentro
  deles não acha.
- **Busca**: só as 32 primeiras palavras da consulta contam (`MAX_TERMS`).
- **Banco**: todo comando SQL tem teto de 5 s (`statement_timeout`), e a espera por conexão
  livre no pool também; estourou, a ferramenta responde erro interno com ref.
- **Argumentos**: texto com caractere nulo (`\x00`) é recusado antes da ferramenta, em
  qualquer das seis; o Postgres não guarda esse caractere.
- **GET /mcp responde 405: sem sessão, não há fluxo SSE do servidor.** DELETE também, como
  qualquer método que não seja POST (`Allow: POST`): não há sessão a encerrar.
