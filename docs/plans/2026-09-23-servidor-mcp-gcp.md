# Servidor MCP no GCP — plano de implementação

> **Para Claude:** SUB-SKILL OBRIGATÓRIA: use superpowers:executing-plans para executar este plano tarefa por tarefa.

**Objetivo:** servidor MCP de verdade, com as seis ferramentas de `MCP_TOOLS.md` sobre o
Postgres do Cloud SQL, publicado no Cloud Run e acessado pelo Claude Code via IAM.

**Arquitetura:** pacote novo `server/decision_memory/`, ao lado do stub. FastMCP do SDK `mcp`
2.2 em Streamable HTTP sem estado; `psycopg` 3 com pool; identidade lida do ID token que o
Cloud Run repassa sem assinatura; papel de banco `dm_app` que só lê e acrescenta. Design em
[`2026-09-23-servidor-mcp-gcp-design.md`](2026-09-23-servidor-mcp-gcp-design.md).

**Stack:** Python 3.12, `mcp` 2.2, `psycopg` 3.2 + `psycopg-pool`, `uvicorn`, PostgreSQL 16,
Cloud Run, Cloud SQL, Secret Manager.

---

## Antes de começar

- Branch: `feat/servidor-mcp-gcp` (já criada).
- Python 3.12 via `uv`: `uv venv -p 3.12 .venv`. O Python do sistema é 3.9 e não roda o SDK.
- Postgres de teste com Docker. Um banco **cujo nome termina em `_test`** — a suíte recria o
  schema `public` e se recusa a rodar em outro:

```bash
docker run --rm -d --name dm-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=decision_memory_test -p 5432:5432 postgres:16
export DM_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/decision_memory_test
```

Fatos verificados antes de escrever este plano (não reverificar):

- No `mcp` 2.2, o middleware de servidor recebe `ctx.request` (o `Request` do Starlette) em
  HTTP e lê `ctx.request.headers`. O `clientInfo` do `initialize` **não** chega às chamadas em
  modo sem estado.
- `streamable_http_app` rejeita `Host` fora de `localhost` com 421 por padrão (proteção contra
  DNS rebinding). No Cloud Run é preciso desligar via `TransportSecuritySettings`.
- O Cloud Run repassa o `Authorization` ao container **sem a assinatura do token**. Não dá
  para revalidar; lê-se as claims e confere-se `aud` e `iss`.
- Se a requisição traz `X-Serverless-Authorization` e `Authorization`, o Cloud Run confere
  **só o `X-Serverless-Authorization`** e repassa o outro sem conferir
  ([service-to-service](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)).
  Um invocador legítimo poderia forjar um `Authorization` sem assinatura com o e-mail de outra
  pessoa. Havendo `X-Serverless-Authorization`, a identidade vem só dele — ilegível, não há
  identidade; nunca se cai para o `Authorization`.

---

### Tarefa 1: tabela `idempotency_key`

**Arquivos:**
- Modificar: `db/schema.sql` (depois da tabela `provenance`, antes da seção de índices)
- Criar: `db/migrations/2026-09-23-idempotency-key.sql`
- Modificar: `db/test_invariants.sql` (antes do `\echo` final)

**Passo 1: teste da invariante.** Em `db/test_invariants.sql`, antes de `\echo 'Todas as invariantes passaram.'`:

```sql
-- 8. Chave de idempotência é única por pessoa -------------------------------------
INSERT INTO idempotency_key (principal_person_id, key, object_type, object_id)
VALUES ('00000000-0000-0000-0000-000000000001', 'k1', 'decision',
        '00000000-0000-0000-0000-0000000000d1');

DO $$ BEGIN
    INSERT INTO idempotency_key (principal_person_id, key, object_type, object_id)
    VALUES ('00000000-0000-0000-0000-000000000001', 'k1', 'decision',
            '00000000-0000-0000-0000-0000000000d1');
    RAISE EXCEPTION 'FALHOU: chave de idempotência repetida foi aceita';
EXCEPTION WHEN unique_violation THEN NULL;
END $$;
```

**Passo 2: rodar e ver falhar.**

```bash
psql "$DM_TEST_ADMIN_URL" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/test_invariants.sql
```
Esperado: `ERROR: relation "idempotency_key" does not exist`. (Em banco já usado, antes rode
`psql "$DM_TEST_ADMIN_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'`.)

**Passo 3: a tabela.** Em `db/schema.sql`, logo depois do `CREATE TABLE provenance (...)`:

```sql
-- Idempotência de escrita por agente (propose_decision). Reenvio com a mesma
-- chave, pela mesma pessoa, devolve o registro já criado em vez de duplicá-lo.
CREATE TABLE idempotency_key (
    principal_person_id  uuid        NOT NULL REFERENCES person (id),
    key                  text        NOT NULL CHECK (key <> ''),
    object_type          text        NOT NULL CHECK (object_type IN ('decision')),
    object_id            uuid        NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_person_id, key)
);
```

E `db/migrations/2026-09-23-idempotency-key.sql`, para a instância que já existe:

```sql
-- Aplica em banco criado antes de 2026-09-23 o que db/schema.sql já traz.
-- Uso: psql -v ON_ERROR_STOP=1 -f db/migrations/2026-09-23-idempotency-key.sql
BEGIN;

CREATE TABLE IF NOT EXISTS idempotency_key (
    principal_person_id  uuid        NOT NULL REFERENCES person (id),
    key                  text        NOT NULL CHECK (key <> ''),
    object_type          text        NOT NULL CHECK (object_type IN ('decision')),
    object_id            uuid        NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_person_id, key)
);

COMMIT;
```

**Passo 4: rodar e ver passar.** Recrie o schema e rode o comando do passo 2.
Esperado: `Todas as invariantes passaram.`

**Passo 5: commit.**

```bash
git add db/schema.sql db/migrations db/test_invariants.sql
git commit -m "feat(db): chave de idempotência para escrita por agente"
```

---

### Tarefa 2: papel `dm_app` que só lê e acrescenta

**Arquivos:**
- Criar: `db/grants.sql`
- Modificar: `db/test_invariants.sql`
- Modificar: `.github/workflows/ci.yml` (job `database`)
- Modificar: `CLAUDE.md` (seção "Invariantes do banco")

**Passo 1: testes.** Em `db/test_invariants.sql`, depois do bloco 8:

```sql
-- 9. O papel do servidor MCP só lê e acrescenta -----------------------------------
-- Expectativa é da pessoa, na atestação (ADR 0002); o banco garante isso mesmo
-- que o código do servidor erre.
SET ROLE dm_app;

DO $$ BEGIN
    INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric,
                             expected_magnitude, due_on)
    VALUES ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-000000000001',
            0.5, 'conversão', '+1 p.p.', '2030-01-01');
    RAISE EXCEPTION 'FALHOU: dm_app gravou expectativa';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO review (decision_id, due_on)
    VALUES ('00000000-0000-0000-0000-0000000000d1', '2030-01-01');
    RAISE EXCEPTION 'FALHOU: dm_app gravou revisão';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    UPDATE decision SET title = 'x' WHERE id = '00000000-0000-0000-0000-0000000000d1';
    RAISE EXCEPTION 'FALHOU: dm_app alterou decisão';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    DELETE FROM evidence;
    RAISE EXCEPTION 'FALHOU: dm_app apagou evidência';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO person (name, email) VALUES ('Intrusa', 'intrusa@example.com');
    RAISE EXCEPTION 'FALHOU: dm_app cadastrou pessoa';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

-- e acrescenta o que as ferramentas precisam
INSERT INTO tag (name) VALUES ('papel-dm-app');

RESET ROLE;
```

**Passo 2: rodar e ver falhar.**

```bash
psql "$DM_TEST_ADMIN_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
psql "$DM_TEST_ADMIN_URL" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/test_invariants.sql
```
Esperado: `ERROR: role "dm_app" does not exist`.

**Passo 3: `db/grants.sql`.**

```sql
-- Papel do servidor MCP (server/decision_memory). Aplicar depois de schema.sql;
-- rodar de novo é seguro, e necessário depois de criar tabela nova: os GRANT
-- com ON ALL TABLES só alcançam as tabelas que já existem. A senha não fica aqui:
--   ALTER ROLE dm_app PASSWORD '...';
-- e vai para o Secret Manager (ver server/decision_memory/README.md).
--
-- O servidor só lê e acrescenta. Nenhum UPDATE, nenhum DELETE, e nenhuma
-- escrita em expectation, review, person ou project: expectativa e revisão são
-- da pessoa (ADR 0002), e cadastro não é trabalho de agente.

BEGIN;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dm_app') THEN
        CREATE ROLE dm_app LOGIN;
    END IF;
END $$;

-- REVOKE em tabela também revoga os privilégios concedidos por coluna: rodar
-- de novo não deixa sobra.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dm_app;

-- Sem efeito a partir do PostgreSQL 15; no 14 (mínimo do README) fecha o
-- CREATE que PUBLIC tem no schema public por padrão.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO dm_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dm_app;

GRANT INSERT ON
    alternative, decision_evidence, decision_learning, evidence_learning,
    tag, decision_tag, evidence_tag, learning_tag, idempotency_key
TO dm_app;

-- Nestas quatro, INSERT só nas colunas que o agente preenche. Atestação é
-- humana (ADR 0002): attested_by/attested_at, state (que fica no padrão
-- 'proposed') e o vínculo na data da decisão (que o gatilho preenche) ficam de
-- fora, assim como as colunas de importação da evidência. O banco não impede
-- que dm_app grave author_kind = 'human' ou 'import' em provenance: isso
-- continua responsabilidade do servidor.
GRANT INSERT (object_type, object_id, author_kind, principal_person_id, model, source_ref)
    ON provenance TO dm_app;
GRANT INSERT (slug, title, context, description, door, decided_on, decider_person_id,
              project_id)
    ON decision TO dm_app;
GRANT INSERT (summary, recorded_on)
    ON learning TO dm_app;
GRANT INSERT (kind, title, summary, url, source_system, external_id, strength)
    ON evidence TO dm_app;

COMMIT;
```

**Passo 4: rodar e ver passar.**

```bash
psql "$DM_TEST_ADMIN_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
psql "$DM_TEST_ADMIN_URL" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
```
Esperado: `Todas as invariantes passaram.`

**Passo 5: CI e CLAUDE.md.** No job `database` de `.github/workflows/ci.yml`:

```yaml
      - name: Aplicar o schema e rodar as invariantes
        run: psql -v ON_ERROR_STOP=1 -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
```

No `CLAUDE.md`, nos dois blocos de "Invariantes do banco", troque
`-f db/schema.sql -f db/test_invariants.sql` por
`-f db/schema.sql -f db/grants.sql -f db/test_invariants.sql`.

**Passo 6: commit.**

```bash
git add db/grants.sql db/test_invariants.sql .github/workflows/ci.yml CLAUDE.md
git commit -m "feat(db): papel dm_app, que só lê e acrescenta"
```

---

### Tarefa 3: ADR 0005 — estado da evidência vem da procedência

**Arquivos:**
- Criar: `docs/adr/0005-estado-da-evidencia.md`
- Modificar: `README.md` (tabela de ADRs)

**Passo 1: o ADR.** Decisor, Expectativa e Status ficam como abaixo — por regra do projeto,
nunca preenchidos por agente.

```markdown
# ADR 0005 — O estado da evidência vem da procedência

- **Status:** Proposto
- **Data:** 2026-09-23
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível

## Contexto

`search_evidence` devolve `state` em todo item ([`MCP_TOOLS.md`](../../MCP_TOOLS.md)), mas a
tabela `evidence` não tem estado: ele mora em `decision` e `learning`. O stub contornou
respondendo `attested` para tudo. Com o servidor real, `attach_evidence` passa a criar evidência
por agente, e essa evidência precisa aparecer como não atestada.

## Decisão

O estado de uma evidência é derivado de `provenance`: `attested` se existe procedência dela com
`attested_at` preenchido, `proposed` caso contrário.

Em `decision` e `learning`, a coluna `state` continua sendo a fonte. O modelo fica com duas
regras, e isso é deliberado: a alternativa era duplicar a informação de atestação.

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
- Se `decision` e `learning` um dia migrarem para a mesma regra, este ADR é o ponto de partida.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher*
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a preencher*
- **Resultado observado:** *preenchido na revisão*
- **Veredito:** *preenchido na revisão*
- **Lição:** *preenchida na revisão*
```

Confira a âncora `#divergências-em-relação-a-mcp_toolsmd` com `python scripts/check_links.py`;
ajuste se o script apontar outra.

**Passo 2: README.** Na tabela "Decisões de arquitetura", acrescente:

```markdown
| [0005](docs/adr/0005-estado-da-evidencia.md) | O estado da evidência vem da procedência | Proposto |
```

**Passo 3: verificar e commitar.**

```bash
python3 scripts/check_links.py
git add docs/adr/0005-estado-da-evidencia.md README.md
git commit -m "docs(adr): 0005, estado da evidência derivado da procedência"
```

---

### Tarefa 4: esqueleto do pacote, dependências e harness de teste

**Arquivos:**
- Criar: `server/requirements-app.txt`, `server/requirements-app-test.txt`
- Criar: `server/decision_memory/__init__.py`, `server/decision_memory/config.py`, `server/decision_memory/db.py`
- Criar: `server/tests_real/conftest.py`, `server/tests_real/test_app_db.py`

**Passo 1: dependências.** `server/requirements-app.txt`:

```
# Dependências do servidor (server/decision_memory). O stub tem as suas em requirements.txt.
mcp>=2.2,<3
pydantic>=2.7,<3
psycopg[binary]>=3.2,<4
psycopg-pool>=3.2,<4
uvicorn>=0.30,<1
```

`server/requirements-app-test.txt`:

```
-r requirements-app.txt
pytest>=8,<9
```

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r server/requirements-app-test.txt
```

**Passo 2: `server/decision_memory/__init__.py`.**

```python
"""Servidor MCP do decision-memory sobre Postgres.

Não confundir com `decision_memory_stub`, que responde a partir de fixtures e
existe para o experimento de invocação. Design em
docs/plans/2026-09-23-servidor-mcp-gcp-design.md.
"""
```

**Passo 3: `server/decision_memory/config.py`.**

```python
"""Configuração por variáveis de ambiente, todas com prefixo DM_."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_password: str | None
    # Públicos aceitos no ID token (a URL do serviço), além do cliente OAuth do
    # gcloud (identity.GCLOUD_CLIENT_ID). Vazio desliga a checagem, o que só é
    # permitido fora do Cloud Run.
    expected_audiences: tuple[str, ...]
    on_cloud_run: bool


def load() -> Settings:
    audiences = os.environ.get("DM_EXPECTED_AUDIENCE", "")
    settings = Settings(
        database_url=os.environ["DM_DATABASE_URL"],
        database_password=os.environ.get("DM_DATABASE_PASSWORD"),
        expected_audiences=tuple(a.strip() for a in audiences.split(",") if a.strip()),
        on_cloud_run="K_SERVICE" in os.environ,
    )
    if settings.on_cloud_run and not settings.expected_audiences:
        raise RuntimeError(
            "DM_EXPECTED_AUDIENCE é obrigatório no Cloud Run: sem ele o servidor "
            "aceitaria token emitido para qualquer outro serviço. Use a URL do "
            "serviço (https://decision-memory-<número do projeto>.<região>.run.app)."
        )
    return settings


def today() -> date:
    """Data de referência. DM_TODAY deixa os testes determinísticos."""
    override = os.environ.get("DM_TODAY")
    return date.fromisoformat(override) if override else date.today()
```

**Passo 4: `server/decision_memory/db.py`.**

```python
"""Pool de conexões. Cada chamada de ferramenta usa uma conexão e uma transação:
sai com commit se deu certo, com rollback se levantou exceção."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Teto por comando SQL. Uma ferramenta de leitura sobre o volume da v0 leva
# milissegundos; 5 s só estoura com consulta patológica, e aí é melhor cancelar
# do que prender uma das quatro conexões do pool.
STATEMENT_TIMEOUT_MS = 5000
# Espera máxima por uma conexão livre do pool. Mesmo teto do comando: com as
# quatro ocupadas, quem espera mais do que um comando pode durar está numa fila
# que não anda; melhor falhar com o erro genérico do que prender a requisição.
POOL_TIMEOUT_S = STATEMENT_TIMEOUT_MS / 1000


def connection_kwargs(password: str | None = None) -> dict:
    """Parâmetros de conexão que o psycopg junta à URL.

    `options` vai como parâmetro de partida da sessão, e por isso vale para
    qualquer forma de URL — host TCP ou o socket Unix do Cloud SQL
    (`?host=/cloudsql/...`). Se a URL trouxer `options` própria, ela é
    substituída por esta, não combinada: outro `-c` posto na URL se perde.
    """
    kwargs: dict = {"options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"}
    if password:
        kwargs["password"] = password
    return kwargs


def make_pool(url: str, password: str | None = None) -> ConnectionPool:
    kwargs: dict = {"row_factory": dict_row, **connection_kwargs(password)}
    # Cloud SQL e o Auth Proxy derrubam conexão ociosa: o pool testa a conexão
    # antes de entregá-la e descarta as que ficaram paradas mais de 5 minutos.
    return ConnectionPool(url, kwargs=kwargs, min_size=1, max_size=4, open=True,
                          timeout=POOL_TIMEOUT_S, check=ConnectionPool.check_connection,
                          max_idle=300)


def evidence_attested(id_sql: str) -> str:
    """Condição SQL: a evidência `id_sql` está atestada.

    Evidência não tem coluna de estado: está atestada quando alguma procedência
    dela tem attested_at (ADR 0005). A regra mora só aqui; busca, pendências e
    escrita a usam.
    """
    return ("EXISTS (SELECT 1 FROM provenance p WHERE p.object_type = 'evidence' "
            f"AND p.object_id = {id_sql} AND p.attested_at IS NOT NULL)")
```

**Passo 5: harness de teste.** `server/tests_real/conftest.py`:

```python
"""Banco de teste de verdade: schema, grants e fixtures carregados do zero.

Exige DM_TEST_ADMIN_URL apontando para um banco vazio cujo nome termina em
_test, conectado como superusuário. A sessão recria o schema public, então a
suíte se recusa a rodar em qualquer outro banco.

A instância Postgres inteira tem de ser dedicada aos testes: papéis valem para
o cluster todo, e a suíte aplica grants.sql e troca a senha do papel dm_app.
Por isso ela também se recusa a rodar fora da máquina local (localhost,
127.0.0.1, ::1 ou socket Unix) e, já conectada, antes de mexer em qualquer
coisa, confere no próprio servidor que a instância só tem bancos *_test e não
é Cloud SQL — o Auth Proxy escuta em 127.0.0.1 e passaria pela checagem de host.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent
sys.path.insert(0, str(SERVER_DIR))

os.environ["DM_TODAY"] = "2026-09-22"

ADMIN_URL = os.environ.get("DM_TEST_ADMIN_URL")
APP_PASSWORD = "dm_app_test"
ANA = "ana@exemplo.com.br"
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def run(coro):
    return asyncio.run(coro)


def _refuse_shared_instance(admin: psycopg.Connection) -> None:
    """Falha se a instância hospeda outro banco que não de teste, ou se é Cloud SQL."""
    others = [r[0] for r in admin.execute(
        "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"
        " AND datname NOT LIKE '%\\_test' ESCAPE '\\' ORDER BY datname"
    )]
    if others:
        pytest.fail(f"a instância de DM_TEST_ADMIN_URL também hospeda {', '.join(others)}; "
                    "a suíte altera o papel dm_app do cluster e só roda numa instância "
                    "dedicada a testes (só bancos *_test)")
    iam = admin.execute(
        "SELECT current_setting('cloudsql.iam_authentication', true)"
    ).fetchone()[0]
    if iam is not None:
        pytest.fail("DM_TEST_ADMIN_URL aponta para uma instância Cloud SQL (via Auth Proxy?); "
                    "a suíte só roda num Postgres local dedicado a testes")


@pytest.fixture(scope="session")
def app_url() -> str:
    if not ADMIN_URL:
        pytest.skip("defina DM_TEST_ADMIN_URL (ver docs/plans/2026-09-23-servidor-mcp-gcp.md)")
    info = conninfo_to_dict(ADMIN_URL)
    dbname = info.get("dbname", "")
    if not dbname.endswith("_test"):
        pytest.fail(f"DM_TEST_ADMIN_URL aponta para '{dbname}'; a suíte só roda em banco *_test")
    host = info.get("host") or ""
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        pytest.fail(f"DM_TEST_ADMIN_URL aponta para o host '{host}'; a suíte altera o papel "
                    "dm_app do cluster e só roda num Postgres local dedicado a testes")

    from decision_memory import seed

    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        _refuse_shared_instance(admin)
        admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        admin.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        admin.execute((ROOT / "db" / "grants.sql").read_text(encoding="utf-8"))
        admin.execute(f"ALTER ROLE dm_app PASSWORD '{APP_PASSWORD}'")
    with psycopg.connect(ADMIN_URL) as conn:
        seed.load(conn, SERVER_DIR / "fixtures", attested_by_email=ANA)
    return make_conninfo(ADMIN_URL, user="dm_app", password=APP_PASSWORD)


@pytest.fixture(scope="session")
def pool(app_url):
    from decision_memory.db import make_pool

    pool = make_pool(app_url)
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def admin_conn(app_url):
    """Conexão de superusuário, para os testes que mexem no dado por fora."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        yield conn
```

**Passo 6: primeiro teste.** `server/tests_real/test_app_db.py`:

```python
from __future__ import annotations

import json
import logging

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from decision_memory import seed
from decision_memory.db import POOL_TIMEOUT_S, STATEMENT_TIMEOUT_MS, connection_kwargs
from decision_memory.server import with_db


def test_servidor_conecta_como_dm_app(pool):
    with pool.connection() as conn:
        assert conn.execute("SELECT current_user AS u").fetchone()["u"] == "dm_app"


def test_pool_espera_conexao_no_maximo_o_teto_do_comando(pool):
    assert pool.timeout == POOL_TIMEOUT_S == STATEMENT_TIMEOUT_MS / 1000


def test_dm_app_nao_escreve_expectativa(pool):
    # Linha válida (decisão proposta, sem revisão feita): só o privilégio a recusa.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric,"
                " expected_magnitude, due_on) VALUES (%s, %s, 0.7, 'm', '+1 p.p.', '2027-01-01')",
                (seed.fid("dec-push-diario"), seed.fid("p-ana")),
            )


def test_consulta_longa_e_cancelada(pool):
    with pytest.raises(psycopg.errors.QueryCanceled):
        with pool.connection() as conn:
            conn.execute("SELECT pg_sleep(%s)", (STATEMENT_TIMEOUT_MS / 1000 + 1,))


@pytest.mark.parametrize("url", [
    "postgresql://dm_app@/decision_memory?host=/cloudsql/proj:regiao:inst",
    "postgresql://dm_app:x@localhost:5432/decision_memory",
])
def test_timeout_vai_na_conexao_em_qualquer_forma_de_url(url):
    """O pool repassa os kwargs a psycopg.connect, que os junta à URL assim."""
    info = conninfo_to_dict(make_conninfo(url, **connection_kwargs()))
    assert info["options"] == f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
    assert info["dbname"] == "decision_memory"
    assert info.get("host") in ("/cloudsql/proj:regiao:inst", "localhost")


def test_erro_de_banco_nao_vai_ao_log_com_o_dado_da_linha(pool, caplog):
    """O str() do erro do psycopg traz o DETAIL, com o valor da linha (um e-mail,
    aqui). No log vão só o código, a mensagem principal e a restrição."""
    caplog.set_level(logging.INFO, logger="decision_memory")

    def work(conn):
        conn.execute("CREATE TEMP TABLE pessoa_tmp (email text CONSTRAINT pessoa_tmp_email UNIQUE)")
        conn.execute("INSERT INTO pessoa_tmp VALUES ('segredo@exemplo.com')")
        conn.execute("INSERT INTO pessoa_tmp VALUES ('segredo@exemplo.com')")

    with pytest.raises(ToolError) as err:
        with_db(pool, work)
    assert "segredo" not in str(err.value)
    assert "segredo" not in caplog.text
    [line] = [json.loads(r.getMessage()) for r in caplog.records if r.name == "decision_memory"]
    assert line["event"] == "db_error" and line["severity"] == "ERROR"
    assert line["type"] == "UniqueViolation" and line["sqlstate"] == "23505"
    assert line["constraint"] == "pessoa_tmp_email"
    assert "duplicate key" in line["message"] and "error" not in line
    assert line["ref"] in str(err.value)


def test_erro_que_nao_e_de_banco_vai_ao_log_so_com_o_tipo(pool, caplog):
    caplog.set_level(logging.INFO, logger="decision_memory")

    def work(conn):
        raise KeyError("segredo@exemplo.com")

    with pytest.raises(ToolError):
        with_db(pool, work)
    assert "segredo" not in caplog.text
    [line] = [json.loads(r.getMessage()) for r in caplog.records if r.name == "decision_memory"]
    assert (line["event"], line["type"], line["bug"]) == ("internal_error", "KeyError", True)
```

**Passo 7: rodar e ver falhar.**

```bash
.venv/bin/python -m pytest server/tests_real -q
```
Esperado: erro de import em `decision_memory.seed` (tarefa 5).

**Passo 8: commit** (o que existe até aqui; a suíte fica verde na tarefa 5).

```bash
git add server/requirements-app*.txt server/decision_memory server/tests_real
git commit -m "feat(server): esqueleto do servidor real e harness de teste com Postgres"
```

---

### Tarefa 5: carga das fixtures (`decision_memory.seed`)

**Arquivos:**
- Criar: `server/decision_memory/seed.py`
- Criar: `server/tests_real/test_app_seed.py`
- Modificar: `.gitignore`

**Passo 1: testes.** `server/tests_real/test_app_seed.py`:

```python
from __future__ import annotations

import json
import shutil

import psycopg
import pytest

from decision_memory import seed

FIXTURES = seed.DEFAULT_FIXTURES


def _counts(conn):
    tables = ("person", "project", "evidence", "decision", "alternative", "learning",
              "review", "expectation", "provenance", "decision_evidence", "tag",
              "decision_tag", "evidence_tag", "learning_tag", "decision_learning",
              "evidence_learning")
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()[0] for t in tables}


def test_carga_traz_o_corpus_inteiro(admin_conn):
    n = _counts(admin_conn)
    assert n["evidence"] == 12
    assert n["decision"] == 6
    assert n["learning"] == 4
    assert n["review"] == 5
    assert n["person"] >= 4


def test_carga_e_idempotente(admin_conn):
    antes = _counts(admin_conn)
    seed.load(admin_conn, FIXTURES, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_decisao_proposta_continua_sem_expectativa(admin_conn):
    row = admin_conn.execute(
        "SELECT d.state, (SELECT count(*) FROM expectation e WHERE e.decision_id = d.id) AS n "
        "FROM decision d WHERE d.id = %s",
        (seed.fid("dec-push-diario"),),
    ).fetchone()
    assert row == ("proposed", 0)


def test_evidencia_importada_nasce_atestada(admin_conn):
    sem_atestacao = admin_conn.execute(
        "SELECT count(*) FROM evidence e WHERE NOT EXISTS ("
        " SELECT 1 FROM provenance p WHERE p.object_type = 'evidence'"
        " AND p.object_id = e.id AND p.attested_at IS NOT NULL)"
    ).fetchone()[0]
    assert sem_atestacao == 0


def test_conflito_em_outra_chave_unica_levanta(admin_conn, tmp_path):
    # Fixture nova com o e-mail de uma pessoa existente: id novo, e-mail repetido.
    # A carga toda roda numa transação e é desfeita, então o banco não muda.
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    people = json.loads((fixtures / "people.json").read_text(encoding="utf-8"))
    people.append({"id": "p-ana-clone", "name": "Ana Clone", "email": "Ana@Exemplo.com.br "})
    (fixtures / "people.json").write_text(json.dumps(people), encoding="utf-8")

    antes = _counts(admin_conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_atestador_desconhecido_levanta_value_error(admin_conn):
    antes = _counts(admin_conn)
    with pytest.raises(ValueError, match="ninguem@exemplo.com.br"):
        seed.load(admin_conn, FIXTURES, attested_by_email="ninguem@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_csv_de_pessoas_normaliza_e_recusa_linha_vazia(tmp_path):
    ok = tmp_path / "pessoas.csv"
    ok.write_text("name,email\n Eva Lima , Eva@Exemplo.com \n", encoding="utf-8")
    assert seed.read_people(ok) == [("Eva Lima", "eva@exemplo.com")]

    ruim = tmp_path / "pessoas-ruim.csv"
    ruim.write_text("name,email\nEva Lima,eva@exemplo.com\nSem Email,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="linha 3"):
        seed.read_people(ruim)


def _fixtures_com_tag(tmp_path, tag):
    """Cópia das fixtures com uma tag a mais na primeira evidência (mesmo id)."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    evidence = json.loads((fixtures / "evidence.json").read_text(encoding="utf-8"))
    evidence[0]["tags"] = [*evidence[0].get("tags", []), tag]
    (fixtures / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return fixtures, evidence[0]


def test_tags_das_fixtures_sao_normalizadas(admin_conn, tmp_path):
    # " CheckOut " crua violaria o CHECK de tag; dobrada, é a tag que já existe.
    fixtures, ev = _fixtures_com_tag(tmp_path, " CheckOut ")
    assert "checkout" in ev["tags"]
    antes = _counts(admin_conn)
    seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_tag_fora_do_padrao_levanta_value_error(admin_conn, tmp_path):
    fixtures, ev = _fixtures_com_tag(tmp_path, "#growth")
    antes = _counts(admin_conn)
    with pytest.raises(ValueError, match=f"{ev['id']}.*#growth"):
        seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes
```

`admin_conn` é autocommit e devolve tuplas (sem `dict_row`), por isso os índices numéricos.

**Passo 2: rodar e ver falhar.** `.venv/bin/python -m pytest server/tests_real -q` → import de `seed` falha.

**Passo 3: `server/decision_memory/seed.py`.**

```python
"""Carga do corpus de fixtures no Postgres. Idempotente.

Os ids são uuid5 do id da fixture, então a mesma fixture vira sempre o mesmo
registro e rodar de novo não duplica nada. Tudo entra com author_kind =
'import'; o que está atestado nas fixtures recebe attested_by/attested_at.

Uso (com o Cloud SQL Auth Proxy na porta 5433):

    PGPASSWORD=... python -m decision_memory.seed \\
        --database-url "host=127.0.0.1 port=5433 dbname=decision_memory user=dm_admin" \\
        --attested-by voce@exemplo.com --people pessoas.csv

`--people` é um CSV com cabeçalho `name,email`. Tem e-mail de gente real: não
versione.

Só o conflito de id é tolerado (é o que torna a carga idempotente). Colisão em
outra chave única — um e-mail já cadastrado com outro id, uma evidência com o
mesmo (source_system, external_id) — levanta erro e desfaz a carga inteira.
"""

from __future__ import annotations

import argparse
import csv
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.types.json import Jsonb

from .writes import normalize_tags

NAMESPACE = uuid.UUID("7f1b0c3e-5d2a-4e8b-9c61-2a4f3e9d8b10")
DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fid(fixture_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"fixtures:{fixture_id}")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def person_id(email: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"person:{normalize_email(email)}")


def read_people(path: Path) -> list[tuple[str, str]]:
    """Lê o CSV `name,email` de pessoas reais. Linha sem nome ou sem e-mail é erro."""
    people: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        # Linha 1 é o cabeçalho.
        for line, row in enumerate(csv.DictReader(handle), start=2):
            name = (row.get("name") or "").strip()
            email = normalize_email(row.get("email") or "")
            if not name or not email:
                raise ValueError(f"{path}, linha {line}: nome e e-mail são obrigatórios")
            people.append((name, email))
    return people


def _read(fixtures: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((fixtures / f"{name}.json").read_text(encoding="utf-8"))


def _provenance(cur, object_type: str, object_id: uuid.UUID, fixture_id: str,
                attested_by: uuid.UUID | None) -> None:
    cur.execute(
        """
        INSERT INTO provenance (object_type, object_id, author_kind, source_ref,
                                attested_by, attested_at)
        SELECT %(t)s, %(o)s, 'import', %(ref)s, %(by)s,
               CASE WHEN %(by)s::uuid IS NULL THEN NULL ELSE now() END
         WHERE NOT EXISTS (SELECT 1 FROM provenance
                            WHERE object_type = %(t)s AND object_id = %(o)s
                              AND source_ref = %(ref)s)
        """,
        {"t": object_type, "o": object_id, "ref": f"fixtures:{fixture_id}", "by": attested_by},
    )


def _tags(cur, link_table: str, fk: str, object_id: uuid.UUID, fixture_id: str,
          names: list[str]) -> None:
    """Tags normalizadas como na escrita por agente (writes.normalize_tags): a
    mesma dobra, e fora do padrão do contrato é erro, que desfaz a carga."""
    try:
        names = normalize_tags(names)
    except ToolError as exc:
        raise ValueError(f"{fixture_id}: {exc}") from None
    for name in names:
        cur.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        cur.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING",
            (object_id, name),
        )


def load(conn: psycopg.Connection, fixtures: Path = DEFAULT_FIXTURES, *,
         attested_by_email: str, extra_people: list[tuple[str, str]] = ()) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for p in _read(fixtures, "people"):
            cur.execute(
                "INSERT INTO person (id, name, email) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (fid(p["id"]), p["name"], normalize_email(p["email"])),
            )
        for name, email in extra_people:
            cur.execute(
                "INSERT INTO person (id, name, email) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (person_id(email), name, normalize_email(email)),
            )
        row = cur.execute(
            "SELECT id FROM person WHERE lower(email) = lower(%s)", (attested_by_email,)
        ).fetchone()
        if row is None:
            raise ValueError(f"atestador {attested_by_email}: pessoa não cadastrada")
        attester = row[0]

        for p in _read(fixtures, "projects"):
            cur.execute(
                "INSERT INTO project (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (fid(p["id"]), p["name"]),
            )

        for e in _read(fixtures, "evidence"):
            cur.execute(
                """
                INSERT INTO evidence (id, kind, title, summary, url, source_system, external_id,
                                      strength, conformance_level, normalized, imported_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO NOTHING
                """,
                (fid(e["id"]), e["kind"], e["title"], e.get("summary"), e.get("url"),
                 e.get("source_system"), e.get("external_id"), e.get("strength"),
                 e.get("conformance_level"), Jsonb(e["record"]) if e.get("record") else None),
            )
            _tags(cur, "evidence_tag", "evidence_id", fid(e["id"]), e["id"], e.get("tags", []))
            _provenance(cur, "evidence", fid(e["id"]), e["id"], attester)

        decisions = _read(fixtures, "decisions")
        for d in decisions:
            did = fid(d["id"])
            cur.execute(
                """
                INSERT INTO decision (id, slug, title, context, description, door, decided_on,
                                      decider_person_id, project_id, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (did, d["slug"], d["title"], d.get("context"), d["description"], d["door"],
                 d["decided_on"], fid(d["decider"]),
                 fid(d["project"]) if d.get("project") else None, d["state"]),
            )
            for i, alt in enumerate(d.get("alternatives", [])):
                cur.execute(
                    "INSERT INTO alternative (id, decision_id, description, rejection_reason) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (fid(f"{d['id']}:alt:{i}"), did, alt["description"], alt["rejection_reason"]),
                )
            for link in d.get("evidence", []):
                cur.execute(
                    "INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (did, fid(link["evidence_id"]), link["role"], link.get("weight"),
                     link.get("note")),
                )
            _tags(cur, "decision_tag", "decision_id", did, d["id"], d.get("tags", []))
            _provenance(cur, "decision", did, d["id"],
                        attester if d["state"] == "attested" else None)

        # Expectativas antes das revisões: o banco recusa expectativa depois de
        # revisão realizada. INSERT ... WHERE NOT EXISTS, e não ON CONFLICT, porque
        # o gatilho BEFORE INSERT dispara antes do teste de conflito — numa
        # segunda carga ele recusaria a linha que já existe.
        for d in decisions:
            x = d.get("expectation")
            if not x:
                continue
            cur.execute(
                """
                INSERT INTO expectation (decision_id, recorded_at, recorded_by, confidence,
                                         expected_metric, expected_magnitude, due_on)
                SELECT %(d)s, %(at)s, %(by)s, %(c)s, %(m)s, %(mag)s, %(due)s
                 WHERE NOT EXISTS (SELECT 1 FROM expectation
                                    WHERE decision_id = %(d)s AND recorded_at = %(at)s)
                """,
                {"d": fid(d["id"]), "at": x["recorded_at"], "by": fid(x["recorded_by"]),
                 "c": x["confidence"], "m": x["expected_metric"],
                 "mag": x["expected_magnitude"], "due": x["due_on"]},
            )

        for ln in _read(fixtures, "learnings"):
            lid = fid(ln["id"])
            cur.execute(
                "INSERT INTO learning (id, summary, recorded_on, state) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (lid, ln["summary"], ln["recorded_on"], ln["state"]),
            )
            for dec in ln.get("from_decisions", []):
                cur.execute(
                    "INSERT INTO decision_learning (decision_id, learning_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (fid(dec), lid),
                )
            for ev in ln.get("from_evidence", []):
                cur.execute(
                    "INSERT INTO evidence_learning (evidence_id, learning_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (fid(ev), lid),
                )
            _tags(cur, "learning_tag", "learning_id", lid, ln["id"], ln.get("tags", []))
            _provenance(cur, "learning", lid, ln["id"],
                        attester if ln["state"] == "attested" else None)

        deciders = {fid(d["id"]): fid(d["decider"]) for d in decisions}
        for r in _read(fixtures, "reviews"):
            did = fid(r["decision_id"])
            cur.execute(
                "INSERT INTO review (id, decision_id, due_on, done_on, verdict, notes, reviewed_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (fid(r["id"]), did, r["due_on"], r.get("done_on"), r.get("verdict"),
                 r.get("notes"), deciders[did] if r.get("done_on") else None),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--attested-by", required=True,
                        help="e-mail de quem atesta a importação; precisa estar cadastrado")
    parser.add_argument("--people", type=Path, help="CSV name,email com as pessoas reais")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args()

    try:
        extra = read_people(args.people) if args.people else []
        with psycopg.connect(args.database_url) as conn:
            load(conn, args.fixtures, attested_by_email=args.attested_by, extra_people=extra)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except psycopg.errors.UniqueViolation as exc:
        detail = exc.diag.message_detail or exc.diag.message_primary
        raise SystemExit(f"carga desfeita: conflito na chave única "
                         f"{exc.diag.constraint_name} ({detail})") from exc
    print(f"Carga concluída ({len(extra)} pessoa(s) além das fixtures).")


if __name__ == "__main__":
    main()
```

**Passo 4: rodar e ver passar.**

```bash
.venv/bin/python -m pytest server/tests_real -q
```
Esperado: 9 passed.

**Passo 5: `.gitignore`.** Acrescente:

```
# Pessoas reais para a carga (decision_memory.seed --people): e-mail não vai para o git
pessoas*.csv
```

**Passo 6: commit.**

```bash
git add server/decision_memory/seed.py server/tests_real/test_app_seed.py .gitignore
git commit -m "feat(server): carga idempotente das fixtures no Postgres"
```

---

### Tarefa 6: identidade e guarda contra expectativa

**Arquivos:**
- Criar: `server/decision_memory/identity.py`, `server/decision_memory/guard.py`
- Criar: `server/tests_real/test_app_identity.py`, `server/tests_real/test_app_config.py`
- Modificar: `server/decision_memory/config.py`

**Passo 1: testes.** `server/tests_real/test_app_identity.py`:

```python
from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace

import mcp.types as t
from conftest import run

from decision_memory.guard import forbidden_keys, nul_paths, refuse_nul
from decision_memory.identity import (
    GCLOUD_CLIENT_ID,
    acting_as,
    current_client,
    current_email,
    email_from_authorization,
    email_from_headers,
)


def token(claims: dict, iss: str | None = "https://accounts.google.com") -> str:
    """ID token como o Cloud Run entrega ao container: sem assinatura."""
    if iss is not None:
        claims = {"iss": iss, **claims}
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")  # noqa: E731
    return f"Bearer {enc({'alg': 'RS256'})}.{enc(claims)}."


AUD = "https://decision-memory-abc-rj.a.run.app"


def test_le_o_email_do_token():
    assert (
        email_from_authorization(token({"email": "Ana@Exemplo.com.br", "aud": AUD}), (AUD,))
        == "ana@exemplo.com.br"
    )


def test_sem_header_nao_ha_identidade():
    assert email_from_authorization(None, (AUD,)) is None
    assert email_from_authorization("Basic xyz", (AUD,)) is None
    assert email_from_authorization("Bearer lixo", (AUD,)) is None


def test_payload_ilegivel_nao_ha_identidade():
    assert email_from_authorization("Bearer a.%%%.b", (AUD,)) is None
    lista = base64.urlsafe_b64encode(b'["a@b.c"]').decode().rstrip("=")
    assert email_from_authorization(f"Bearer a.{lista}.", (AUD,)) is None


def test_publico_errado_nao_ha_identidade():
    assert email_from_authorization(token({"email": "a@b.c", "aud": "https://outro"}), (AUD,)) is None


def test_publico_do_cliente_do_gcloud_e_aceito():
    """O token que o gcloud emite para conta de usuário (proxy, print-identity-token)
    traz como aud o id do cliente OAuth do próprio gcloud, não a URL do serviço."""
    assert GCLOUD_CLIENT_ID == "32555940559.apps.googleusercontent.com"
    claims = {"email": "ana@x.com", "aud": GCLOUD_CLIENT_ID, "azp": GCLOUD_CLIENT_ID,
              "email_verified": True}
    assert email_from_authorization(token(claims), (AUD,)) == "ana@x.com"
    assert email_from_headers({"authorization": token(claims)}, (AUD,)) == "ana@x.com"


def test_outro_cliente_oauth_nao_e_aceito():
    claims = {"email": "ana@x.com", "aud": "123-outro.apps.googleusercontent.com"}
    assert email_from_authorization(token(claims), (AUD,)) is None


def test_publico_em_lista():
    assert (
        email_from_authorization(token({"email": "a@b.c", "aud": ["https://outro", AUD]}), (AUD,))
        == "a@b.c"
    )
    assert email_from_authorization(token({"email": "a@b.c", "aud": ["https://outro"]}), (AUD,)) is None
    assert email_from_authorization(token({"email": "a@b.c", "aud": []}), (AUD,)) is None


def test_sem_publico_configurado_nao_confere():
    assert email_from_authorization(token({"email": "a@b.c", "aud": "x"}), ()) == "a@b.c"


def test_email_nao_verificado_nao_vale():
    claims = {"email": "a@b.c", "aud": AUD, "email_verified": False}
    assert email_from_authorization(token(claims), (AUD,)) is None


def test_emissor_do_google_nas_duas_grafias():
    for iss in ("accounts.google.com", "https://accounts.google.com"):
        assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, iss), (AUD,)) == "a@b.c"


def test_emissor_estranho_ou_ausente_nao_ha_identidade():
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, "https://evil"), (AUD,)) is None
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, None), (AUD,)) is None


def test_email_verified_ausente_vale():
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}), (AUD,)) == "a@b.c"


def test_so_authorization():
    headers = {"authorization": token({"email": "ana@x.com", "aud": AUD})}
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_so_x_serverless_authorization():
    headers = {"x-serverless-authorization": token({"email": "ana@x.com", "aud": AUD})}
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_com_os_dois_vale_o_que_o_cloud_run_verificou():
    """Com os dois, o Cloud Run só confere o X-Serverless; o Authorization pode ser forjado."""
    headers = {
        "x-serverless-authorization": token({"email": "ana@x.com", "aud": AUD}),
        "authorization": token({"email": "vitima@x.com", "aud": AUD}),
    }
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_x_serverless_ilegivel_nao_cai_para_authorization():
    headers = {
        "x-serverless-authorization": "Bearer lixo",
        "authorization": token({"email": "vitima@x.com", "aud": AUD}),
    }
    assert email_from_headers(headers, (AUD,)) is None


def test_sem_headers_nao_ha_identidade():
    assert email_from_headers({}, (AUD,)) is None


def test_acting_as_poe_e_restaura_identidade():
    assert current_email() is None and current_client() is None
    with acting_as("ana@x.com", "claude-code"):
        assert (current_email(), current_client()) == ("ana@x.com", "claude-code")
        with acting_as("bia@x.com"):
            assert (current_email(), current_client()) == ("bia@x.com", "tests")
        assert (current_email(), current_client()) == ("ana@x.com", "claude-code")
    assert current_email() is None and current_client() is None


def test_campos_de_expectativa_sao_detectados():
    args = {
        "decision_id": "d1",
        "confidence": 0.8,
        "confiança": "alta",
        "resultado_esperado": "sobe",
        "Expectativa": "x",
    }
    assert forbidden_keys(args) == sorted(
        ["confidence", "confiança", "resultado_esperado", "Expectativa"]
    )


def test_campos_aninhados_sao_detectados():
    assert forbidden_keys({"meta": {"confidence": 0.8}}) == ["meta.confidence"]
    assert forbidden_keys({"alternatives": [{"expectativa": "x"}, {"ok": 1}]}) == [
        "alternatives[0].expectativa"
    ]
    # Só chaves: valor com o termo não é recusado.
    assert forbidden_keys({"summary": "alta confiança", "tags": ["confidence"]}) == []


def test_argumentos_limpos_ou_nao_dict_passam():
    assert forbidden_keys({"decision_id": "d1", "query": "preço"}) == []
    assert forbidden_keys(None) == []
    assert forbidden_keys(["confidence"]) == []
    assert forbidden_keys("confidence") == []


def _through(middleware, arguments):
    ctx = SimpleNamespace(method="tools/call", params={"name": "x", "arguments": arguments})

    async def call_next(_ctx):
        return "chamou a ferramenta"

    return run(middleware(ctx, call_next))


def test_caractere_nulo_e_recusado_antes_da_ferramenta():
    for args in ({"query": "a\x00b"}, {"tags": ["ok", "x\x00"]},
                 {"meta": {"nota": "\x00"}}, {"chave\x00": "valor"}):
        res = _through(refuse_nul, args)
        assert isinstance(res, t.CallToolResult) and res.is_error, args
        assert "\\x00" in res.content[0].text and "Nada foi gravado" in res.content[0].text


def test_sem_caractere_nulo_segue_para_a_ferramenta():
    assert _through(refuse_nul, {"query": "checkout", "limit": 3, "tags": None}) \
        == "chamou a ferramenta"
    assert nul_paths({"a": ["x", {"b": "y\x00"}]}) == ["a[1].b"]


# ------------------------------------------------------- log de recusa de identidade


def _rejections(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "decision_memory" and "identity_rejected" in r.getMessage()]


def test_recusa_de_identidade_gera_uma_linha_sem_email_nem_token(caplog):
    caplog.set_level(logging.INFO, logger="decision_memory")
    bearer = token({"email": "ana@x.com", "aud": "https://outro"})
    assert email_from_headers({"authorization": bearer}, (AUD,)) is None
    [line] = _rejections(caplog)
    assert line == {"severity": "WARNING", "event": "identity_rejected", "reason": "bad_aud",
                    "header": "authorization", "iss": "https://accounts.google.com",
                    "aud": "https://outro"}
    text = caplog.text
    assert "ana@x.com" not in text and bearer[7:] not in text
    assert all(r.levelno == logging.WARNING for r in caplog.records)


def test_motivos_da_recusa(caplog):
    caplog.set_level(logging.INFO, logger="decision_memory")
    casos = {
        "no_bearer": "Basic xyz",
        "unreadable": "Bearer lixo",
        "bad_iss": token({"email": "a@b.c", "aud": AUD}, "https://evil"),
        "bad_aud": token({"email": "a@b.c", "aud": ["https://x", "https://y"]}),
        "unverified": token({"email": "a@b.c", "aud": AUD, "email_verified": False}),
        "no_email": token({"aud": AUD}),
    }
    for reason, header in casos.items():
        caplog.clear()
        assert email_from_headers({"x-serverless-authorization": header}, (AUD,)) is None
        [line] = _rejections(caplog)
        assert (line["reason"], line["header"]) == (reason, "x-serverless-authorization")
        assert "a@b.c" not in caplog.text
    # O aud recusado vai como veio (lista), o iss também.
    caplog.clear()
    email_from_headers({"authorization": casos["bad_aud"]}, (AUD,))
    assert _rejections(caplog)[0]["aud"] == ["https://x", "https://y"]


def test_sem_header_ou_com_identidade_aceita_nao_ha_log(caplog):
    caplog.set_level(logging.INFO, logger="decision_memory")
    assert email_from_headers({}, (AUD,)) is None
    assert email_from_headers({"authorization": token({"email": "a@b.c", "aud": AUD})},
                              (AUD,)) == "a@b.c"
    assert _rejections(caplog) == []
```

**Passo 2: rodar e ver falhar.** `.venv/bin/python -m pytest server/tests_real/test_app_identity.py -q` → import falha.

**Passo 3: `server/decision_memory/identity.py`.**

```python
"""Quem está chamando.

Com o Cloud Run fechado (--no-allow-unauthenticated), a plataforma valida o ID
token do Google e repassa o header ao container SEM a assinatura
(https://docs.cloud.google.com/run/docs/troubleshooting). Não há como
revalidar: o servidor confia na validação da plataforma, lê as claims e confere
público e emissor. Isso só é seguro com o serviço fechado — ver README, "Deploy".

O público aceito é a URL do serviço (DM_EXPECTED_AUDIENCE) ou o cliente OAuth
do gcloud (GCLOUD_CLIENT_ID): é esse o `aud` do token de conta de usuário que
o `gcloud run services proxy` injeta.

Qual header a plataforma validou: se vierem `X-Serverless-Authorization` e
`Authorization`, o Cloud Run confere SÓ o primeiro e repassa o segundo intacto
(https://docs.cloud.google.com/run/docs/authenticating/service-to-service).
Quem tem acesso ao serviço poderia então mandar o próprio token no
X-Serverless e um `Authorization` forjado, sem assinatura, com o e-mail de
outra pessoa. Por isso, havendo X-Serverless-Authorization, a identidade vem
só dele — e, se ele for ilegível, não há identidade; nunca se cai para o
Authorization.

A identidade viaja num ContextVar posto pela middleware antes de a ferramenta
rodar. Testes que chamam a ferramenta direto usam `acting_as`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

# O logger do pacote: o "mcp" fica em WARNING (app.build_app), este não.
log = logging.getLogger("decision_memory")

_email: ContextVar[str | None] = ContextVar("dm_principal_email", default=None)
_client: ContextVar[str | None] = ContextVar("dm_client", default=None)

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Id público do cliente OAuth do gcloud CLI. O ID token que o gcloud emite para
# conta de usuário — o do `gcloud run services proxy` e o de `gcloud auth
# print-identity-token` — traz esse id como `aud` (e como `azp`), não a URL do
# serviço; sem ele na lista, toda pessoa pelo proxy ficaria sem identidade. O
# Cloud Run já conferiu o público na borda, antes de a requisição chegar aqui:
# esta checagem no servidor é defesa em profundidade, não a barreira.
GCLOUD_CLIENT_ID = "32555940559.apps.googleusercontent.com"


def _audience_ok(aud: object, audiences: tuple[str, ...]) -> bool:
    """`aud` de um JWT pode ser string ou lista; basta um elemento conferir.

    Com `audiences` configurado, vale também o cliente do gcloud.
    """
    if not audiences:
        return True
    accepted = (*audiences, GCLOUD_CLIENT_ID)
    if isinstance(aud, str):
        return aud in accepted
    if isinstance(aud, list):
        return any(isinstance(a, str) and a in accepted for a in aud)
    return False


# Quanto do aud/iss recusado vai ao log: são valores de quem chama.
MAX_LOGGED = 200


def _check(header: str | None, audiences: tuple[str, ...]) -> tuple[str | None, str | None, dict]:
    """(e-mail, motivo da recusa, claims). Sem header, nem e-mail nem motivo."""
    if header is None:
        return None, None, {}
    if not header.lower().startswith("bearer "):
        return None, "no_bearer", {}
    parts = header[7:].strip().split(".")
    if len(parts) < 2:
        return None, "unreadable", {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None, "unreadable", {}
    if not isinstance(claims, dict):
        return None, "unreadable", {}
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None, "bad_iss", claims
    if not _audience_ok(claims.get("aud"), audiences):
        return None, "bad_aud", claims
    # Só False recusa: a claim falta em alguns tokens federados, e o header já
    # foi verificado pela plataforma.
    if claims.get("email_verified") is False:
        return None, "unverified", claims
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "no_email", claims
    return email.strip().lower(), None, claims


def email_from_authorization(header: str | None, audiences: tuple[str, ...]) -> str | None:
    """E-mail do ID token no header, ou None se não houver identidade aceitável.

    Sem `audiences` configurado, o público não é conferido; com ele, vale
    também GCLOUD_CLIENT_ID. O emissor tem de ser o Google sempre.
    """
    return _check(header, audiences)[0]


def _clip(value: object) -> object:
    """aud/iss como vieram, cortados: texto, lista de textos ou só o nome do tipo."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_LOGGED]
    if isinstance(value, list):
        return [v[:MAX_LOGGED] if isinstance(v, str) else type(v).__name__ for v in value[:5]]
    return type(value).__name__


def _log_rejection(reason: str, header_name: str, claims: dict) -> None:
    """Uma linha JSON por recusa, para diagnosticar o deploy. Nunca o e-mail nem o
    token: só o motivo, o header e os valores de aud e iss que não serviram."""
    log.warning(json.dumps({"severity": "WARNING", "event": "identity_rejected",
                            "reason": reason, "header": header_name,
                            "iss": _clip(claims.get("iss")), "aud": _clip(claims.get("aud"))}))


def email_from_headers(headers: Mapping[str, str], audiences: tuple[str, ...]) -> str | None:
    """E-mail do header que o Cloud Run verificou.

    Com X-Serverless-Authorization presente, só ele conta; o Authorization é
    ignorado, porque nesse caso a plataforma não o conferiu. Header presente e
    recusado deixa uma linha `identity_rejected` no log; header ausente, não.
    """
    name = ("x-serverless-authorization" if headers.get("x-serverless-authorization") is not None
            else "authorization")
    email, reason, claims = _check(headers.get(name), audiences)
    if reason is not None:
        _log_rejection(reason, name, claims)
    return email


def current_email() -> str | None:
    return _email.get()


def current_client() -> str | None:
    return _client.get()


@contextmanager
def acting_as(email: str | None, client: str | None = "tests") -> Iterator[None]:
    """Põe a identidade para o bloco e restaura a anterior ao sair."""
    t1, t2 = _email.set(email), _client.set(client)
    try:
        yield
    finally:
        _email.reset(t1)
        _client.reset(t2)


def middleware(audiences: tuple[str, ...]):
    """Middleware do servidor MCP que resolve a identidade de cada requisição."""

    async def resolve_identity(ctx, call_next):
        request = getattr(ctx, "request", None)
        headers = request.headers if request is not None else {}
        email = email_from_headers(headers, audiences)
        # O cliente (User-Agent) é controlado por quem chama: serve só para
        # telemetria, nunca para autorização.
        with acting_as(email, headers.get("user-agent")):
            return await call_next(ctx)

    return resolve_identity
```

**Passo 4: `server/decision_memory/guard.py`** — a recusa de expectativa, portada do stub.

```python
"""Recusa confiança ou expectativa vinda de agente, sob qualquer nome.

Nenhuma ferramenta tem parâmetro assim; se chegar um, a resposta é recusa
explícita, como resultado de ferramenta com is_error — não erro de protocolo —
para o agente ler a mensagem e seguir sem o campo.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

import mcp.types as t

FORBIDDEN_ARGUMENT_TERMS = (
    "confidence",
    "confianca",
    "expectation",
    "expectativa",
    "expected_metric",
    "expected_magnitude",
    "resultado_esperado",
    "magnitude_esperada",
    "certeza",
)

EXPECTATION_REFUSAL = (
    "Expectativa é registrada pela pessoa na atestação, não pelo agente. Siga sem ela."
)


def fold(text: str) -> str:
    """Minúsculas e sem acento, para `confiança` casar com `confianca`."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def forbidden_keys(arguments: Any) -> list[str]:
    """Caminhos das chaves que carregam confiança ou expectativa, ordenados.

    Desce por dicionários e listas aninhados; olha só chaves, não valores. Chave
    no topo sai pelo nome (`confidence`); aninhada, pelo caminho
    (`meta.confidence`, `alternatives[0].expectativa`).
    """
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if any(term in fold(str(key)) for term in FORBIDDEN_ARGUMENT_TERMS):
                    found.add(path)
                walk(value, path)
        elif isinstance(node, list | tuple):
            for index, item in enumerate(node):
                walk(item, f"{prefix}[{index}]")

    if isinstance(arguments, Mapping):
        walk(arguments, "")
    return sorted(found)


async def refuse_expectation(ctx, call_next):
    """Middleware: recusa `tools/call` com argumento proibido antes da ferramenta."""
    if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
        offending = forbidden_keys(ctx.params.get("arguments") or {})
        if offending:
            text = " ".join(
                (
                    EXPECTATION_REFUSAL,
                    f"Campos recusados: {', '.join(offending)}.",
                    "Chame de novo sem eles.",
                )
            )
            return t.CallToolResult(
                is_error=True, content=[t.TextContent(type="text", text=text)]
            )
    return await call_next(ctx)


NUL_REFUSAL = (
    "Parâmetro inválido: texto com caractere nulo (\\x00). Nada foi gravado."
)


def nul_paths(arguments: Any) -> list[str]:
    """Caminhos das chaves ou valores de texto que contêm o caractere nulo.

    O Postgres não guarda \\x00 em text. Tirá-lo em silêncio mudaria o dado;
    recusar com a mensagem certa deixa o agente corrigir e chamar de novo.
    """
    found: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if "\x00" in node:
                found.add(path or "(argumento)")
        elif isinstance(node, Mapping):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and "\x00" in key:
                    found.add(child.replace("\x00", "\\x00"))
                walk(value, child.replace("\x00", "\\x00"))
        elif isinstance(node, list | tuple):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(arguments, "")
    return sorted(found)


async def refuse_nul(ctx, call_next):
    """Middleware: recusa `tools/call` com \\x00 em qualquer texto dos argumentos.

    Vale para as seis ferramentas de uma vez: é na fronteira, antes de qualquer
    SQL, que o erro ainda pode ser explicado ao agente.
    """
    if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
        offending = nul_paths(ctx.params.get("arguments") or {})
        if offending:
            text = f"{NUL_REFUSAL} Campos: {', '.join(offending)}. Chame de novo sem ele."
            return t.CallToolResult(
                is_error=True, content=[t.TextContent(type="text", text=text)]
            )
    return await call_next(ctx)
```

**Passo 5: rodar e ver passar.** Esperado: todos passam.

**Passo 5b: `DM_EXPECTED_AUDIENCE` obrigatório no Cloud Run.** Teste em `server/tests_real/test_app_config.py` (com `K_SERVICE` e sem o público, `config.load()` levanta `RuntimeError`) e a checagem em `config.load` — já refletida no bloco da Tarefa 4.

**Passo 6: commit.**

```bash
git add server/decision_memory/identity.py server/decision_memory/guard.py server/tests_real/test_app_identity.py
git commit -m "feat(server): identidade pelo token do Cloud Run e recusa de expectativa"
```

---

### Tarefa 7: modelos de saída e bloco `pending`

**Arquivos:**
- Criar: `server/decision_memory/models.py`
- Criar: `server/decision_memory/pending.py`

**Passo 1: `models.py`.** Copie `server/decision_memory_stub/models.py` inteiro e faça só estas
mudanças (o README do stub diz que nada dele é base do servidor além de descrições e corpus;
os modelos são o formato de `MCP_TOOLS.md`, por isso a cópia):

```python
class ProposeData(BaseModel):
    decision_id: str
    slug: str
    state: str = "proposed"
    attest_url: str | None = Field(
        None, description="Nulo enquanto não houver superfície de atestação."
    )
    missing: list[str] = Field(
        default_factory=list, description="Campos que tornariam o registro útil."
    )


class AttachData(BaseModel):
    decision_id: str
    evidence_id: str
    role: str
    reused_existing: bool = Field(
        False, description="true quando (source_system, external_id) já existia."
    )
    already_linked: bool = Field(
        False, description="true quando a evidência já estava vinculada a essa decisão; nada mudou."
    )
    state: str = Field("proposed", description="Estado da evidência, derivado da procedência.")
```

Troque também a docstring do módulo para dizer que é do servidor real.

**Passo 2: `pending.py`.**

```python
"""O bloco `pending`: o lembrete que viaja junto de toda resposta.

É a mitigação nº 1 do ADR 0002: o servidor não controla quando é chamado, então
o lembrete vai na resposta. Curto, específico e ligado ao que acabou de
acontecer. Vem vazio, nunca ausente.
"""

from __future__ import annotations

from typing import Any

from .config import today
from .db import evidence_attested
from .models import Pending, ReviewDue

MAX_REVIEWS_DUE = 3

# Evidência não tem coluna de estado: a regra do ADR 0005 está em db.py.
UNATTESTED_COUNT_SQL = f"""
SELECT (SELECT count(*) FROM decision WHERE state = 'proposed')
     + (SELECT count(*) FROM learning WHERE state = 'proposed')
     + (SELECT count(*) FROM evidence e WHERE NOT {evidence_attested('e.id')}) AS n
"""

# Só revisões já vencidas: as que ainda não venceram não são cobrança, são ruído.
OVERDUE_SQL = """
SELECT d.id::text AS decision_id, d.title, r.due_on,
       ARRAY(SELECT t.name FROM decision_tag dt JOIN tag t ON t.id = dt.tag_id
              WHERE dt.decision_id = d.id) AS tags
  FROM review r JOIN decision d ON d.id = r.decision_id
 WHERE r.done_on IS NULL AND r.due_on <= %(today)s
"""


def _hint(due: list[ReviewDue], unattested: int, shared_tags: set[str]) -> str | None:
    """Uma frase para o agente.

    `shared_tags` são as tags do contexto que alguma revisão vencida também tem.
    Diferente do stub, que citava a primeira tag do contexto mesmo sem relação
    com as revisões, aqui só se cita tag que de fato liga as duas coisas.
    """
    if due:
        tag = next(iter(sorted(shared_tags)), None)
        escopo = f" relacionada à tag {tag}" if tag else ""
        plural = "revisões vencidas" if len(due) > 1 else "revisão vencida"
        return f"Há {len(due)} {plural}{escopo}. Mencione ao usuário antes de prosseguir."
    if unattested:
        return (
            f"{unattested} registros aguardam atestação de uma pessoa. "
            "Confiança e resultado esperado são preenchidos nesse momento, não aqui."
        )
    return None


def build(
    conn: Any,
    *,
    context_tags: set[str] | None = None,
    on_this_record: list[str] | None = None,
) -> Pending:
    """Monta o bloco. `conn` usa dict_row, como as conexões do pool."""
    context_tags = context_tags or set()
    reference = today()
    scored: list[tuple[int, int, str, ReviewDue, set[str]]] = []
    for row in conn.execute(OVERDUE_SQL, {"today": reference}).fetchall():
        shared = context_tags & set(row["tags"])
        overdue = (reference - row["due_on"]).days
        review = ReviewDue(
            decision_id=row["decision_id"],
            title=row["title"],
            due_on=row["due_on"].isoformat(),
            overdue_days=overdue,
        )
        scored.append((1 if shared else 0, overdue, row["decision_id"], review, shared))
    # Quem compartilha tag primeiro, depois a mais vencida; o id desempata para
    # que a ordem não dependa do plano de execução.
    scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
    top = scored[:MAX_REVIEWS_DUE]
    due = [r[3] for r in top]
    shared_tags = set().union(*(r[4] for r in top))
    unattested = conn.execute(UNATTESTED_COUNT_SQL).fetchone()["n"]
    return Pending(
        on_this_record=on_this_record or [],
        reviews_due=due,
        unattested_count=unattested,
        hint=_hint(due, unattested, shared_tags),
    )
```

Sem teste próprio: é coberto pelos testes de ferramenta da tarefa 9
(`test_duas_revisoes_vencidas`, `test_pending_prioriza_revisao_da_mesma_tag`).

**Passo 3: commit.**

```bash
git add server/decision_memory/models.py server/decision_memory/pending.py
git commit -m "feat(server): modelos de saída e bloco pending sobre o banco"
```

---

### Tarefa 8: consultas de leitura

**Arquivos:**
- Criar: `server/decision_memory/reads.py`

A regra de relevância é a do stub (`decision_memory_stub/corpus.py:score`), portada: o agente
pergunta em linguagem natural, e exigir todos os termos (`websearch_to_tsquery`) não acharia
nada. Termos em OR; conta quantos casam e quantos casam tag; ordena por relevância e desempata
por id. Nunca por efeito. Como no stub, a busca ignora acento e caixa: os termos saem dobrados
de `terms()` e o texto é dobrado na hora da consulta (`fold_sql`), sem mudar o schema — ao
custo de não usar o índice GIN, o que não pesa no volume da v0.

**Passo 1: `reads.py`.**

```python
"""Consultas de leitura: busca, decisão e revisões pendentes."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import models
from .config import today
from .db import evidence_attested
from .guard import fold
from .writes import fold_tags

STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem
    e ou que se ao aos à às pelo pela como mais menos ja já nao não sobre entre
    """.split()
)

EVIDENCE_STATE = f"CASE WHEN {evidence_attested('e.id')} THEN 'attested' ELSE 'proposed' END"

INCLUDE = ("evidence", "learning", "decision")

# O dicionário 'simple' guarda o acento, e a coluna `search` do schema também:
# "confirmacao" não casaria com "confirmação". Tirar o acento no próprio schema
# (extensão unaccent na coluna gerada) é mudança de spec e pede ADR; por ora o
# texto é dobrado aqui, na hora da consulta, e os termos saem dobrados de
# `terms()`. Letras maiúsculas acentuadas estão na lista porque, com collation
# C, lower() não mexe em Ç nem Ã. Letra acentuada fora da lista (não usada em
# português) continua com acento no banco e não casa.
#
# Custo: o tsvector é calculado linha a linha e o índice GIN de `search` não
# é usado. Com o volume da v0 não pesa; quando pesar, a saída é unaccent numa
# coluna gerada com índice, via ADR.
ACCENTED = "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ"
PLAIN = "aaaaaeeeeiiiiooooouuuucn" * 2


def fold_sql(text_sql: str) -> str:
    """Expressão SQL que põe `text_sql` em minúsculas e sem acento, como `fold`."""
    return f"translate(lower({text_sql}), '{ACCENTED}', '{PLAIN}')"


def _folded_tsvector(text_sql: str) -> str:
    return f"to_tsvector('simple', {fold_sql(text_sql)})"


# O mesmo texto de que o schema gera a coluna `search` de cada tabela.
_EV_TEXT = _folded_tsvector("coalesce(e.title, '') || ' ' || coalesce(e.summary, '')")
_DC_TEXT = _folded_tsvector(
    "coalesce(d.title, '') || ' ' || coalesce(d.context, '') || ' ' || coalesce(d.description, '')"
)
_LN_TEXT = _folded_tsvector("l.summary")

# Consulta com mais palavras que isso não é pergunta, é texto colado; e cada
# termo custa um to_tsquery por linha em _HITS. As primeiras bastam.
MAX_TERMS = 32

# Termos em OR: a relevância é decidida em Python, com a mesma regra do stub.
# Tag pode ter hífen ou espaço (spec: `^[a-z0-9][a-z0-9 _./-]*$`), então é
# comparada palavra a palavra, pelo mesmo tsvector dobrado.
_HITS = "(SELECT count(*) FROM unnest(%(terms)s::text[]) term WHERE {vec} @@ to_tsquery('simple', term))"
_CANDIDATE = (
    "(%(any)s::text IS NULL OR {vec} @@ to_tsquery('simple', %(any)s)"
    " OR {tag_vec} @@ to_tsquery('simple', %(any)s))"
)


def _candidate(vec: str, tags_sql: str) -> str:
    # Ponto e barra viram espaço antes do parser: sem isso, 'growth/pricing'
    # seria um caminho e 'v2.checkout' um host, cada um um token só, e a tag não
    # casaria palavra a palavra como em `terms()`.
    tag_text = f"translate(array_to_string({tags_sql}, ' '), './', '  ')"
    return _CANDIDATE.format(vec=vec, tag_vec=_folded_tsvector(tag_text))


_EV_TAGS = "ARRAY(SELECT t.name FROM evidence_tag x JOIN tag t ON t.id = x.tag_id WHERE x.evidence_id = e.id)"
_LN_TAGS = "ARRAY(SELECT t.name FROM learning_tag x JOIN tag t ON t.id = x.tag_id WHERE x.learning_id = l.id)"
_DC_TAGS = "ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id WHERE x.decision_id = d.id)"

SEARCH_EVIDENCE_SQL = f"""
SELECT e.id::text AS id, e.title, e.summary, e.strength::text AS strength,
       e.source_system, e.external_id, e.url, {_EV_TAGS} AS tags,
       {_HITS.format(vec=_EV_TEXT)} AS hits, {EVIDENCE_STATE} AS state
  FROM evidence e
 WHERE {_candidate(_EV_TEXT, _EV_TAGS)}
   AND (%(kinds)s::text[] IS NULL OR e.kind::text = ANY(%(kinds)s))
   AND (%(source_system)s::text IS NULL OR e.source_system = %(source_system)s)
   AND (%(tags)s::text[] IS NULL OR {_EV_TAGS} && %(tags)s::text[])
"""

SEARCH_LEARNING_SQL = f"""
SELECT l.id::text AS id, l.summary AS title, NULL AS summary, l.state::text AS state,
       {_LN_TAGS} AS tags, {_HITS.format(vec=_LN_TEXT)} AS hits
  FROM learning l
 WHERE {_candidate(_LN_TEXT, _LN_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_LN_TAGS} && %(tags)s::text[])
"""

SEARCH_DECISION_SQL = f"""
SELECT d.id::text AS id, d.title, d.description AS summary, d.state::text AS state,
       {_DC_TAGS} AS tags, {_HITS.format(vec=_DC_TEXT)} AS hits
  FROM decision d
 WHERE {_candidate(_DC_TEXT, _DC_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_DC_TAGS} && %(tags)s::text[])
"""


def terms(text: str) -> list[str]:
    """Palavras da consulta, em minúsculas e sem acento, sem stopwords e sem
    repetição.

    Só letras e dígitos sobrevivem: cada termo vai para to_tsquery, e nenhum
    operador (& | ! : * ( ) ' <->) pode chegar lá. A dobra vem antes do corte
    para que texto em forma decomposta (e + acento combinante) não parta a
    palavra ao meio.
    """
    raw = "".join(ch if ch.isalnum() else " " for ch in fold(text)).split()
    out: list[str] = []
    for word in raw:
        if len(word) > 2 and word not in STOPWORDS and word not in out:
            out.append(word)
    return out


def relevance(n_terms: int, hits: int, tag_hits: int) -> float:
    """A regra do stub: relevância textual pura, nunca efeito."""
    if n_terms == 0:
        return 1.0
    if tag_hits == 0:
        # Uma palavra solta em comum não é relevância: sem casar tag, consulta de
        # três termos ou mais precisa de pelo menos dois.
        needed = 1 if n_terms <= 2 else 2
        if hits < needed:
            return 0.0
    return hits + 2.0 * tag_hits


def search(conn, query: str, tags: list[str] | None, kinds: list[str] | None,
           source_system: str | None, include: list[str] | None,
           limit: int) -> tuple[list[models.SearchItem], int]:
    unknown = sorted(set(include or []) - set(INCLUDE))
    if unknown:
        raise ToolError(f"include aceita só {', '.join(INCLUDE)}; recebeu {', '.join(unknown)}.")
    include = include or list(INCLUDE)
    if kinds or source_system:
        # Tipo e origem são atributos de evidência; pedir por eles restringe a busca.
        include = ["evidence"]
    limit = max(1, min(int(limit), 50))
    # O filtro por tag é pelo nome exato, dobrado como na escrita (fold_tags);
    # tag fora do padrão não é recusada, só não casa com nada. Como no stub, as
    # palavras da tag também entram como termos, passando por `terms()`: tag
    # crua em to_tsquery seria erro de sintaxe com "(" ou "&".
    wanted_tags = fold_tags(tags)
    words = terms(query)
    for tag in wanted_tags:
        words += [w for w in terms(tag) if w not in words]
    words = words[:MAX_TERMS]
    params: dict[str, Any] = {
        "terms": words,
        "any": " | ".join(words) or None,
        "kinds": kinds or None, "source_system": source_system,
        "tags": wanted_tags or None,
    }

    scored: list[tuple[float, models.SearchItem]] = []

    def keep(row: dict, item: models.SearchItem) -> None:
        tag_tokens = {w for tag in row["tags"] for w in terms(tag)}
        tag_hits = len(set(words) & tag_tokens)
        score = relevance(len(words), row["hits"], tag_hits)
        if score > 0:
            scored.append((score, item))

    if "evidence" in include:
        for row in conn.execute(SEARCH_EVIDENCE_SQL, params).fetchall():
            source = None
            if row["source_system"] or row["url"]:
                source = models.SourceRef(system=row["source_system"],
                                          external_id=row["external_id"], url=row["url"])
            keep(row, models.SearchItem(
                type="evidence", id=row["id"], title=row["title"], summary=row["summary"],
                strength=row["strength"], source=source, tags=row["tags"], state=row["state"]))
    if "learning" in include:
        for row in conn.execute(SEARCH_LEARNING_SQL, params).fetchall():
            keep(row, models.SearchItem(type="learning", id=row["id"], title=row["title"],
                                        tags=row["tags"], state=row["state"]))
    if "decision" in include:
        for row in conn.execute(SEARCH_DECISION_SQL, params).fetchall():
            keep(row, models.SearchItem(type="decision", id=row["id"], title=row["title"],
                                        summary=row["summary"], tags=row["tags"],
                                        state=row["state"]))

    # Relevância textual e, no empate, id — nunca efeito (ADR 0003).
    scored.sort(key=lambda r: (r[0], r[1].id), reverse=True)
    return [item for _, item in scored[:limit]], len(scored)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def get_decision(conn, id: str | None, slug: str | None) -> models.DecisionData:
    if not id and not slug:
        raise ToolError("Informe id ou slug da decisão.")
    row = conn.execute(
        """
        SELECT d.*, d.id::text AS id_text, p.name AS decider_name, pr.name AS project_name,
               ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id
                      WHERE x.decision_id = d.id ORDER BY t.name) AS tags
          FROM decision d
          JOIN person p ON p.id = d.decider_person_id
          LEFT JOIN project pr ON pr.id = d.project_id
         WHERE d.id = %(id)s OR d.slug = %(slug)s
         ORDER BY (d.id = %(id)s) DESC NULLS LAST  -- vindo os dois, o id vence
         LIMIT 1
        """,
        {"id": _as_uuid(id), "slug": slug},
    ).fetchone()
    if row is None:
        raise ToolError(
            f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
        )
    did = row["id"]

    # Alternativas e lições não têm coluna de ordem de inserção: o id (uuid)
    # dá ordem estável, não cronológica.
    alternatives = [
        models.Alternative(description=a["description"], rejection_reason=a["rejection_reason"])
        for a in conn.execute(
            "SELECT description, rejection_reason FROM alternative WHERE decision_id = %s "
            "ORDER BY id", (did,)).fetchall()
    ]
    evidence = [
        models.DecisionEvidence(
            evidence_id=e["id"], title=e["title"], kind=e["kind"], role=e["role"],
            weight=float(e["weight"]) if e["weight"] is not None else None,
            strength=e["strength"], note=e["note"],
            source=models.SourceRef(system=e["source_system"], external_id=e["external_id"],
                                    url=e["url"]) if e["source_system"] or e["url"] else None)
        for e in conn.execute(
            """
            SELECT e.id::text AS id, e.title, e.kind::text AS kind, de.role::text AS role,
                   de.weight, e.strength::text AS strength, de.note,
                   e.source_system, e.external_id, e.url
              FROM decision_evidence de JOIN evidence e ON e.id = de.evidence_id
             WHERE de.decision_id = %s ORDER BY de.weight DESC NULLS LAST, e.id
            """, (did,)).fetchall()
    ]
    learnings = [
        models.DecisionLearning(learning_id=ln["id"], summary=ln["summary"], state=ln["state"])
        for ln in conn.execute(
            "SELECT l.id::text AS id, l.summary, l.state::text AS state FROM decision_learning x "
            "JOIN learning l ON l.id = x.learning_id WHERE x.decision_id = %s ORDER BY l.id",
            (did,)).fetchall()
    ]
    reviews = [
        models.DecisionReview(review_id=r["id"], due_on=r["due_on"].isoformat(),
                              done_on=r["done_on"].isoformat() if r["done_on"] else None,
                              verdict=r["verdict"], notes=r["notes"])
        for r in conn.execute(
            "SELECT id::text AS id, due_on, done_on, verdict::text AS verdict, notes "
            "FROM review WHERE decision_id = %s ORDER BY due_on, id", (did,)).fetchall()
    ]

    expectation = None
    if row["state"] == "attested":
        x = conn.execute(
            "SELECT p.name, x.recorded_at, x.confidence, x.expected_metric, "
            "x.expected_magnitude, x.due_on FROM expectation x "
            "JOIN person p ON p.id = x.recorded_by WHERE x.decision_id = %s "
            "ORDER BY x.recorded_at DESC LIMIT 1", (did,)).fetchone()
        if x:
            expectation = models.Expectation(
                recorded_by=x["name"], recorded_at=x["recorded_at"].isoformat(),
                confidence=float(x["confidence"]), expected_metric=x["expected_metric"],
                expected_magnitude=x["expected_magnitude"], due_on=x["due_on"].isoformat())

    prov = conn.execute(
        """
        SELECT pv.author_kind::text AS author_kind, pv.model, pv.source_ref, pv.attested_at,
               pp.name AS principal, pa.name AS attested_by
          FROM provenance pv
          LEFT JOIN person pp ON pp.id = pv.principal_person_id
          LEFT JOIN person pa ON pa.id = pv.attested_by
         WHERE pv.object_type = 'decision' AND pv.object_id = %s
         ORDER BY pv.created_at, pv.id LIMIT 1
        """, (did,)).fetchone()

    return models.DecisionData(
        decision_id=row["id_text"], slug=row["slug"], title=row["title"],
        context=row["context"], description=row["description"], door=row["door"],
        decided_on=row["decided_on"].isoformat(), decider=row["decider_name"],
        project=row["project_name"], state=row["state"], tags=row["tags"],
        alternatives=alternatives, evidence=evidence, learnings=learnings, reviews=reviews,
        expectation=expectation,
        provenance=models.Provenance(
            author_kind=prov["author_kind"], model=prov["model"], principal=prov["principal"],
            source_ref=prov["source_ref"], attested_by=prov["attested_by"],
            attested_at=prov["attested_at"].isoformat() if prov["attested_at"] else None,
        ) if prov else None,
    )


def _like_literal(text: str | None) -> str | None:
    """`text` para ILIKE ... ESCAPE '\\' casando como texto: % e _ de quem chama
    não viram curinga."""
    if text is None:
        return None
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def pending_reviews(conn, owner_email: str | None, project: str | None, overdue_only: bool,
                    include_unattested: bool) -> models.PendingReviewsData:
    reference = today()
    rows = conn.execute(
        """
        SELECT r.id::text AS review_id, d.id::text AS decision_id, d.slug, d.title, r.due_on,
               p.name AS owner, pr.name AS project
          FROM review r
          JOIN decision d ON d.id = r.decision_id
          JOIN person p ON p.id = d.decider_person_id
          LEFT JOIN project pr ON pr.id = d.project_id
         WHERE r.done_on IS NULL
           AND (%(owner)s::text IS NULL OR lower(p.email) = lower(%(owner)s))
           AND (%(project)s::text IS NULL
                OR pr.name ILIKE '%%' || %(project)s || '%%' ESCAPE '\\')
         ORDER BY r.due_on, d.id, r.id
        """,
        {"owner": owner_email, "project": _like_literal(project)},
    ).fetchall()
    reviews = []
    for r in rows:
        overdue = (reference - r["due_on"]).days
        if overdue_only and overdue < 0:
            continue
        reviews.append(models.PendingReviewItem(
            review_id=r["review_id"], decision_id=r["decision_id"], slug=r["slug"],
            title=r["title"], due_on=r["due_on"].isoformat(), overdue_days=overdue,
            owner=r["owner"], project=r["project"]))
    # Mais vencida primeiro; decisão e revisão desempatam, para a ordem não
    # depender do plano de execução.
    reviews.sort(key=lambda r: (-r.overdue_days, r.decision_id, r.review_id))

    unattested: list[models.UnattestedItem] = []
    if include_unattested:
        unattested = [
            models.UnattestedItem(type=u["type"], id=u["id"], title=u["title"],
                                  created_on=u["created_on"].isoformat())
            for u in conn.execute(
                f"""
                SELECT 'decision' AS type, id::text AS id, title, decided_on AS created_on
                  FROM decision WHERE state = 'proposed'
                UNION ALL
                SELECT 'learning', id::text, summary, recorded_on FROM learning WHERE state = 'proposed'
                UNION ALL
                SELECT 'evidence', e.id::text, e.title, e.created_at::date FROM evidence e
                 WHERE {EVIDENCE_STATE} = 'proposed'
                ORDER BY 4 DESC, 2
                """).fetchall()
        ]

    return models.PendingReviewsData(
        reviews=reviews, unattested=unattested,
        overdue_count=sum(1 for r in reviews if r.overdue_days >= 0))
```

Nota: `unattested` não é filtrado por `owner_email`, como no stub. Anote no README do servidor.

**Passo 2: commit** (os testes vêm na tarefa 9, com o servidor montado).

```bash
git add server/decision_memory/reads.py
git commit -m "feat(server): busca, decisão e revisões pendentes sobre o banco"
```

---

### Tarefa 9: servidor com as ferramentas de leitura, e seus testes

**Arquivos:**
- Criar: `server/decision_memory/server.py`
- Criar: `server/tests_real/test_app_surface.py`, `server/tests_real/test_app_reads.py`

**Passo 1: testes de superfície.** `server/tests_real/test_app_surface.py`:

```python
from __future__ import annotations

import re

import pytest
from conftest import ROOT, run

from decision_memory import guard
from decision_memory.server import build_server

EXPECTED_TOOLS = {"search_evidence", "get_decision", "propose_decision",
                  "attach_evidence", "record_learning", "list_pending_reviews"}


@pytest.fixture(scope="module")
def tools(pool):
    return {tool.name: tool for tool in run(build_server(pool).list_tools())}


def descricoes_do_mcp_tools() -> dict[str, str]:
    texto = (ROOT / "MCP_TOOLS.md").read_text(encoding="utf-8")
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"^### `(\w+)`\n\n> (.+)$", texto, re.MULTILINE)}


@pytest.mark.xfail(strict=True, reason="escritas entram na tarefa 10")
def test_sao_exatamente_seis_ferramentas(tools):
    assert set(tools) == EXPECTED_TOOLS, "não crie a sétima ferramenta sem ADR"


def test_descricoes_sao_as_do_mcp_tools(tools):
    esperadas = descricoes_do_mcp_tools()
    assert set(esperadas) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description == esperadas[name], f"{name} divergiu de MCP_TOOLS.md"


def test_toda_ferramenta_declara_output_schema(tools):
    assert [n for n, t in tools.items() if not t.output_schema] == []


def test_anotacoes_batem_com_mcp_tools(tools):
    somente_leitura = {"search_evidence", "get_decision", "list_pending_reviews"}
    for name, tool in tools.items():
        assert tool.annotations.open_world_hint is False, name
        assert tool.annotations.read_only_hint is (name in somente_leitura), name


def test_nenhum_schema_de_entrada_pede_confianca_ou_expectativa(tools):
    for name, tool in tools.items():
        campos = " ".join(tool.input_schema.get("properties", {})).lower()
        for termo in ("confidence", "confianca", "expectation", "expectativa", "certeza"):
            assert termo not in campos, f"{name} expõe '{termo}'"


def test_ordem_das_middlewares(pool):
    """Identidade primeiro; depois as recusas, antes de qualquer ferramenta."""
    nossas = build_server(pool).middleware[-3:]
    assert nossas[0].__name__ == "resolve_identity"
    assert nossas[1:] == [guard.refuse_expectation, guard.refuse_nul]
```

**Passo 2: testes de leitura.** `server/tests_real/test_app_reads.py`:

```python
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import psycopg
import pytest
from conftest import run
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.types.json import Jsonb

from decision_memory import reads
from decision_memory.guard import fold
from decision_memory.identity import acting_as
from decision_memory.reads import terms
from decision_memory.seed import fid
from decision_memory.server import build_server


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args):
    return run(server.call_tool(name, args))


def test_busca_traz_a_evidencia_que_contradiz(server):
    res = call(server, "search_evidence", {"query": "checkout confirmação"})
    ids = [i["id"] for i in res.structured_content["data"]["items"]]
    assert str(fid("ev-checkout-confirm")) in ids
    assert str(fid("ev-checkout-pagina-unica")) in ids


def test_pergunta_em_linguagem_natural_encontra(server):
    res = call(server, "search_evidence", {"query": "já testamos tirar a confirmação do checkout?"})
    assert res.structured_content["data"]["items"]


def test_busca_sem_resultado_manda_dizer_isso_em_voz_alta(server):
    res = call(server, "search_evidence", {"query": "programa de fidelidade por pontos"})
    data = res.structured_content["data"]
    assert data["items"] == []
    assert "explicitamente" in data["note"]


SET_POINT = ("UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', %s) "
             "WHERE id = %s")


def test_ordem_da_busca_nao_depende_do_efeito_nem_da_forca(server, admin_conn):
    """ADR 0003: ranquear por efeito compara grandezas de origens diferentes.

    Os efeitos dos resultados são redistribuídos na ordem inversa do ranking:
    o primeiro fica com o valor do último, e assim por diante. Qualquer
    ordenação que olhe para o efeito — sinal, magnitude, o que for — se
    inverteria. Rebaixar a força só do primeiro colocado o tiraria do topo se a
    busca olhasse para a força.
    """
    consulta = {"query": "checkout conversão", "include": ["evidence"]}
    antes = _ids(server, consulta)
    pontos = dict(admin_conn.execute(
        "SELECT id::text, normalized->'effect'->'point' FROM evidence WHERE id::text = ANY(%s) "
        "AND normalized->'effect'->>'point' IS NOT NULL", (antes,)).fetchall())
    com_efeito = [i for i in antes if i in pontos]  # na ordem do ranking
    assert len(com_efeito) >= 2 and len({str(pontos[i]) for i in com_efeito}) >= 2, \
        "o teste precisa de ao menos dois efeitos distintos"
    primeiro = antes[0]
    forca = admin_conn.execute("SELECT strength FROM evidence WHERE id = %s",
                               (primeiro,)).fetchone()[0]
    assert forca == "causal"
    try:
        for eid, ponto in zip(com_efeito, reversed([pontos[i] for i in com_efeito]), strict=True):
            admin_conn.execute(SET_POINT, (Jsonb(ponto), eid))
        admin_conn.execute("UPDATE evidence SET strength = 'anecdotal' WHERE id = %s", (primeiro,))
        depois = _ids(server, consulta)
    finally:
        for eid in com_efeito:
            admin_conn.execute(SET_POINT, (Jsonb(pontos[eid]), eid))
        admin_conn.execute("UPDATE evidence SET strength = %s WHERE id = %s", (forca, primeiro))
    assert antes == depois


def test_filtro_por_origem_e_por_tipo(server):
    res = call(server, "search_evidence",
               {"query": "checkout", "kinds": ["experiment"], "source_system": "growthbook"})
    itens = res.structured_content["data"]["items"]
    assert itens and all(i["source"]["system"] == "growthbook" for i in itens)


def test_evidencia_importada_aparece_atestada(server):
    res = call(server, "search_evidence", {"query": "checkout", "include": ["evidence"]})
    assert {i["state"] for i in res.structured_content["data"]["items"]} == {"attested"}


def test_decisao_atestada_traz_expectativa(server):
    data = call(server, "get_decision", {"slug": "remover-confirmacao-checkout"}).structured_content["data"]
    assert data["state"] == "attested"
    assert data["expectation"]["confidence"] == 0.7
    assert "contradicts" in {e["role"] for e in data["evidence"]}
    assert data["provenance"]["author_kind"] == "import"


def test_decisao_proposta_nao_traz_expectativa(server):
    data = call(server, "get_decision", {"id": str(fid("dec-push-diario"))}).structured_content["data"]
    assert data["state"] == "proposed"
    assert data["expectation"] is None


def test_decisao_inexistente_diz_o_que_fazer(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "get_decision", {"id": "nao-existe"})
    assert "search_evidence" in str(excinfo.value)


def test_duas_revisoes_vencidas(server):
    data = call(server, "list_pending_reviews", {"overdue_only": True}).structured_content["data"]
    assert data["overdue_count"] == 2


def test_bloco_pending_nunca_esta_ausente(server):
    for name, args in [("search_evidence", {"query": "zzz inexistente"}),
                       ("get_decision", {"slug": "remover-confirmacao-checkout"}),
                       ("list_pending_reviews", {})]:
        assert "pending" in call(server, name, args).structured_content, name


def test_pending_prioriza_revisao_da_mesma_tag(server):
    res = call(server, "search_evidence", {"query": "onboarding"})
    due = res.structured_content["pending"]["reviews_due"]
    assert due[0]["decision_id"] == str(fid("dec-onboarding-tres-etapas"))


def test_termo_nunca_carrega_operador_de_tsquery():
    """Cada termo vai para to_tsquery: só letras e dígitos podem chegar lá."""
    for palavra in terms("checkout & !x | (y) 'aspas' a:b c*d <-> e\\f"):
        assert palavra.isalnum(), palavra


@pytest.mark.parametrize("consulta", [
    "checkout & !x | (y",
    "checkout' | 'x",
    "checkout:* & conversão:A",
    "checkout <-> confirmação",
    "'''",
    "::: *** !!!",
])
def test_operadores_de_tsquery_na_consulta_sao_inofensivos(server, consulta):
    res = call(server, "search_evidence", {"query": consulta})
    assert not res.is_error
    assert "data" in res.structured_content


def _ids(server, args):
    res = call(server, "search_evidence", args)
    return [i["id"] for i in res.structured_content["data"]["items"]]


def test_busca_ignora_acento_e_caixa(server):
    """Como no stub: consulta sem acento acha texto com acento, e vice-versa."""
    com_acento = _ids(server, {"query": "confirmação"})
    assert com_acento
    assert _ids(server, {"query": "confirmacao"}) == com_acento
    assert _ids(server, {"query": "CONFIRMAÇÃO"}) == com_acento


def test_termos_saem_sem_acento():
    assert terms("CONFIRMAÇÃO não Conversão") == ["confirmacao", "conversao"]


@pytest.mark.parametrize("collation", ["", ' COLLATE "C"'])
def test_dobra_no_banco_independe_da_collation(admin_conn, collation):
    """Com collation C, lower() não mexe em Ç nem Ã; a dobra tem de dar conta."""
    sql = reads.fold_sql(f"(%s::text{collation})")
    assert admin_conn.execute(f"SELECT {sql}", ("CONFIRMAÇÃO Ações ÚNICA",)).fetchone()[0] \
        == "confirmacao acoes unica"


def test_dobra_do_banco_e_do_python_concordam():
    assert len(reads.ACCENTED) == len(reads.PLAIN)
    for com, sem in zip(reads.ACCENTED, reads.PLAIN, strict=True):
        assert fold(com) == sem, com


def test_operadores_de_tsquery_nas_tags_sao_inofensivos(server):
    res = call(server, "search_evidence", {"query": "checkout", "tags": ["x & !y", "(z", "a:*"]})
    assert not res.is_error


@contextmanager
def _evidencia_com_tags(admin_conn, nomes):
    """Evidência cuja única ligação com as palavras das tags são as próprias tags."""
    eid = admin_conn.execute(
        "INSERT INTO evidence (kind, title, summary) VALUES ('analysis', "
        "'Registro zzqk de teste', 'Texto sem as palavras da tag') RETURNING id::text"
    ).fetchone()[0]
    tids = []
    try:
        for nome in nomes:
            tids.append(admin_conn.execute(
                "INSERT INTO tag (name) VALUES (%s) RETURNING id", (nome,)).fetchone()[0])
            admin_conn.execute("INSERT INTO evidence_tag VALUES (%s, %s)", (eid, tids[-1]))
        yield eid
    finally:
        admin_conn.execute("DELETE FROM evidence_tag WHERE evidence_id = %s", (eid,))
        admin_conn.execute("DELETE FROM evidence WHERE id = %s", (eid,))
        admin_conn.execute("DELETE FROM tag WHERE id = ANY(%s)", (tids,))


@pytest.fixture
def evidencia_com_tag_composta(admin_conn):
    with _evidencia_com_tags(admin_conn, ["pagina-unica"]) as eid:
        yield eid


def test_tag_com_barra_ou_ponto_casa_por_palavra(server, admin_conn):
    """O parser do Postgres guardaria 'growth/pricing' como caminho, inteiro."""
    with _evidencia_com_tags(admin_conn, ["growth/pricing", "v2.zzfrete"]) as eid:
        assert eid in _ids(server, {"query": "pricing"})
        assert eid in _ids(server, {"query": "growth"})
        assert eid in _ids(server, {"query": "zzfrete"})
        assert _ids(server, {"query": "", "tags": ["growth/pricing"]}) == [eid]


def test_tag_composta_casa_por_palavra(server, evidencia_com_tag_composta):
    """Tag pode ter hífen ou espaço (spec): casa palavra a palavra, como no stub."""
    assert evidencia_com_tag_composta in _ids(server, {"query": "pagina unica"})


def test_filtro_por_tag_composta_e_pelo_nome_exato(server, evidencia_com_tag_composta):
    for tag in ("pagina-unica", " Página-Única "):
        assert _ids(server, {"query": "", "tags": [tag]}) == [evidencia_com_tag_composta], tag
    assert _ids(server, {"query": "", "tags": ["pagina"]}) == []


def test_consulta_enorme_usa_so_os_primeiros_termos(server):
    """10 mil palavras: só as MAX_TERMS primeiras contam, e responde rápido."""
    palavras = ["checkout", "confirmação"] + [f"zz{i:05d}" for i in range(10_000)]
    inicio = time.monotonic()
    enorme = _ids(server, {"query": " ".join(palavras)})
    assert time.monotonic() - inicio < 2
    assert enorme == _ids(server, {"query": " ".join(palavras[:reads.MAX_TERMS])})
    assert enorme


def test_id_vence_slug_quando_vem_os_dois(server):
    data = call(server, "get_decision", {"id": str(fid("dec-push-diario")),
                                         "slug": "remover-confirmacao-checkout"})
    assert data.structured_content["data"]["slug"] != "remover-confirmacao-checkout"
    assert data.structured_content["data"]["decision_id"] == str(fid("dec-push-diario"))


def test_dono_padrao_vem_da_identidade(server):
    with acting_as("carla@exemplo.com.br"):
        data = call(server, "list_pending_reviews", {}).structured_content["data"]
    assert [r["slug"] for r in data["reviews"]] == ["onboarding-tres-etapas"]
    with acting_as("ana@exemplo.com.br"):
        data = call(server, "list_pending_reviews", {}).structured_content["data"]
    assert {r["slug"] for r in data["reviews"]} == {"remover-confirmacao-checkout",
                                                    "manter-devolucao-30-dias"}


@pytest.mark.parametrize("erro", [psycopg.OperationalError("SELECT segredo FROM tabela_x"),
                                  RuntimeError("segredo no traceback")])
def test_erro_inesperado_vira_mensagem_generica(server, monkeypatch, caplog, erro):
    def explode(*args, **kwargs):
        raise erro

    monkeypatch.setattr(reads, "search", explode)
    with pytest.raises(ToolError) as excinfo:
        call(server, "search_evidence", {"query": "checkout"})
    mensagem = str(excinfo.value)
    assert "ref" in mensagem and "segredo" not in mensagem
    ref = mensagem.split("ref ")[1].split(")")[0]
    entries = [json.loads(r.getMessage()) for r in caplog.records if ref in r.getMessage()]
    assert entries and entries[0]["type"] == type(erro).__name__
    # Cloud Logging lê a severidade do campo, não do nível do logger.
    assert entries[0]["severity"] == "ERROR"


def test_resumo_em_texto_corta_a_consulta(server):
    consulta = "checkout " + "x" * 500
    res = call(server, "search_evidence", {"query": consulta})
    assert res.structured_content["data"]["query"] == consulta
    assert "x" * 121 not in res.content[0].text


def test_include_desconhecido_e_recusado_com_os_valores_aceitos(server):
    with pytest.raises(ToolError) as excinfo:
        call(server, "search_evidence", {"query": "checkout", "include": ["evidence", "lesson"]})
    texto = str(excinfo.value)
    assert "lesson" in texto
    assert all(v in texto for v in ("evidence", "learning", "decision"))


def test_filtro_de_projeto_trata_curinga_como_texto(server):
    def reviews(project):
        return call(server, "list_pending_reviews",
                    {"project": project}).structured_content["data"]["reviews"]

    assert reviews("checkout")  # o filtro funciona
    for curinga in ("%", "_", "\\", "Checkout_2026", "%2026"):
        assert reviews(curinga) == [], curinga


def test_filtro_por_tag_usa_a_mesma_normalizacao_da_escrita(server, evidencia_com_tag_composta):
    # Repetida, com acento, caixa e espaço: a mesma dobra de writes.fold_tags.
    args = {"query": "", "tags": ["Página-Única", "pagina-unica ", "  "]}
    assert _ids(server, args) == [evidencia_com_tag_composta]
```

Se `test_duas_revisoes_vencidas` falhar, compare com o stub (`server/tests/test_stub.py`,
mesmo nome) antes de mudar o número: a data de referência é 2026-09-22 nos dois.

**Passo 3: rodar e ver falhar.** `.venv/bin/python -m pytest server/tests_real -q` → import de `server`.

**Passo 4: `server/decision_memory/server.py`** (leitura; as escritas entram na tarefa 10).

```python
"""As seis ferramentas de MCP_TOOLS.md sobre o Postgres.

As descrições são cópia literal de MCP_TOOLS.md, e há teste para isso: é a
descrição que faz o agente chamar a ferramenta na hora certa.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

import mcp.types as t
import psycopg
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from psycopg_pool import ConnectionPool

from . import guard, identity, models, pending, reads

log = logging.getLogger("decision_memory")
T = TypeVar("T")

READ = t.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

DESCRIPTIONS = {
    "search_evidence": (
        "Chame ANTES de redigir proposta, PRD, plano de experimento ou qualquer recomendação "
        "de produto, para saber o que a organização já testou, decidiu ou aprendeu sobre o "
        'tema. Chame também sempre que alguém perguntar "já testamos isso?" ou "por que '
        'decidimos X?". Retorna evidências, lições e decisões, com a força de cada evidência.'
    ),
    "get_decision": (
        "Chame quando precisar do raciocínio completo por trás de uma decisão específica: "
        "contexto, alternativas descartadas e por quê, evidências que sustentaram ou "
        "contradisseram, lições derivadas e revisões realizadas."
    ),
    "propose_decision": (
        'Chame quando uma escolha de produto for fechada na conversa — "vamos com a opção B", '
        '"decidimos não lançar", "mantemos o fluxo atual" — para que ela entre no registro com '
        "contexto e alternativas enquanto o raciocínio ainda está fresco. O registro nasce como "
        "proposta e será confirmado por uma pessoa."
    ),
    "attach_evidence": (
        "Chame quando uma evidência — resultado de experimento, estudo, análise, entrevista ou "
        "documento — tiver pesado numa decisão, seja a favor ou contra. Registre também a "
        "evidência que contradisse a escolha: evidência contrária registrada é o que distingue "
        "memória de justificativa."
    ),
    "record_learning": (
        "Chame quando um experimento terminar, uma decisão for revisada ou alguém formular uma "
        'conclusão que valha para além do caso atual — "usuários preferem X quando Y". Escreva '
        "a lição como afirmação reutilizável, não como resumo do caso."
    ),
    "list_pending_reviews": (
        "Chame no início de uma sessão de planejamento, ao retomar um projeto ou quando alguém "
        "perguntar o que está pendente: lista decisões cuja revisão de desfecho venceu ou está "
        "próxima, e registros propostos aguardando atestação."
    ),
}

NOTHING_FOUND = (
    "Nada encontrado no registro sobre esse tema. Diga isso explicitamente ao usuário em vez "
    "de seguir como se não houvesse consultado."
)


# Consulta ecoada no resumo em texto; o `structured_content` leva a íntegra.
MAX_ECHO = 120


def _echo(query: str) -> str:
    return query if len(query) <= MAX_ECHO else query[:MAX_ECHO] + "…"


def _text(*parts: str) -> list[t.TextContent]:
    return [t.TextContent(type="text", text=" ".join(p for p in parts if p))]


def _pending_sentence(block: models.Pending) -> str:
    bits = []
    if block.reviews_due:
        nearest = block.reviews_due[0]
        bits.append(f"Revisão vencida há {nearest.overdue_days} dias: {nearest.title}.")
    if block.on_this_record:
        bits.append("Falta: " + "; ".join(block.on_this_record) + ".")
    return " ".join(bits)


def _result(summary: str, body: Any, block: models.Pending) -> t.CallToolResult:
    return t.CallToolResult(content=_text(summary, _pending_sentence(block)),
                            structured_content=body.model_dump(mode="json"))


def with_db(pool: ConnectionPool, work: Callable[[psycopg.Connection], T]) -> T:
    """Uma conexão, uma transação. Erro de banco não vaza SQL para o agente."""
    try:
        with pool.connection() as conn:
            return work(conn)
    except ToolError:
        raise
    except Exception as exc:
        # Qualquer outra falha, de banco ou não, sai com a mesma mensagem; o log,
        # achável pela ref, diz o que houve. Sem str(exc): no erro do psycopg ele
        # traz o DETAIL do Postgres, com o valor da linha (um e-mail, por
        # exemplo); fora do banco, pode trazer argumento de quem chama. Vão só o
        # tipo, o código, a mensagem principal e a restrição.
        ref = uuid.uuid4().hex[:8]
        bug = not isinstance(exc, psycopg.OperationalError)
        diag = exc.diag if isinstance(exc, psycopg.Error) else None
        # `severity` é o campo que o Cloud Logging lê de uma linha JSON no stdout.
        log.error(json.dumps({"severity": "ERROR",
                              "event": "db_error" if diag is not None else "internal_error",
                              "ref": ref, "bug": bug, "type": type(exc).__name__,
                              "sqlstate": diag.sqlstate if diag else None,
                              "message": diag.message_primary if diag else None,
                              "constraint": diag.constraint_name if diag else None}))
        raise ToolError(
            f"Erro interno ao acessar o registro (ref {ref}). Nada foi gravado. "
            "Tente de novo; se persistir, avise quem administra o servidor."
        ) from None


def build_server(pool: ConnectionPool, audiences: tuple[str, ...] = ()) -> MCPServer:
    server = MCPServer(
        name="decision-memory",
        version="0.2.0",
        instructions=(
            "Registro de decisões e aprendizagens de produto. Consulte antes de redigir "
            "proposta, PRD ou plano de experimento, e registre a decisão quando ela for "
            "fechada na conversa. Confiança e resultado esperado são preenchidos por "
            "pessoa na atestação, nunca por agente."
        ),
    )

    @server.tool(name="search_evidence", description=DESCRIPTIONS["search_evidence"],
                 annotations=READ)
    def search_evidence(
        query: str,
        tags: list[str] | None = None,
        kinds: list[str] | None = None,
        source_system: str | None = None,
        include: list[str] | None = None,
        limit: int = 10,
    ) -> Annotated[t.CallToolResult, models.SearchResponse]:
        def work(conn):
            items, total = reads.search(conn, query, tags, kinds, source_system, include, limit)
            hit_tags = {tag for item in items for tag in item.tags}
            block = pending.build(conn, context_tags=hit_tags or set(reads.terms(query)))
            return items, total, block

        items, total, block = with_db(pool, work)
        body = models.SearchResponse(
            data=models.SearchData(query=query, items=items, total=total,
                                   note=None if items else NOTHING_FOUND),
            pending=block)
        summary = (f"{len(items)} de {total} resultados para '{_echo(query)}'." if items
                   else f"Nenhum resultado para '{_echo(query)}'.")
        return _result(summary, body, block)

    @server.tool(name="get_decision", description=DESCRIPTIONS["get_decision"], annotations=READ)
    def get_decision(
        id: str | None = None, slug: str | None = None
    ) -> Annotated[t.CallToolResult, models.DecisionResponse]:
        def work(conn):
            data = reads.get_decision(conn, id, slug)
            return data, pending.build(conn, context_tags=set(data.tags))

        data, block = with_db(pool, work)
        contra = sum(1 for e in data.evidence if e.role == "contradicts")
        summary = (f"{data.title} ({data.state}): {len(data.evidence)} evidências, "
                   f"{contra} contrária(s), {len(data.alternatives)} alternativa(s) descartada(s).")
        return _result(summary, models.DecisionResponse(data=data, pending=block), block)

    @server.tool(name="list_pending_reviews", description=DESCRIPTIONS["list_pending_reviews"],
                 annotations=READ)
    def list_pending_reviews(
        owner_email: str | None = None,
        project: str | None = None,
        overdue_only: bool = False,
        include_unattested: bool = True,
    ) -> Annotated[t.CallToolResult, models.PendingReviewsResponse]:
        owner = owner_email or identity.current_email()

        def work(conn):
            data = reads.pending_reviews(conn, owner, project, overdue_only, include_unattested)
            return data, pending.build(conn)

        data, block = with_db(pool, work)
        summary = (f"{len(data.reviews)} revisões em aberto, {data.overdue_count} vencida(s); "
                   f"{len(data.unattested)} registro(s) aguardando atestação.")
        return t.CallToolResult(
            content=_text(summary),
            structured_content=models.PendingReviewsResponse(data=data, pending=block)
            .model_dump(mode="json"))

    # As escritas entram aqui (tarefa 10).

    # Ordem importa: a identidade é resolvida antes das recusas e da ferramenta.
    server.middleware.append(identity.middleware(audiences))
    server.middleware.append(guard.refuse_expectation)
    server.middleware.append(guard.refuse_nul)
    return server
```

Atenção a `test_duas_revisoes_vencidas`: sem identidade, `owner` é `None` e a lista não filtra.
Nos testes as chamadas diretas não passam pela middleware, então `current_email()` é `None`.

**Passo 5: rodar e ver passar** (exceto as ferramentas de escrita, que o teste de superfície
ainda não encontra).

```bash
.venv/bin/python -m pytest server/tests_real -q
```
Esperado: tudo passa; `test_sao_exatamente_seis_ferramentas` sai como xfail (só três
ferramentas). `test_descricoes_sao_as_do_mcp_tools` já passa: confere as seis descrições de
MCP_TOOLS.md contra as ferramentas registradas, que por ora são as três de leitura. Se algum teste de busca falhar, **não mexa na regra de
relevância para passar** — compare com o stub e entenda a diferença (provável: acento).

**Passo 6: commit.**

```bash
git add server/decision_memory/server.py server/tests_real/test_app_surface.py server/tests_real/test_app_reads.py
git commit -m "feat(server): ferramentas de leitura sobre o Postgres"
```

---

### Tarefa 10: ferramentas de escrita

**Arquivos:**
- Criar: `server/decision_memory/writes.py`
- Modificar: `server/decision_memory/server.py` (no ponto "As escritas entram aqui")
- Criar: `server/tests_real/test_app_writes.py`
- Modificar: `server/decision_memory/models.py` — campo `reused` em `ProposeData`
- Modificar: `server/tests_real/test_app_surface.py` — tirar o `xfail` de
  `test_sao_exatamente_seis_ferramentas` (é `strict`: com as seis, passa e o xfail vira falha)

**Passo 1: testes.** `server/tests_real/test_app_writes.py`:

```python
from __future__ import annotations

import threading
import time

import pytest
from conftest import ANA, run

from decision_memory import writes
from decision_memory.identity import acting_as
from decision_memory.seed import fid
from decision_memory.server import build_server


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args, as_email=ANA):
    with acting_as(as_email):
        return run(server.call_tool(name, args))


# Títulos com "zeppelin" não casam com nenhuma consulta dos testes de leitura.
PROPOSTA = {
    "title": "Adotar entrega zeppelin no mesmo dia",
    "description": "Entregar no mesmo dia para pedidos até as 14h (teste-escrita).",
    "decider_email": ANA,
}


def proposta(server, title: str, **extra) -> str:
    res = call(server, "propose_decision", {**PROPOSTA, "title": title, **extra})
    return res.structured_content["data"]["decision_id"]


def count(admin_conn, sql: str, *params) -> int:
    return admin_conn.execute(sql, params).fetchone()[0]


def test_proposta_nasce_proposed_e_lista_o_que_falta(server, admin_conn):
    res = call(server, "propose_decision", PROPOSTA).structured_content
    data = res["data"]
    assert data["state"] == "proposed"
    assert data["attest_url"] is None
    assert set(data["missing"]) == {"alternatives", "evidence", "context"}
    assert len(res["pending"]["on_this_record"]) == 3
    kind, model, principal, ref, attested = admin_conn.execute(
        "SELECT pv.author_kind::text, pv.model, p.email, pv.source_ref, pv.attested_at "
        "FROM provenance pv JOIN person p ON p.id = pv.principal_person_id "
        "WHERE pv.object_type = 'decision' AND pv.object_id = %s", (data["decision_id"],)
    ).fetchone()
    assert (kind, model, principal) == ("agent", "unknown", ANA)
    assert ref == "mcp; client=tests"
    assert attested is None
    state = admin_conn.execute("SELECT state::text FROM decision WHERE id = %s",
                               (data["decision_id"],)).fetchone()[0]
    assert state == "proposed"


def test_proposta_completa_grava_alternativas_tags_e_evidencia(server, admin_conn):
    did = proposta(
        server, "Decisão zeppelin completa", context="Pedidos atrasavam na capital.",
        alternatives=[{"description": "Entrega em dois dias",
                       "rejection_reason": "Perde para a concorrência."}],
        tags=["  Teste-Escrita ", "", "ZEPPELIN", "zeppelin", "Conversão"],
        evidence=[{"evidence_id": str(fid("ev-checkout-confirm")), "role": "supports",
                   "weight": 0.5}],
    )
    tags = {r[0] for r in admin_conn.execute(
        "SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id "
        "WHERE x.decision_id = %s", (did,))}
    assert tags == {"teste-escrita", "zeppelin", "conversao"}
    assert count(admin_conn, "SELECT count(*) FROM alternative WHERE decision_id = %s", did) == 1
    role = admin_conn.execute("SELECT role::text FROM decision_evidence WHERE decision_id = %s",
                              (did,)).fetchone()[0]
    assert role == "supports"


def test_sem_identidade_nao_escreve(server, admin_conn):
    title = "Decisão zeppelin sem identidade"
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": title}, as_email=None)
    assert "nada foi gravado" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


def test_conta_nao_cadastrada_nao_escreve(server, admin_conn):
    title = "Decisão zeppelin de conta estranha"
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": title},
             as_email="estranha@exemplo.com")
    assert "não está cadastrada" in str(excinfo.value)
    # O SDK registra o texto do erro; e-mail nele iria para o log.
    assert "estranha@exemplo.com" not in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


def test_email_da_conta_casa_sem_caixa(server):
    """A identidade chega em minúsculas pela middleware, mas o banco pode não estar."""
    did = proposta(server, "Decisão zeppelin em caixa alta")
    with acting_as(ANA.upper()):
        res = run(server.call_tool("record_learning", {
            "summary": "Caixa do e-mail não muda quem é a pessoa zeppelin",
            "decision_ids": [did]}))
    assert res.structured_content["data"]["state"] == "proposed"


def test_decisor_desconhecido_nao_cria_registro(server, admin_conn):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": "Decisão zeppelin fantasma",
                                          "decider_email": "ninguem@exemplo.com"})
    assert str(excinfo.value).endswith(
        "Pessoa não encontrada. Confirme o e-mail com o usuário; o registro não foi criado.")
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin fantasma'") == 0


def test_data_invalida_explica_o_formato(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "decided_on": "ontem"})
    assert "AAAA-MM-DD" in str(excinfo.value)


def test_erro_no_meio_desfaz_tudo(server, admin_conn):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {
            **PROPOSTA, "title": "Decisão zeppelin com evidência inválida",
            "tags": ["zeppelin-desfeito"],
            "alternatives": [{"description": "a", "rejection_reason": "b"}],
            "evidence": [{"evidence_id": "00000000-0000-0000-0000-000000000000",
                          "role": "supports"}]})
    assert "Evidência não encontrada" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin com evidência inválida'") == 0
    assert count(admin_conn, "SELECT count(*) FROM tag WHERE name = 'zeppelin-desfeito'") == 0


def test_idempotency_key_devolve_o_mesmo_registro(server, admin_conn):
    args = {**PROPOSTA, "title": "Decisão zeppelin idempotente", "idempotency_key": "k-123"}
    a = call(server, "propose_decision", args).structured_content["data"]
    b = call(server, "propose_decision", args).structured_content["data"]
    assert a["decision_id"] == b["decision_id"]
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin idempotente'") == 1


def test_idempotency_key_concorrente_nao_duplica_nem_falha(server, admin_conn, monkeypatch):
    """Duas chamadas simultâneas, mesma pessoa e chave: um registro, mesma resposta.

    O atraso em `_slug` abre a janela entre conferir a chave e gravá-la; sem o
    advisory lock, as duas passariam pela conferência e a segunda cairia na
    chave primária de idempotency_key.
    """
    slug = writes._slug

    def slow_slug(conn, title):
        time.sleep(0.3)
        return slug(conn, title)

    monkeypatch.setattr(writes, "_slug", slow_slug)
    args = {**PROPOSTA, "title": "Decisão zeppelin concorrente", "idempotency_key": "k-corrida"}
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = call(server, "propose_decision", args).structured_content["data"]
        except Exception as exc:  # noqa: BLE001 - o teste compara o que voltou
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(isinstance(r, dict) for r in results), results
    assert results[0]["decision_id"] == results[1]["decision_id"]
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin concorrente'") == 1


def test_slug_repetido_ganha_sufixo(server):
    a = call(server, "propose_decision", {**PROPOSTA, "title": "Título zeppelin repetido"})
    b = call(server, "propose_decision", {**PROPOSTA, "title": "Título zeppelin repetido"})
    sa, sb = (r.structured_content["data"]["slug"] for r in (a, b))
    assert sa != sb
    assert sa.startswith("titulo-zeppelin-repetido") and sb.startswith("titulo-zeppelin-repetido-")


def test_evidencia_ja_existente_e_reaproveitada(server, admin_conn):
    did = proposta(server, "Decisão zeppelin para anexar")
    antes = count(admin_conn, "SELECT count(*) FROM evidence")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "outro título", "strength": "causal",
                     "source_system": "internal.lab", "external_id": "exp-2025-0311"},
    }).structured_content["data"]
    assert data["reused_existing"] is True
    assert data["already_linked"] is False
    assert data["role"] == "contradicts"
    assert data["evidence_id"] == str(fid("ev-checkout-pagina-unica"))
    assert count(admin_conn, "SELECT count(*) FROM evidence") == antes


def test_evidencia_nova_de_agente_aparece_proposta_na_busca(server, admin_conn):
    did = proposta(server, "Decisão zeppelin com evidência nova")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence": {"kind": "analysis", "title": "Análise de entregas dirigivel",
                     "strength": "correlational"},
    }).structured_content
    assert data["data"]["state"] == "proposed"
    assert data["data"]["reused_existing"] is False
    assert data["pending"]["on_this_record"] == [
        "Só há evidência favorável registrada até agora: houve algo que contradisse a escolha?"]
    kind = admin_conn.execute(
        "SELECT author_kind::text FROM provenance WHERE object_type = 'evidence' "
        "AND object_id = %s", (data["data"]["evidence_id"],)).fetchone()[0]
    assert kind == "agent"
    busca = call(server, "search_evidence", {"query": "dirigivel"}).structured_content["data"]
    achados = {i["id"]: i["state"] for i in busca["items"]}
    assert achados[data["data"]["evidence_id"]] == "proposed"


def test_experimento_sem_origem_entra_por_agente(server):
    did = proposta(server, "Decisão zeppelin com experimento informal")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "Teste zeppelin de corredor",
                     "strength": "anecdotal", "url": "https://exemplo.com/planilha"},
    }).structured_content["data"]
    assert data["state"] == "proposed"
    assert data["reused_existing"] is False


def test_vinculo_repetido_nao_muda_nada(server, admin_conn):
    did = str(fid("dec-remover-confirmacao"))
    eid = str(fid("ev-checkout-confirm"))
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "discarded", "evidence_id": eid, "weight": 0.1,
    }).structured_content["data"]
    res = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports", "evidence_id": eid,
    }).structured_content
    assert res["pending"]["on_this_record"] == [], "vínculo repetido não cobra nada"
    assert data["already_linked"] is True
    assert data["role"] == "supports", "o papel original não pode ser trocado por agente"
    role, weight = admin_conn.execute(
        "SELECT role::text, weight FROM decision_evidence WHERE decision_id = %s "
        "AND evidence_id = %s", (did, eid)).fetchone()
    assert (role, float(weight)) == ("supports", 0.9)


@pytest.mark.parametrize("kind", ["experiment", "analysis"])
def test_identidade_de_origem_nova_nao_entra_por_agente(server, admin_conn, kind):
    """Qualquer tipo: (source_system, external_id) inédito só entra pela ingestão."""
    did = str(fid("dec-remover-confirmacao"))
    with pytest.raises(Exception) as excinfo:
        call(server, "attach_evidence", {
            "decision_id": did, "role": "supports",
            "evidence": {"kind": kind, "title": "x zeppelin", "strength": "causal",
                         "source_system": "growthbook", "external_id": f"inedito_{kind}"}})
    assert "ingestão" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM evidence "
                             "WHERE external_id = %s OR title = 'x zeppelin'", f"inedito_{kind}") == 0


def test_licao_precisa_de_origem(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "record_learning", {"summary": "algo"})
    assert "origem" in str(excinfo.value)


def test_licao_nasce_proposta(server, admin_conn):
    did = proposta(server, "Decisão zeppelin que ensinou algo")
    eid = str(fid("ev-checkout-confirm"))
    res = call(server, "record_learning", {
        "summary": "Clientes aceitam frete zeppelin mais caro quando a entrega é no mesmo dia",
        "decision_ids": [did, did], "evidence_ids": [eid], "tags": ["Teste-Escrita"],
    }).structured_content
    data = res["data"]
    assert data["state"] == "proposed"
    assert data["linked_decisions"] == [did]
    assert data["linked_evidence"] == [eid]
    assert res["pending"]["on_this_record"] == []
    lid = data["learning_id"]
    state, recorded_on = admin_conn.execute(
        "SELECT state::text, recorded_on::text FROM learning WHERE id = %s", (lid,)).fetchone()
    assert (state, recorded_on) == ("proposed", "2026-09-22")
    assert count(admin_conn, "SELECT count(*) FROM provenance WHERE object_type = 'learning' "
                             "AND object_id = %s AND author_kind = 'agent'", lid) == 1
    assert count(admin_conn, "SELECT count(*) FROM learning_tag x JOIN tag t ON t.id = x.tag_id "
                             "WHERE x.learning_id = %s AND t.name = 'teste-escrita'", lid) == 1


def test_licao_curta_pede_condicao(server):
    did = proposta(server, "Decisão zeppelin com lição curta")
    res = call(server, "record_learning", {"summary": "zeppelin funciona",
                                           "decision_ids": [did]}).structured_content
    assert res["pending"]["on_this_record"] == [
        "Lição muito curta para viajar entre projetos: em que condição ela vale?"]


# ---------------------------------------------------------------- validação


def erro(server, name, args) -> str:
    """Mensagem do erro; nunca pode ser o erro interno genérico."""
    with pytest.raises(Exception) as excinfo:
        call(server, name, args)
    message = str(excinfo.value)
    assert "Erro interno" not in message, message
    return message


@pytest.fixture(scope="module")
def decisao(server):
    return proposta(server, "Decisão zeppelin para validar tipos")


@pytest.mark.parametrize("weight", ["0.5", True, 1.5, -0.1])
def test_weight_invalido_e_recusado(server, decisao, weight):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "weight": weight})
    assert "weight" in message


@pytest.mark.parametrize("weight", [0, 1])
def test_weight_nos_limites_e_aceito(server, weight):
    did = proposta(server, f"Decisão zeppelin com peso {weight}")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "weight": weight,
    }).structured_content["data"]
    assert data["already_linked"] is False


@pytest.mark.parametrize(("evidence", "trecho"), [
    ({"kind": "rumor", "title": "t", "strength": "causal"}, "kind"),
    ({"kind": "analysis", "title": "t", "strength": "forte"}, "strength"),
    ({"kind": "analysis", "title": 3, "strength": "causal"}, "title"),
    ({"kind": "analysis", "title": "  ", "strength": "causal"}, "title"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "url": ["x"]}, "url"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "summary": 1}, "summary"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "source_system": "x"},
     "external_id"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "source_system": 1,
      "external_id": "x"}, "source_system"),
])
def test_evidencia_nova_malformada_e_recusada(server, decisao, evidence, trecho):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports", "evidence": evidence})
    assert trecho in message


def test_note_precisa_ser_texto(server, decisao):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "note": 7})
    assert "note" in message


def test_evidence_id_e_evidence_juntos_sao_recusados(server, decisao):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")),
        "evidence": {"kind": "analysis", "title": "t", "strength": "causal"}})
    assert "um dos dois" in message


def test_chaves_inesperadas_na_evidencia_sao_ignoradas(server, admin_conn, decisao):
    data = call(server, "attach_evidence", {
        "decision_id": decisao, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "Planilha zeppelin", "strength": "anecdotal",
                     "conformance_level": 2, "normalized": {"x": 1}, "imported_at": "2026-01-01"},
    }).structured_content["data"]
    row = admin_conn.execute(
        "SELECT conformance_level, normalized, imported_at, source_system FROM evidence "
        "WHERE id = %s", (data["evidence_id"],)).fetchone()
    assert tuple(row) == (None, None, None, None)


@pytest.mark.parametrize(("args", "trecho"), [
    ({"title": "   "}, "title"),
    ({"description": ""}, "description"),
    ({"alternatives": [{"description": "a", "rejection_reason": " "}]}, "rejection_reason"),
    ({"alternatives": [{"description": "a"}]}, "rejection_reason"),
    ({"evidence": [{"evidence_id": 3, "role": "supports"}]}, "evidence_id"),
    ({"evidence": [{"evidence_id": str(fid("ev-checkout-confirm")), "role": "apoia"}]}, "role"),
    ({"evidence": [{"evidence_id": str(fid("ev-checkout-confirm")), "role": "supports",
                    "weight": "alto"}]}, "weight"),
    ({"evidence": ["ev-checkout-confirm"]}, "evidence"),
    ({"tags": ["ok", 1]}, "tags"),
])
def test_proposta_malformada_e_recusada_sem_gravar(server, admin_conn, args, trecho):
    title = args.get("title", "Decisão zeppelin malformada")
    message = erro(server, "propose_decision", {**PROPOSTA, "title": title, **args})
    assert trecho in message
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin malformada'") == 0


@pytest.mark.parametrize(("args", "trecho"), [
    ({"summary": "  "}, "summary"),
    ({"decision_ids": [1]}, "decision_ids"),
    ({"evidence_ids": "abc"}, "evidence_ids"),
])
def test_licao_malformada_e_recusada(server, decisao, args, trecho):
    message = erro(server, "record_learning",
                   {"summary": "Uma lição zeppelin", "decision_ids": [decisao], **args})
    assert trecho in message


# ---------------------------------------------------- cobrança e reaproveitamento


def test_favoravel_nao_cobra_contraria_quando_ela_ja_existe(server):
    did = proposta(server, "Decisão zeppelin já contestada", evidence=[
        {"evidence_id": str(fid("ev-checkout-pagina-unica")), "role": "contradicts"}])
    res = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm"))}).structured_content
    assert res["pending"]["on_this_record"] == []


def test_idempotencia_devolve_o_registro_gravado_nao_o_novo(server, admin_conn):
    args = {**PROPOSTA, "title": "Decisão zeppelin gravada primeiro",
            "context": "Havia atraso.", "idempotency_key": "k-gravado",
            "alternatives": [{"description": "a", "rejection_reason": "b"}]}
    a = call(server, "propose_decision", args).structured_content
    assert a["data"]["reused"] is False
    assert a["data"]["missing"] == ["evidence"]
    b = call(server, "propose_decision", {
        **PROPOSTA, "title": "Decisão zeppelin reenviada", "idempotency_key": "k-gravado"})
    data = b.structured_content["data"]
    assert data["reused"] is True
    assert data["decision_id"] == a["data"]["decision_id"]
    assert data["slug"] == a["data"]["slug"]
    assert data["state"] == "proposed"
    assert data["missing"] == ["evidence"], "o que falta é do registro gravado"
    assert b.structured_content["pending"]["on_this_record"] == [writes.PROMPTS["evidence"]]
    assert "não foi aplicado" in b.content[0].text
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin reenviada'") == 0


def test_slug_nao_termina_em_hifen():
    """Corte em 80 caracteres logo antes de um espaço deixaria o hífen no fim."""
    title = "a" * 79 + " zeppelin"
    assert writes.slug_base(title) == "a" * 79


def test_email_do_decisor_com_espacos_e_caixa(server, admin_conn):
    did = proposta(server, "Decisão zeppelin com e-mail folgado",
                   decider_email=f"  {ANA.upper()} ")
    email = admin_conn.execute(
        "SELECT p.email FROM decision d JOIN person p ON p.id = d.decider_person_id "
        "WHERE d.id = %s", (did,)).fetchone()[0]
    assert email == ANA


def test_idempotency_key_em_branco_e_recusada(server, admin_conn):
    title = "Decisão zeppelin com chave em branco"
    message = erro(server, "propose_decision",
                   {**PROPOSTA, "title": title, "idempotency_key": "   "})
    assert "idempotency_key" in message
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


@pytest.mark.parametrize("tag", ["​", "a\tb", "🚀", "#frete"])
def test_tag_fora_do_padrao_do_contrato_e_recusada(server, admin_conn, tag):
    title = "Decisão zeppelin com tag estranha"
    message = erro(server, "propose_decision", {**PROPOSTA, "title": title, "tags": [tag]})
    assert repr(writes.fold(tag).strip()) in message
    assert writes.TAG_PATTERN in message
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


def test_tag_no_padrao_do_contrato_e_aceita(server, admin_conn):
    lid = call(server, "record_learning", {
        "summary": "Lição zeppelin com tags no padrão do contrato de ingestão",
        "decision_ids": [proposta(server, "Decisão zeppelin com tag boa")],
        "tags": ["ok-tag", "Growth/Pricing v2.1", "  "],
    }).structured_content["data"]["learning_id"]
    tags = {r[0] for r in admin_conn.execute(
        "SELECT t.name FROM learning_tag x JOIN tag t ON t.id = x.tag_id "
        "WHERE x.learning_id = %s", (lid,))}
    assert tags == {"ok-tag", "growth/pricing v2.1"}


def test_padrao_de_tag_e_o_do_contrato():
    import json

    from conftest import ROOT
    schema = json.loads((ROOT / "spec" / "experiment-record-v0.schema.json").read_text())
    patterns = set()

    def walk(node):
        if isinstance(node, dict):
            if "tags" in node and isinstance(node["tags"], dict):
                patterns.add(node["tags"]["items"]["pattern"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    assert patterns == {writes.TAG_PATTERN}


def test_user_agent_longo_e_cortado_na_procedencia(server, admin_conn):
    with acting_as(ANA, "cliente/" + "x" * 500):
        res = run(server.call_tool("propose_decision",
                                   {**PROPOSTA, "title": "Decisão zeppelin de cliente prolixo"}))
    did = res.structured_content["data"]["decision_id"]
    ref = admin_conn.execute("SELECT source_ref FROM provenance WHERE object_type = 'decision' "
                             "AND object_id = %s", (did,)).fetchone()[0]
    assert ref == "mcp; client=cliente/" + "x" * (writes.MAX_CLIENT - len("cliente/"))
```

**Passo 2: rodar e ver falhar.** Esperado: `Unknown tool: propose_decision` ou equivalente.

**Passo 3: `server/decision_memory/writes.py`.**

```python
"""Escritas por agente. Tudo nasce 'proposed', com procedência author_kind = agent.

Cada função roda dentro da transação aberta por `server.with_db`: se levantar,
nada fica gravado. O papel dm_app não tem UPDATE nem DELETE, e nas tabelas
provenance, decision, learning e evidence só pode inserir nas colunas que o
agente preenche (db/grants.sql) — nada aqui altera o que já existe.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import identity
from .config import today
from .db import evidence_attested
from .guard import fold

EVIDENCE_KINDS = ("experiment", "study", "analysis", "document", "external")
STRENGTHS = ("causal", "correlational", "anecdotal")
ROLES = ("supports", "contradicts", "discarded")

# Padrão de tag do contrato de ingestão (spec/experiment-record-v0.schema.json);
# há teste que confere que é o mesmo. Tag de agente segue a mesma regra da
# tag importada.
TAG_PATTERN = "^[a-z0-9][a-z0-9 _./-]*$"
# Sem as âncoras, para fullmatch: o '$' aceitaria um '\n' no fim.
_TAG_RE = re.compile(TAG_PATTERN.removeprefix("^").removesuffix("$"))

# Constante, não parâmetro: o banco não impede dm_app de gravar 'human' ou
# 'import' em provenance, então é o servidor que garante que escrita por MCP
# é sempre de agente.
AUTHOR_KIND = "agent"

# O MCP não diz qual modelo está do outro lado, e o clientInfo não chega em HTTP
# sem estado. Não inventamos: 'unknown', e o cliente (User-Agent) vai para
# source_ref. Ver README, "Divergências".
UNKNOWN_MODEL = "unknown"
# O User-Agent vem de quem chama e não tem teto; em source_ref, até isso.
MAX_CLIENT = 200

DECIDER_NOT_FOUND = (
    "Pessoa não encontrada. Confirme o e-mail com o usuário; o registro não foi criado."
)

ORIGIN_FROM_INGESTION = (
    "Identidade de origem (source_system + external_id) só entra pela ingestão do contrato "
    "(spec/), não por agente; nada foi gravado. Registre a evidência sem source_system e "
    "external_id, com a url, ou peça a importação a quem administra."
)

# Mesmas frases do stub: falam só do registro recém-criado.
PROMPTS = {
    "alternatives": "Sem alternativas descartadas: quais opções foram consideradas e por que perderam?",
    "evidence": "Sem evidência vinculada: o que sustentou ou contradisse essa escolha?",
    "context": "Sem contexto: qual era o problema no momento da decisão?",
}
ONLY_SUPPORTS = (
    "Só há evidência favorável registrada até agora: houve algo que contradisse a escolha?"
)


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"{what} inválido: {value}. Use search_evidence para obter o id.") from None


# Validação de tipo. O schema de entrada já barra boa parte, mas não o que vem
# dentro de objetos livres (evidence, itens de evidence); e um tipo errado que
# chegasse ao SQL viraria o erro interno genérico, que não diz o que corrigir.


def _opt_text(value: Any, what: str) -> str | None:
    """Texto opcional; vazio (depois do strip) vale como ausente."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{what} deve ser texto.")
    return value if value.strip() else None


def _req_text(value: Any, what: str) -> str:
    text = _opt_text(value, what)
    if text is None:
        raise ToolError(f"{what} é obrigatório e não pode ficar em branco.")
    return text


def _text_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{what} deve ser uma lista de textos.")
    return value


def _weight(value: Any) -> float | None:
    if value is None:
        return None
    # bool é int em Python; aqui não é peso.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolError("weight deve ser um número de 0 a 1.")
    if not 0 <= value <= 1:
        raise ToolError("weight vai de 0 a 1.")
    return float(value)


def _role(value: Any) -> str:
    if value not in ROLES:
        raise ToolError("role deve ser supports, contradicts ou discarded.")
    return value


def _lock(conn, *parts: object) -> None:
    """Trava de transação sobre uma chave textual; solta sozinha no commit ou rollback.

    A espera pela trava conta no statement_timeout (5 s, db.py): se a outra
    transação demorar mais que isso, esta sai com o erro interno genérico, sem
    gravar nada. Aceitável na v0, em que cada escrita leva milissegundos.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (":".join(str(p) for p in parts),))


def require_principal(conn) -> uuid.UUID:
    email = identity.current_email()
    if email is None:
        raise ToolError(
            "Não consegui identificar sua conta nesta chamada; nada foi gravado. "
            "Conecte pelo gcloud run services proxy (ver README do servidor)."
        )
    row = conn.execute("SELECT id FROM person WHERE lower(email) = lower(%s)",
                       (email,)).fetchone()
    if row is None:
        raise ToolError(
            # Sem o e-mail no texto: o SDK registra a mensagem de erro no log, e
            # quem chama sabe com que conta entrou.
            "Sua conta não está cadastrada no registro. Peça a quem administra "
            "para incluí-la; nada foi gravado."
        )
    return row["id"]


def _provenance(conn, object_type: str, object_id: uuid.UUID, principal: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id, "
        "model, source_ref) VALUES (%s, %s, %s, %s, %s, %s)",
        (object_type, object_id, AUTHOR_KIND, principal, UNKNOWN_MODEL,
         f"mcp; client={(identity.current_client() or 'desconhecido')[:MAX_CLIENT]}"),
    )


def fold_tags(tags: list[str] | None) -> list[str]:
    """Minúsculas, sem acento e sem espaço nas pontas; vazias somem, repetidas também.

    Serve à escrita (por normalize_tags) e ao filtro de tag da busca, para que
    as duas pontas dobrem a tag do mesmo jeito.
    """
    return sorted({fold(tag).strip() for tag in tags or []} - {""})


def normalize_tags(tags: list[str] | None) -> list[str]:
    """fold_tags, e o que sobra tem de casar com TAG_PATTERN, o padrão do contrato.

    Isso também satisfaz o CHECK de tag (name = lower(name) AND name <> ''). Fora
    do padrão (tab, caractere invisível, emoji, '#'), recusa dizendo qual tag.
    """
    names = fold_tags(tags)
    for name in names:
        if not _TAG_RE.fullmatch(name):
            raise ToolError(f"Tag fora do padrão {TAG_PATTERN}: {name!r}. Use letras "
                            "minúsculas, dígitos, espaço e _ . / -; nada foi gravado.")
    return names


def _tags(conn, link_table: str, fk: str, object_id: uuid.UUID, tags: list[str] | None) -> None:
    for name in normalize_tags(tags):
        conn.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING", (object_id, name))


def slug_base(title: str) -> str:
    """Título dobrado, só letras e dígitos separados por hífen, até 80 caracteres."""
    words = "".join(c if c.isalnum() else " " for c in fold(title)).split()
    return "-".join(words)[:80].rstrip("-") or "decisao"


def _slug(conn, title: str) -> str:
    base = slug_base(title)
    # Dois títulos iguais ao mesmo tempo escolheriam o mesmo sufixo.
    _lock(conn, "slug", base)
    taken = {r["slug"] for r in conn.execute(
        "SELECT slug FROM decision WHERE slug = %s OR slug LIKE %s",
        (base, base + "-%")).fetchall()}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _evidence_exists(conn, evidence_id: uuid.UUID) -> None:
    if conn.execute("SELECT 1 FROM evidence WHERE id = %s", (evidence_id,)).fetchone() is None:
        raise ToolError(f"Evidência não encontrada: {evidence_id}. Use search_evidence ou "
                        "envie o objeto evidence.")


def _decision_exists(conn, decision_id: uuid.UUID) -> None:
    if conn.execute("SELECT 1 FROM decision WHERE id = %s", (decision_id,)).fetchone() is None:
        raise ToolError(f"Decisão não encontrada: {decision_id}. Use search_evidence para "
                        "localizá-la.")


def _date(value: str | None) -> date:
    if not value:
        return today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"decided_on inválida: {value}. Use o formato AAAA-MM-DD.") from None


def _stored_decision(conn, principal: uuid.UUID, key: str) -> dict[str, Any] | None:
    """O registro já gravado com essa chave, com o que falta calculado dele mesmo."""
    row = conn.execute(
        "SELECT d.id::text AS id, d.slug, d.state::text AS state, "
        "coalesce(btrim(d.context), '') = '' AS no_context, "
        "NOT EXISTS (SELECT 1 FROM alternative a WHERE a.decision_id = d.id) AS no_alternatives, "
        "NOT EXISTS (SELECT 1 FROM decision_evidence x WHERE x.decision_id = d.id) AS no_evidence "
        "FROM idempotency_key k JOIN decision d ON d.id = k.object_id "
        "WHERE k.principal_person_id = %s AND k.key = %s", (principal, key)).fetchone()
    if row is None:
        return None
    missing = [f for f, absent in (("alternatives", row["no_alternatives"]),
                                   ("evidence", row["no_evidence"]),
                                   ("context", row["no_context"])) if absent]
    return {"decision_id": row["id"], "slug": row["slug"], "state": row["state"],
            "missing": missing, "reused": True}


def _alternatives(value: Any) -> list[tuple[str, str]]:
    bad = ToolError("Cada alternativa precisa de description e rejection_reason, ambos texto "
                    "não vazio.")
    if value is None:
        return []
    if not isinstance(value, list):
        raise bad
    out = []
    for alt in value:
        if not isinstance(alt, dict):
            raise bad
        try:
            out.append((_req_text(alt.get("description"), "description"),
                        _req_text(alt.get("rejection_reason"), "rejection_reason")))
        except ToolError:
            raise bad from None
    return out


def _evidence_links(value: Any) -> list[tuple[uuid.UUID, str, float | None, str | None]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ToolError("evidence deve ser uma lista de objetos {evidence_id, role}.")
    return [(_uuid(_req_text(link.get("evidence_id"), "evidence_id"), "evidence_id"),
             _role(link.get("role")), _weight(link.get("weight")),
             _opt_text(link.get("note"), "note")) for link in value]


def propose_decision(conn, *, title: str, description: str, decider_email: str,
                     context: str | None, decided_on: str | None, door: str,
                     project: str | None, alternatives: list[dict[str, str]] | None,
                     tags: list[str] | None, evidence: list[dict[str, Any]] | None,
                     idempotency_key: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    title = _req_text(title, "title")
    description = _req_text(description, "description")
    context = _opt_text(context, "context")
    alts = _alternatives(alternatives)
    links = _evidence_links(evidence)
    tags = _text_list(tags, "tags")
    normalize_tags(tags)  # recusa tag fora do padrão antes de qualquer escrita
    if idempotency_key is not None and _opt_text(idempotency_key, "idempotency_key") is None:
        # Chave em branco não vira "sem chave" em silêncio: o agente achou que
        # tinha proteção contra duplicata.
        raise ToolError("idempotency_key em branco: envie uma chave não vazia ou omita o "
                        "campo; nada foi gravado.")

    if idempotency_key:
        # Serializa chamadas com a mesma (pessoa, chave): a segunda espera a
        # primeira terminar e então encontra a chave já gravada. O que volta é
        # o registro gravado; o conteúdo desta chamada não é aplicado.
        _lock(conn, "idempotency", principal, idempotency_key)
        stored = _stored_decision(conn, principal, idempotency_key)
        if stored:
            return stored, [PROMPTS[f] for f in stored["missing"]]

    if door not in ("one_way", "two_way"):
        raise ToolError("door deve ser one_way ou two_way.")
    decider = conn.execute(
        "SELECT id FROM person WHERE lower(btrim(email)) = lower(%s)",
        ((_opt_text(decider_email, "decider_email") or "").strip(),)).fetchone()
    if decider is None:
        raise ToolError(DECIDER_NOT_FOUND)
    project = _opt_text(project, "project")
    project_id = None
    if project:
        try:
            as_uuid = uuid.UUID(project)
        except ValueError:
            as_uuid = None
        row = conn.execute("SELECT id FROM project WHERE id = %s OR lower(name) = lower(%s)",
                           (as_uuid, project)).fetchone()
        if row is None:
            raise ToolError(f"Projeto não encontrado: {project}. Confirme o nome com o usuário; "
                            "o registro não foi criado.")
        project_id = row["id"]
    decided = _date(_opt_text(decided_on, "decided_on"))

    slug = _slug(conn, title)
    decision_id = conn.execute(
        "INSERT INTO decision (slug, title, context, description, door, decided_on, "
        "decider_person_id, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (slug, title, context, description, door, decided, decider["id"], project_id),
    ).fetchone()["id"]
    for alt_description, rejection_reason in alts:
        conn.execute("INSERT INTO alternative (decision_id, description, rejection_reason) "
                     "VALUES (%s, %s, %s)", (decision_id, alt_description, rejection_reason))
    _tags(conn, "decision_tag", "decision_id", decision_id, tags)
    for evidence_id, role, weight, note in links:
        _link(conn, decision_id, evidence_id, role, weight, note)
    _provenance(conn, "decision", decision_id, principal)
    if idempotency_key:
        conn.execute("INSERT INTO idempotency_key (principal_person_id, key, object_type, "
                     "object_id) VALUES (%s, %s, 'decision', %s)",
                     (principal, idempotency_key, decision_id))
    missing = [f for f, v in (("alternatives", alts), ("evidence", links),
                              ("context", context)) if not v]
    return ({"decision_id": str(decision_id), "slug": slug, "state": "proposed",
             "missing": missing, "reused": False}, [PROMPTS[f] for f in missing])


def _link(conn, decision_id: uuid.UUID, evidence_id: uuid.UUID, role: str,
          weight: float | None, note: str | None) -> tuple[str, bool]:
    """Vincula; se já havia vínculo, devolve o papel original sem mudar nada."""
    _evidence_exists(conn, evidence_id)
    inserted = conn.execute(
        "INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING role::text AS role",
        (decision_id, evidence_id, role, weight, note)).fetchone()
    if inserted:
        return inserted["role"], False
    existing = conn.execute("SELECT role::text AS role FROM decision_evidence "
                            "WHERE decision_id = %s AND evidence_id = %s",
                            (decision_id, evidence_id)).fetchone()
    return existing["role"], True


def _new_evidence(conn, evidence: Any, principal: uuid.UUID) -> tuple[uuid.UUID, bool]:
    """Resolve o objeto evidence: reaproveita pela origem ou cria sem origem.

    Agente nunca cria evidência com (source_system, external_id): essa
    identidade vem só da ingestão pelo contrato. Chaves que o agente mandar
    além das previstas (conformance_level, normalized, ...) são ignoradas.
    """
    if not isinstance(evidence, dict):
        raise ToolError("evidence deve ser um objeto {kind, title, strength, ...}.")
    title = _opt_text(evidence.get("title"), "title")
    summary = _opt_text(evidence.get("summary"), "summary")
    url = _opt_text(evidence.get("url"), "url")
    system = _opt_text(evidence.get("source_system"), "source_system")
    external = _opt_text(evidence.get("external_id"), "external_id")
    if (system is None) != (external is None):
        raise ToolError("source_system e external_id vão juntos: informe os dois ou nenhum.")
    if system is not None:
        row = conn.execute("SELECT id FROM evidence WHERE source_system = %s AND external_id = %s",
                           (system, external)).fetchone()
        if row is None:
            raise ToolError(ORIGIN_FROM_INGESTION)
        return row["id"], True

    kind, strength = evidence.get("kind"), evidence.get("strength")
    if kind not in EVIDENCE_KINDS:
        raise ToolError(f"kind deve ser um de: {', '.join(EVIDENCE_KINDS)}.")
    if strength not in STRENGTHS:
        raise ToolError(f"strength deve ser um de: {', '.join(STRENGTHS)}.")
    if title is None:
        raise ToolError("A evidência nova precisa de title, texto não vazio.")
    eid = conn.execute(
        "INSERT INTO evidence (kind, title, summary, url, strength) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (kind, title, summary, url, strength)).fetchone()["id"]
    _provenance(conn, "evidence", eid, principal)
    return eid, False


def attach_evidence(conn, *, decision_id: str, role: str, evidence_id: str | None,
                    evidence: dict[str, Any] | None, weight: float | None,
                    note: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    did = _uuid(decision_id, "decision_id")
    _decision_exists(conn, did)
    if evidence_id and evidence:
        raise ToolError("Informe um dos dois, não ambos: evidence_id de uma evidência existente "
                        "ou o objeto evidence.")
    if not evidence_id and not evidence:
        raise ToolError("Informe evidence_id de uma evidência existente ou o objeto evidence.")
    role = _role(role)
    weight = _weight(weight)
    note = _opt_text(note, "note")

    if evidence_id:
        eid, reused = _uuid(_req_text(evidence_id, "evidence_id"), "evidence_id"), True
    else:
        eid, reused = _new_evidence(conn, evidence, principal)

    final_role, already = _link(conn, did, eid, role, weight, note)
    row = conn.execute(
        f"SELECT CASE WHEN {evidence_attested('%s')} THEN 'attested' ELSE 'proposed' END "
        "AS state, EXISTS (SELECT 1 FROM decision_evidence WHERE decision_id = %s "
        "AND role = 'contradicts') AS contested", (eid, did)).fetchone()
    # Só cobra a evidência contrária quando este vínculo é novo, favorável, e a
    # decisão ainda não tem nenhuma contrária.
    nudge = ([ONLY_SUPPORTS] if final_role == "supports" and not already
             and not row["contested"] else [])
    return ({"decision_id": str(did), "evidence_id": str(eid), "role": final_role,
             "reused_existing": reused, "already_linked": already, "state": row["state"]},
            nudge)


def record_learning(conn, *, summary: str, decision_ids: list[str] | None,
                    evidence_ids: list[str] | None, tags: list[str] | None) -> dict[str, Any]:
    principal = require_principal(conn)
    summary = _req_text(summary, "summary")
    decision_ids = _text_list(decision_ids, "decision_ids")
    evidence_ids = _text_list(evidence_ids, "evidence_ids")
    tags = _text_list(tags, "tags")
    normalize_tags(tags)  # recusa tag fora do padrão antes de qualquer escrita
    if not decision_ids and not evidence_ids:
        raise ToolError("Uma lição precisa de origem: informe decision_ids ou evidence_ids.")
    # dict.fromkeys: tira repetidos sem mudar a ordem, e o vínculo não bate na PK.
    dids = list(dict.fromkeys(_uuid(d, "decision_id") for d in decision_ids))
    eids = list(dict.fromkeys(_uuid(e, "evidence_id") for e in evidence_ids))
    for d in dids:
        _decision_exists(conn, d)
    for e in eids:
        _evidence_exists(conn, e)
    lid = conn.execute("INSERT INTO learning (summary, recorded_on) VALUES (%s, %s) RETURNING id",
                       (summary, today())).fetchone()["id"]
    for d in dids:
        conn.execute("INSERT INTO decision_learning (decision_id, learning_id) VALUES (%s, %s)",
                     (d, lid))
    for e in eids:
        conn.execute("INSERT INTO evidence_learning (evidence_id, learning_id) VALUES (%s, %s)",
                     (e, lid))
    _tags(conn, "learning_tag", "learning_id", lid, tags)
    _provenance(conn, "learning", lid, principal)
    return {"learning_id": str(lid), "linked_decisions": [str(d) for d in dids],
            "linked_evidence": [str(e) for e in eids]}
```

Em `server/decision_memory/models.py`, `ProposeData` ganha, depois de `missing`:

```python
    reused: bool = Field(
        False,
        description=(
            "true quando a idempotency_key já tinha registrado esta decisão: volta o registro "
            "gravado e o conteúdo desta chamada não foi aplicado."
        ),
    )
```

**Passo 4: registrar em `server.py`.** No ponto "As escritas entram aqui" (o
comentário sai), acrescente `writes` ao import `from . import ...` e a constante
`WRITE` ao lado de `READ`:

```python
    # junto de READ, no topo do módulo:
    # WRITE = dict(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    # e nos imports: from pydantic import Field

    @server.tool(name="propose_decision", description=DESCRIPTIONS["propose_decision"],
                 annotations=t.ToolAnnotations(idempotent_hint=True, **WRITE))
    def propose_decision(
        title: str,
        description: str,
        decider_email: str,
        context: str | None = None,
        decided_on: str | None = None,
        door: str = "two_way",
        project: str | None = None,
        alternatives: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> Annotated[t.CallToolResult, models.ProposeResponse]:
        def work(conn):
            data, on_this_record = writes.propose_decision(
                conn, title=title, description=description, decider_email=decider_email,
                context=context, decided_on=decided_on, door=door, project=project,
                alternatives=alternatives, tags=tags, evidence=evidence,
                idempotency_key=idempotency_key)
            return data, pending.build(conn, context_tags=set(writes.normalize_tags(tags)),
                                       on_this_record=on_this_record)

        data, block = with_db(pool, work)
        body = models.ProposeResponse(data=models.ProposeData(**data), pending=block)
        if data["reused"]:
            summary = (f"Essa idempotency_key já tinha registrado a decisão {data['decision_id']} "
                       f"({data['state']}); o conteúdo desta chamada não foi aplicado.")
        else:
            summary = (f"Decisão registrada como proposta ({data['decision_id']}). Uma pessoa "
                       "precisa atestá-la e registrar a expectativa; ainda não há superfície "
                       "de atestação, então ela fica proposta.")
        return _result(summary, body, block)

    @server.tool(name="attach_evidence", description=DESCRIPTIONS["attach_evidence"],
                 annotations=t.ToolAnnotations(idempotent_hint=True, **WRITE))
    def attach_evidence(
        decision_id: str,
        role: str,
        evidence_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        # strict: sem ele, "0.5" e true virariam número em silêncio.
        weight: Annotated[float | None, Field(strict=True)] = None,
        note: str | None = None,
    ) -> Annotated[t.CallToolResult, models.AttachResponse]:
        def work(conn):
            data, nudge = writes.attach_evidence(conn, decision_id=decision_id, role=role,
                                                 evidence_id=evidence_id, evidence=evidence,
                                                 weight=weight, note=note)
            tags = {r["name"] for r in conn.execute(
                "SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id "
                "WHERE x.decision_id = %s", (data["decision_id"],)).fetchall()}
            return data, pending.build(conn, context_tags=tags, on_this_record=nudge)

        data, block = with_db(pool, work)
        if data["already_linked"]:
            summary = (f"Evidência {data['evidence_id']} já estava vinculada como "
                       f"{data['role']}; nada mudou.")
        else:
            summary = (f"Evidência {data['evidence_id']} vinculada como {data['role']}"
                       + (" (reaproveitada, não duplicada)." if data["reused_existing"] else "."))
        return _result(summary, models.AttachResponse(data=models.AttachData(**data),
                                                      pending=block), block)

    @server.tool(name="record_learning", description=DESCRIPTIONS["record_learning"],
                 annotations=t.ToolAnnotations(idempotent_hint=False, **WRITE))
    def record_learning(
        summary: str,
        decision_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Annotated[t.CallToolResult, models.LearningResponse]:
        def work(conn):
            data = writes.record_learning(conn, summary=summary, decision_ids=decision_ids,
                                          evidence_ids=evidence_ids, tags=tags)
            nudge = (["Lição muito curta para viajar entre projetos: em que condição ela vale?"]
                     if len(summary.split()) < 8 else [])
            return data, pending.build(conn, context_tags=set(writes.normalize_tags(tags)),
                                       on_this_record=nudge)

        data, block = with_db(pool, work)
        body = models.LearningResponse(data=models.LearningData(**data), pending=block)
        return _result(f"Lição registrada como proposta ({data['learning_id']}).", body, block)
```

**Passo 5: rodar tudo.**

```bash
.venv/bin/python -m pytest server/tests_real -q
```
Esperado: tudo passa, inclusive `test_sao_exatamente_seis_ferramentas` e
`test_descricoes_sao_as_do_mcp_tools`.

**Passo 6: commit.**

```bash
git add server/decision_memory/writes.py server/decision_memory/server.py \
  server/decision_memory/models.py \
  server/tests_real/test_app_writes.py server/tests_real/test_app_surface.py
git commit -m "feat(server): ferramentas de escrita, tudo nasce proposto"
```

---

### Tarefa 11: transporte HTTP, e o caminho inteiro por HTTP

**Arquivos:**
- Criar: `server/decision_memory/app.py`, `server/decision_memory/__main__.py`
- Criar: `server/tests_real/test_app_http.py`

**Passo 1: teste.** Estes são os únicos testes que passam pela cadeia de middlewares de
verdade (a chamada em processo, `server.call_tool`, a pula): identidade vinda do header e
chegando à ferramenta síncrona pelo ContextVar, precedência do X-Serverless-Authorization,
público e emissor do token, recusas de expectativa e de `\x00`, User-Agent na procedência
e a proteção contra DNS rebinding. As propostas criadas são apagadas ao fim do módulo:
ele roda antes de `test_app_seed`, que conta as decisões.

```python
"""O caminho inteiro por HTTP: transporte, middlewares e ferramenta.

São os únicos testes que passam pela cadeia de middlewares de verdade — a
chamada em processo (`server.call_tool`) a pula. Cobrem identidade vinda do
header chegando à ferramenta síncrona (que roda noutra thread), as recusas de
expectativa e de \\x00, e a proteção contra DNS rebinding.
"""

from __future__ import annotations

import base64
import json
import logging
import threading

import pytest
from conftest import ANA
from starlette.testclient import TestClient

from decision_memory import __main__ as entry
from decision_memory.app import build_app, build_http_app
from decision_memory.config import Settings
from decision_memory.guard import EXPECTATION_REFUSAL, NUL_REFUSAL
from decision_memory.server import build_server

AUD = "https://decision-memory-teste.a.run.app"
ISS = "https://accounts.google.com"
CARLA = "carla@exemplo.com.br"
HEADERS = {"accept": "application/json, text/event-stream",
           "content-type": "application/json"}
USER_AGENT = "cliente-http-teste/1.0"
# Marca das propostas deste módulo, apagadas ao final para não mudar as
# contagens dos módulos que rodam depois (test_app_seed, por exemplo).
MARK = "Proposta de teste por HTTP (zeppelin)."


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def bearer(email: str, aud: str = AUD, iss: str = ISS) -> str:
    """ID token como o Cloud Run repassa: sem assinatura."""
    return f"Bearer {_b64({'alg': 'RS256'})}.{_b64({'email': email, 'aud': aud, 'iss': iss})}."


@pytest.fixture(scope="module")
def client(pool, admin_conn):
    app = build_http_app(build_server(pool, (AUD,)), on_cloud_run=False)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c
    with admin_conn.transaction():
        ids = [r[0] for r in admin_conn.execute(
            "SELECT id FROM decision WHERE description = %s", (MARK,))]
        admin_conn.execute("DELETE FROM provenance WHERE object_type = 'decision' "
                           "AND object_id = ANY(%s)", (ids,))
        admin_conn.execute("DELETE FROM decision WHERE id = ANY(%s)", (ids,))


def post(client, method, params, **headers):
    return client.post("/mcp", headers={**HEADERS, **headers},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def rpc(client, method, params, **headers):
    r = post(client, method, params, **headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(client, name, args, **headers):
    return rpc(client, "tools/call", {"name": name, "arguments": args}, **headers)


def text(res) -> str:
    return res["content"][0]["text"]


def count(admin_conn, sql: str, *params) -> int:
    return admin_conn.execute(sql, params).fetchone()[0]


def decisions_titled(admin_conn, title: str) -> int:
    return count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title)


def proposal(title: str, **extra) -> dict:
    # "zeppelin" não casa com nenhuma consulta dos testes de leitura.
    return {"title": title, "description": MARK,
            "decider_email": ANA, **extra}


def principal_of(admin_conn, decision_id: str) -> tuple[str, str]:
    return admin_conn.execute(
        "SELECT p.email, pv.source_ref FROM provenance pv "
        "JOIN person p ON p.id = pv.principal_person_id "
        "WHERE pv.object_type = 'decision' AND pv.object_id = %s", (decision_id,)).fetchone()


def test_initialize(client):
    res = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "teste", "version": "0"}})
    assert res["serverInfo"]["name"] == "decision-memory"


def test_lista_exatamente_as_seis_ferramentas(client):
    names = {tool["name"] for tool in rpc(client, "tools/list", {})["tools"]}
    assert names == {"search_evidence", "get_decision", "propose_decision",
                     "attach_evidence", "record_learning", "list_pending_reviews"}


def test_leitura_sem_identidade_funciona(client):
    res = call(client, "search_evidence", {"query": "checkout"})
    assert res["isError"] is False, res
    assert res["structuredContent"]["data"]["items"]


def test_escrita_com_token_grava_em_nome_da_pessoa(client, admin_conn):
    # A ferramenta é síncrona e roda numa thread do pool; se o ContextVar posto
    # pela middleware não chegasse lá, a escrita seria recusada por falta de conta.
    res = call(client, "propose_decision", proposal("Zeppelin via HTTP com token"),
               authorization=bearer(ANA), **{"user-agent": USER_AGENT})
    assert res["isError"] is False, res
    data = res["structuredContent"]["data"]
    assert data["state"] == "proposed"
    email, source_ref = principal_of(admin_conn, data["decision_id"])
    assert email == ANA
    assert source_ref == f"mcp; client={USER_AGENT}"


def test_x_serverless_prevalece_sobre_authorization_forjado(client, admin_conn):
    res = call(client, "propose_decision", proposal("Zeppelin com Authorization forjado"),
               authorization=bearer(CARLA), **{"x-serverless-authorization": bearer(ANA)})
    assert res["isError"] is False, res
    email, _ = principal_of(admin_conn, res["structuredContent"]["data"]["decision_id"])
    assert email == ANA


def test_x_serverless_ilegivel_nao_cai_para_authorization(client, admin_conn):
    title = "Zeppelin com X-Serverless ilegível"
    res = call(client, "propose_decision", proposal(title),
               authorization=bearer(ANA), **{"x-serverless-authorization": "Bearer lixo"})
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_identidade_nao_vaza_para_a_chamada_seguinte(client, admin_conn):
    ok = call(client, "propose_decision", proposal("Zeppelin antes do anônimo"),
              authorization=bearer(ANA))
    assert ok["isError"] is False, ok
    title = "Zeppelin anônimo logo depois"
    res = call(client, "propose_decision", proposal(title))
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_escrita_sem_token_e_recusada(client, admin_conn):
    title = "Zeppelin anônimo"
    res = call(client, "propose_decision", proposal(title))
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


@pytest.mark.parametrize("token", [
    bearer(ANA, aud="https://outro-servico.a.run.app"),
    bearer(ANA, iss="https://emissor-qualquer.example"),
], ids=["aud-errado", "iss-errado"])
def test_token_de_outro_publico_ou_emissor_e_recusado(client, admin_conn, token):
    title = "Zeppelin com token alheio"
    res = call(client, "propose_decision", proposal(title), authorization=token)
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_agente_que_tenta_passar_confianca_e_recusado(client, admin_conn):
    before = count(admin_conn, "SELECT count(*) FROM learning")
    res = call(client, "record_learning",
               {"summary": "algo zeppelin", "decision_ids": ["x"], "confiança": 0.9},
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert EXPECTATION_REFUSAL in text(res)
    assert "confiança" in text(res)
    assert count(admin_conn, "SELECT count(*) FROM learning") == before


def test_confianca_aninhada_tambem_e_recusada(client, admin_conn):
    title = "Zeppelin com confiança aninhada"
    res = call(client, "propose_decision",
               proposal(title, alternatives=[{"title": "B", "reason": "caro",
                                              "confidence": "alta"}]),
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert EXPECTATION_REFUSAL in text(res)
    assert "alternatives[0].confidence" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_caractere_nulo_e_recusado(client, admin_conn):
    res = call(client, "propose_decision", proposal("Zeppelin com \x00 nulo"),
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert NUL_REFUSAL in text(res)
    assert "title" in text(res)
    # O título tem \x00, que o Postgres nem aceita como parâmetro: busca pelo padrão.
    assert count(admin_conn,
                 "SELECT count(*) FROM decision WHERE title LIKE 'Zeppelin com %%nulo'") == 0


def _request_with_deadline(client, method, seconds=5.0):
    """Faz a requisição numa thread; None se ela não voltar no prazo."""
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(r=client.request(method, "/mcp", headers=HEADERS)),
        daemon=True)
    worker.start()
    worker.join(seconds)
    return box.get("r")


@pytest.mark.parametrize("method", ["GET", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"])
def test_so_post_em_mcp_o_resto_e_405_sem_pendurar(client, method):
    # Sem sessão não há fluxo SSE do servidor nem sessão a encerrar. Antes, o GET
    # abria um stream que nunca fechava; os outros métodos só servem ao POST.
    r = _request_with_deadline(client, method)
    assert r is not None, f"{method} /mcp não respondeu em 5 s"
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_barra_final_tambem_e_so_post(client):
    r = client.request("GET", "/mcp/", headers=HEADERS, follow_redirects=False)
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_post_continua_funcionando_depois_do_405(client):
    assert _request_with_deadline(client, "GET").status_code == 405
    assert len(rpc(client, "tools/list", {})["tools"]) == 6


def test_host_fora_de_localhost_e_recusado_fora_do_cloud_run(client):
    r = post(client, "tools/list", {}, host="decision-memory-teste.a.run.app")
    assert r.status_code == 421


def test_no_cloud_run_host_run_app_e_aceito(pool):
    # Outro servidor: o gerenciador de sessões de um app só sobe uma vez.
    app = build_http_app(build_server(pool, (AUD,)), on_cloud_run=True)
    with TestClient(app, base_url="https://decision-memory-teste.a.run.app") as c:
        r = post(c, "tools/list", {})
    assert r.status_code == 200, r.text
    assert len(r.json()["result"]["tools"]) == 6


def test_build_app_fecha_o_pool_ao_desligar(app_url):
    app = build_app(Settings(database_url=app_url, database_password=None,
                             expected_audiences=(AUD,), on_cloud_run=False))
    with TestClient(app, base_url="http://localhost:8080") as c:
        assert not app.state.pool.closed
        assert len(rpc(c, "tools/list", {})["tools"]) == 6
    assert app.state.pool.closed


def test_build_app_cala_o_log_do_sdk_abaixo_de_warning(app_url):
    # O SDK registra em INFO o texto de erro das ferramentas.
    app = build_app(Settings(database_url=app_url, database_password=None,
                             expected_audiences=(), on_cloud_run=False))
    app.state.pool.close()
    assert logging.getLogger("mcp").level == logging.WARNING


def test_no_cloud_run_uvicorn_confia_no_proxy():
    assert entry.uvicorn_options({"K_SERVICE": "decision-memory", "PORT": "9000"}) == {
        "host": "0.0.0.0", "port": 9000, "forwarded_allow_ips": "*"}
    assert entry.uvicorn_options({}) == {"host": "0.0.0.0", "port": 8080}
```

Se a resposta vier em SSE em vez de JSON, confira que `build_http_app` passa
`json_response=True`.

**Passo 2: rodar e ver falhar** (import de `decision_memory.app`).

**Passo 3: `server/decision_memory/app.py`.**

```python
"""App ASGI: Streamable HTTP sem estado em /mcp, como pede MCP_TOOLS.md."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, db
from .server import build_server


MCP_PATH = "/mcp"


class PostOnly:
    """Em /mcp (e /mcp/), só POST; qualquer outro método recebe 405 `Allow: POST`.

    Sem estado, não há sessão: nada que o servidor possa empurrar por um fluxo
    SSE (GET), nem sessão a encerrar (DELETE). O SDK, porém, aceita o GET e
    abre um stream que nunca fecha — no Cloud Run isso prende a instância,
    ocupada e cobrada, até o timeout da requisição. A especificação do
    Streamable HTTP permite 405 quando o servidor não oferece esse fluxo. Os
    demais métodos não fazem sentido no transporte; respondem igual, com o
    header Allow que o 405 pede.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["method"] != "POST"
                and scope["path"].rstrip("/") == MCP_PATH):
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600,
                           "message": "Method Not Allowed: servidor sem estado, só POST."}},
                status_code=405, headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_http_app(server: MCPServer, *, on_cloud_run: bool) -> Starlette:
    """Sem estado e com resposta JSON: cada POST é uma requisição completa.

    Sem estado porque o Cloud Run distribui as requisições entre instâncias; o
    clientInfo do initialize, portanto, não chega às chamadas seguintes — o
    cliente é identificado pelo User-Agent de cada requisição.
    """
    # A proteção contra DNS rebinding recusa (421) Host fora de localhost. No
    # Cloud Run o Host é o domínio run.app e o serviço é fechado por IAM — um
    # navegador sem token não chega aqui —, então ela é desligada só lá.
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=False)
                if on_cloud_run else None)
    app = server.streamable_http_app(stateless_http=True, json_response=True,
                                     transport_security=security)
    app.add_middleware(PostOnly)
    return app


def close_pool_on_shutdown(app: Starlette, pool: ConnectionPool) -> None:
    """Compõe o lifespan do SDK (que sobe o gerenciador de sessões) com o fechamento
    do pool, para as conexões saírem limpas quando o Cloud Run para a instância."""
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[Any]:
        try:
            async with sdk_lifespan(a) as state:
                yield state
        finally:
            pool.close()

    app.router.lifespan_context = lifespan


def build_app(settings: config.Settings | None = None) -> Starlette:
    # Uma linha por evento no stdout: o Cloud Logging captura sem configuração.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # O SDK registra em INFO o texto dos erros de ferramenta, que pode trazer dado
    # de quem chama; do SDK, só aviso e erro.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    settings = settings or config.load()
    pool = db.make_pool(settings.database_url, settings.database_password)
    server = build_server(pool, settings.expected_audiences)
    app = build_http_app(server, on_cloud_run=settings.on_cloud_run)
    close_pool_on_shutdown(app, pool)
    app.state.pool = pool
    return app
```

**Passo 4: `server/decision_memory/__main__.py`.**

```python
"""Entrada: `python -m decision_memory`. Escuta em $PORT (Cloud Run) ou 8080."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import uvicorn

from .app import build_app


def uvicorn_options(environ: Mapping[str, str]) -> dict[str, Any]:
    options: dict[str, Any] = {"host": "0.0.0.0", "port": int(environ.get("PORT", "8080"))}
    if "K_SERVICE" in environ:
        # No Cloud Run só o front end do Google chega ao container, e é ele que
        # põe X-Forwarded-Proto/For: confiar neles mantém https nos redirects
        # (/mcp/ → /mcp) em vez de apontar para http.
        options["forwarded_allow_ips"] = "*"
    return options


def main() -> None:
    uvicorn.run(build_app(), **uvicorn_options(os.environ))


if __name__ == "__main__":
    main()
```

O `with TestClient(app)` é obrigatório: é ele que sobe o lifespan do gerenciador de
sessões. Cada app só sobe esse gerenciador uma vez, por isso o teste do Cloud Run
constrói outro servidor.

**Passo 5: rodar tudo.** `.venv/bin/python -m pytest server/tests_real -q` → tudo passa.

**Passo 6: teste de fumaça local.**

```bash
PYTHONPATH=server DM_DATABASE_URL="$(python3 -c "print('$DM_TEST_ADMIN_URL'.replace('postgres:postgres@','dm_app:dm_app_test@'))")" \
  .venv/bin/python -m decision_memory &
sleep 2
curl -s localhost:8080/mcp -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_evidence","arguments":{"query":"checkout"}}}' | head -c 400
kill %1
```
Esperado: JSON com `"items"` contendo "Checkout sem etapa de confirmação". E
`curl -si localhost:8080/mcp` (GET) responde na hora `405` com `allow: POST`: sem o
`PostOnly`, o SDK abria um stream SSE que nunca fechava e o curl ficava pendurado.

**Passo 7: commit.**

```bash
git add server/decision_memory/app.py server/decision_memory/__main__.py server/tests_real/test_app_http.py \
  docs/plans/2026-09-23-servidor-mcp-gcp.md
git commit -m "feat(server): Streamable HTTP sem estado e teste ponta a ponta por HTTP"
```

---

### Tarefa 12: container e CI

**Arquivos:**
- Criar: `server/Dockerfile`, `server/.dockerignore`
- Modificar: `.github/workflows/ci.yml`
- Modificar: `CLAUDE.md`

**Passo 1: `server/Dockerfile`.** O contexto de build é `server/`.

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt

COPY decision_memory ./decision_memory

ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "-m", "decision_memory"]
```

`server/.dockerignore`:

```
*
!requirements-app.txt
!decision_memory/
decision_memory/__pycache__/
```

**Passo 2: build local.**

```bash
docker build -t decision-memory:dev server
```
Esperado: build conclui. (Rodar o container exige `DM_DATABASE_URL` alcançável; opcional.)

**Passo 3: job de CI.** Em `.github/workflows/ci.yml`, depois do job `stub`:

```yaml
  app:
    name: Servidor MCP
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: decision_memory_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd="pg_isready -U postgres"
          --health-interval=5s
          --health-timeout=5s
          --health-retries=10

    env:
      DM_TEST_ADMIN_URL: postgresql://postgres:postgres@localhost:5432/decision_memory_test

    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: Instalar as dependências do servidor
        run: pip install -r server/requirements-app-test.txt

      # Schema, grants e fixtures são aplicados do zero pela própria suíte.
      - name: Rodar os testes do servidor
        run: python -m pytest server/tests_real -q

      # A imagem que vai ao Cloud Run (gcloud run deploy --source server) tem de
      # construir; o build pega Dockerfile, requirements e .dockerignore quebrados.
      - name: Construir a imagem do servidor
        run: docker build -t decision-memory:ci server
```

**Passo 4: CLAUDE.md.** Em "Rodar os testes", troque "Os quatro abaixo" por "Os cinco abaixo"
e acrescente ao fim:

````markdown
### Servidor MCP

Precisa de um PostgreSQL 16 com um banco vazio **cujo nome termine em `_test`**: a suíte
recria o schema `public`, aplica `schema.sql` e `grants.sql` e carrega as fixtures.

```bash
pip install -r server/requirements-app-test.txt
DM_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/decision_memory_test \
  python -m pytest server/tests_real -q
```
````

**Passo 5: verificar tudo o que a CI roda.**

```bash
python3 scripts/check_links.py
.venv/bin/python -m pytest server/tests -q
.venv/bin/python -m pytest server/tests_real -q
```

**Passo 6: commit.**

```bash
git add server/Dockerfile server/.dockerignore .github/workflows/ci.yml CLAUDE.md
git commit -m "ci: container e suíte do servidor contra Postgres"
```

---

### Tarefa 13: README do servidor, com deploy e divergências

**Arquivos:**
- Criar: `server/decision_memory/README.md`
- Modificar: `README.md` (seções "Estrutura" e "Estado")
- Modificar: `docs/plans/2026-09-23-servidor-mcp-gcp-design.md` (correções da implementação)

**Passo 1: `server/decision_memory/README.md`.**

````markdown
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
#    allUsers: qualquer um chama sem token, e um Authorization forjado chega intacto ao servidor.
#    allAuthenticatedUsers: qualquer conta Google do mundo lê o registro, não só quem recebeu acesso.
#    invoker-iam-disabled: o Cloud Run pula a checagem de IAM e não valida token nenhum, mesmo
#    sem allUsers na política — o mesmo que aberto.
POLICY=$(gcloud run services get-iam-policy decision-memory --project $PROJECT \
  --region $REGION --format json)
IAM_OFF=$(gcloud run services describe decision-memory --project $PROJECT --region $REGION \
  --format='value(metadata.annotations["run.googleapis.com/invoker-iam-disabled"])')
if printf '%s' "$POLICY" | grep -qE 'allUsers|allAuthenticatedUsers' || [ "$IAM_OFF" = "true" ]; then
  echo "ABERTO — remova allUsers/allAuthenticatedUsers e religue o IAM (--invoker-iam-check) antes de usar"
else
  echo "fechado"
fi

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
   fora de `DM_EXPECTED_AUDIENCE` e do cliente do gcloud, ou `iss` que não é o Google). Nos
   logs do serviço, a linha `identity_rejected` diz o motivo (`no_bearer`, `unreadable`,
   `bad_iss`, `bad_aud`, `unverified`, `no_email`), o header lido e o `aud`/`iss` recusados —
   nunca o e-mail nem o token. Sem nenhuma linha dessas, o token nem chegou. Se voltar "Sua
   conta não está cadastrada", falta o e-mail em `person`.
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
````

**Passo 2: README raiz.** Em "Estrutura", troque a linha de `server/` por:

```
└── server/
    ├── decision_memory_stub/          stub MCP sobre fixtures, para testar invocação
    └── decision_memory/               servidor MCP sobre Postgres, para o Cloud Run
```

E em "Estado", acrescente depois do parágrafo existente:

```markdown
Há também um servidor de verdade em [`server/decision_memory/`](server/decision_memory/README.md):
as seis ferramentas sobre o Postgres, publicado no Cloud Run e acessado por IAM. Sem OAuth e sem
superfície de atestação ainda.
```

**Passo 3: corrigir o design.** Em `docs/plans/2026-09-23-servidor-mcp-gcp-design.md`,
acrescente ao fim:

```markdown
## Correções durante o plano

- **Identidade:** o Cloud Run entrega o token sem assinatura; não há revalidação com
  `google-auth`. O servidor lê as claims e confere `aud` e `iss`. Por isso o serviço fechado é
  requisito de segurança, verificado no deploy. Com `X-Serverless-Authorization` presente, a
  identidade vem só dele, o único header que o Cloud Run confere quando vêm os dois.
  `DM_EXPECTED_AUDIENCE` é obrigatório no Cloud Run e vai já no primeiro deploy.
- **`provenance.model`:** o `clientInfo` não chega em modo sem estado. Fica `unknown`, e o
  `User-Agent` vai para `source_ref`.
- **Busca:** `websearch_to_tsquery` exige todos os termos e perguntas em linguagem natural não
  achariam nada. Termos em OR, com a regra de relevância do stub portada.
- **Carga:** `python -m decision_memory.seed`, ids uuid5 das fixtures, em vez de
  `scripts/load_fixtures.py`.
- **`attach_evidence`:** agente não cria evidência com `source_system`/`external_id`, de
  qualquer tipo; identidade de origem entra só pela ingestão.
```

**Passo 4: verificar e commitar.**

```bash
python3 scripts/check_links.py
git add server/decision_memory/README.md README.md docs/plans/2026-09-23-servidor-mcp-gcp-design.md
git commit -m "docs(server): deploy, uso, verificação manual e divergências"
```

---

### Tarefa 14: deploy (com a pessoa, não sozinho)

Não executar sem combinar. Cria recursos no projeto `ufpr-ppgcd`, gera senha e mexe no banco
compartilhado.

1. Mostrar o diff da branch inteira (`git diff main...HEAD --stat` e o diff completo) e esperar
   aprovação antes de qualquer push — regra do `CLAUDE.md`.
2. Pedir: e-mail de quem atesta a carga, o CSV de pessoas reais e quem recebe `run.invoker`.
3. Seguir o roteiro "Deploy" do README do servidor, passo a passo, mostrando a saída de cada um.
4. Rodar a "Verificação manual". Registrar o resultado no PR, incluindo o que falhou.
