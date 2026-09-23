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
    # Públicos aceitos no ID token (a URL do serviço). Vazio desliga a checagem,
    # o que só é permitido fora do Cloud Run.
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


def make_pool(url: str, password: str | None = None) -> ConnectionPool:
    kwargs: dict = {"row_factory": dict_row}
    if password:
        kwargs["password"] = password
    # Cloud SQL e o Auth Proxy derrubam conexão ociosa: o pool testa a conexão
    # antes de entregá-la e descarta as que ficaram paradas mais de 5 minutos.
    return ConnectionPool(url, kwargs=kwargs, min_size=1, max_size=4, open=True,
                          check=ConnectionPool.check_connection, max_idle=300)
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

import psycopg
import pytest

from decision_memory import seed


def test_servidor_conecta_como_dm_app(pool):
    with pool.connection() as conn:
        assert conn.execute("SELECT current_user AS u").fetchone()["u"] == "dm_app"


def test_dm_app_nao_escreve_expectativa(pool):
    # Linha válida (decisão proposta, sem revisão feita): só o privilégio a recusa.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric,"
                " expected_magnitude, due_on) VALUES (%s, %s, 0.7, 'm', '+1 p.p.', '2027-01-01')",
                (seed.fid("dec-push-diario"), seed.fid("p-ana")),
            )
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
from psycopg.types.json import Jsonb

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


def _tags(cur, link_table: str, fk: str, object_id: uuid.UUID, names: list[str]) -> None:
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
            _tags(cur, "evidence_tag", "evidence_id", fid(e["id"]), e.get("tags", []))
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
            _tags(cur, "decision_tag", "decision_id", did, d.get("tags", []))
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
            _tags(cur, "learning_tag", "learning_id", lid, ln.get("tags", []))
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

from decision_memory.guard import forbidden_keys
from decision_memory.identity import (
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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_email: ContextVar[str | None] = ContextVar("dm_principal_email", default=None)
_client: ContextVar[str | None] = ContextVar("dm_client", default=None)

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def _audience_ok(aud: object, audiences: tuple[str, ...]) -> bool:
    """`aud` de um JWT pode ser string ou lista; basta um elemento conferir."""
    if not audiences:
        return True
    if isinstance(aud, str):
        return aud in audiences
    if isinstance(aud, list):
        return any(isinstance(a, str) and a in audiences for a in aud)
    return False


def email_from_authorization(header: str | None, audiences: tuple[str, ...]) -> str | None:
    """E-mail do ID token no header, ou None se não houver identidade aceitável.

    Sem `audiences` configurado, o público não é conferido. O emissor tem de
    ser o Google sempre.
    """
    if not header or not header.lower().startswith("bearer "):
        return None
    parts = header[7:].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None
    if not _audience_ok(claims.get("aud"), audiences):
        return None
    # Só False recusa: a claim falta em alguns tokens federados, e o header já
    # foi verificado pela plataforma.
    if claims.get("email_verified") is False:
        return None
    email = claims.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def email_from_headers(headers: Mapping[str, str], audiences: tuple[str, ...]) -> str | None:
    """E-mail do header que o Cloud Run verificou.

    Com X-Serverless-Authorization presente, só ele conta; o Authorization é
    ignorado, porque nesse caso a plataforma não o conferiu.
    """
    serverless = headers.get("x-serverless-authorization")
    if serverless is not None:
        return email_from_authorization(serverless, audiences)
    return email_from_authorization(headers.get("authorization"), audiences)


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

Curto, específico e ligado ao que acabou de acontecer. Vem vazio, nunca ausente.
"""

from __future__ import annotations

from .config import today
from .models import Pending, ReviewDue

MAX_REVIEWS_DUE = 3

UNATTESTED_COUNT_SQL = """
SELECT (SELECT count(*) FROM decision WHERE state = 'proposed')
     + (SELECT count(*) FROM learning WHERE state = 'proposed')
     + (SELECT count(*) FROM evidence e
         WHERE NOT EXISTS (SELECT 1 FROM provenance p
                            WHERE p.object_type = 'evidence' AND p.object_id = e.id
                              AND p.attested_at IS NOT NULL)) AS n
"""

OVERDUE_SQL = """
SELECT d.id::text AS decision_id, d.title, r.due_on,
       ARRAY(SELECT t.name FROM decision_tag dt JOIN tag t ON t.id = dt.tag_id
              WHERE dt.decision_id = d.id) AS tags
  FROM review r JOIN decision d ON d.id = r.decision_id
 WHERE r.done_on IS NULL AND r.due_on <= %(today)s
"""


def _hint(due: list[ReviewDue], unattested: int, context_tags: set[str]) -> str | None:
    if due:
        tag = next(iter(sorted(context_tags)), None)
        escopo = f" relacionada à tag {tag}" if tag else ""
        plural = "revisões vencidas" if len(due) > 1 else "revisão vencida"
        return f"Há {len(due)} {plural}{escopo}. Mencione ao usuário antes de prosseguir."
    if unattested:
        return (
            f"{unattested} registros aguardam atestação de uma pessoa. "
            "Confiança e resultado esperado são preenchidos nesse momento, não aqui."
        )
    return None


def build(conn, *, context_tags: set[str] | None = None,
          on_this_record: list[str] | None = None) -> Pending:
    context_tags = context_tags or set()
    reference = today()
    scored = []
    for row in conn.execute(OVERDUE_SQL, {"today": reference}).fetchall():
        shares_tag = 1 if context_tags & set(row["tags"]) else 0
        overdue = (reference - row["due_on"]).days
        scored.append((shares_tag, overdue, ReviewDue(
            decision_id=row["decision_id"], title=row["title"],
            due_on=row["due_on"].isoformat(), overdue_days=overdue)))
    scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
    due = [r[2] for r in scored[:MAX_REVIEWS_DUE]]
    unattested = conn.execute(UNATTESTED_COUNT_SQL).fetchone()["n"]
    return Pending(
        on_this_record=on_this_record or [],
        reviews_due=due,
        unattested_count=unattested,
        hint=_hint(due, unattested, context_tags),
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
por id. Nunca por efeito.

**Passo 1: `reads.py`.**

```python
"""Consultas de leitura: busca, decisão e revisões pendentes."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import models
from .config import today
from .guard import fold

STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem
    e ou que se ao aos à às pelo pela como mais menos ja já nao não sobre entre
    """.split()
)

EVIDENCE_STATE = """
CASE WHEN EXISTS (SELECT 1 FROM provenance p WHERE p.object_type = 'evidence'
                   AND p.object_id = e.id AND p.attested_at IS NOT NULL)
     THEN 'attested' ELSE 'proposed' END
"""

# Termos em OR: a relevância é decidida em Python, com a mesma regra do stub.
_HITS = "(SELECT count(*) FROM unnest(%(terms)s::text[]) term WHERE {alias}.search @@ to_tsquery('simple', term))"
_CANDIDATE = "(%(any)s::text IS NULL OR {alias}.search @@ to_tsquery('simple', %(any)s) OR {tags} && %(folded)s::text[])"

_EV_TAGS = "ARRAY(SELECT t.name FROM evidence_tag x JOIN tag t ON t.id = x.tag_id WHERE x.evidence_id = e.id)"
_LN_TAGS = "ARRAY(SELECT t.name FROM learning_tag x JOIN tag t ON t.id = x.tag_id WHERE x.learning_id = l.id)"
_DC_TAGS = "ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id WHERE x.decision_id = d.id)"

SEARCH_EVIDENCE_SQL = f"""
SELECT e.id::text AS id, e.title, e.summary, e.strength::text AS strength,
       e.source_system, e.external_id, e.url, {_EV_TAGS} AS tags,
       {_HITS.format(alias="e")} AS hits, {EVIDENCE_STATE} AS state
  FROM evidence e
 WHERE {_CANDIDATE.format(alias="e", tags=_EV_TAGS)}
   AND (%(kinds)s::text[] IS NULL OR e.kind::text = ANY(%(kinds)s))
   AND (%(source_system)s::text IS NULL OR e.source_system = %(source_system)s)
   AND (%(tags)s::text[] IS NULL OR {_EV_TAGS} && %(tags)s::text[])
"""

SEARCH_LEARNING_SQL = f"""
SELECT l.id::text AS id, l.summary AS title, NULL AS summary, l.state::text AS state,
       {_LN_TAGS} AS tags, {_HITS.format(alias="l")} AS hits
  FROM learning l
 WHERE {_CANDIDATE.format(alias="l", tags=_LN_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_LN_TAGS} && %(tags)s::text[])
"""

SEARCH_DECISION_SQL = f"""
SELECT d.id::text AS id, d.title, d.description AS summary, d.state::text AS state,
       {_DC_TAGS} AS tags, {_HITS.format(alias="d")} AS hits
  FROM decision d
 WHERE {_CANDIDATE.format(alias="d", tags=_DC_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_DC_TAGS} && %(tags)s::text[])
"""


def terms(text: str) -> list[str]:
    """Palavras da consulta, em minúsculas e com acento (o dicionário 'simple'
    guarda o acento), sem stopwords e sem repetição."""
    raw = "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
    out: list[str] = []
    for word in raw:
        if len(word) > 2 and fold(word) not in STOPWORDS and word not in out:
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
    include = include or ["evidence", "learning", "decision"]
    if kinds or source_system:
        # Tipo e origem são atributos de evidência; pedir por eles restringe a busca.
        include = ["evidence"]
    limit = max(1, min(int(limit), 50))
    wanted_tags = [fold(tag) for tag in (tags or [])]
    words = terms(query) + [w for w in wanted_tags if w not in terms(query)]
    folded = [fold(w) for w in words]
    params: dict[str, Any] = {
        "terms": words, "folded": folded,
        "any": " | ".join(words) or None,
        "kinds": kinds or None, "source_system": source_system,
        "tags": wanted_tags or None,
    }

    scored: list[tuple[float, models.SearchItem]] = []

    def keep(row: dict, item: models.SearchItem) -> None:
        tag_hits = len(set(folded) & set(row["tags"]))
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
         WHERE d.id = %s OR d.slug = %s
         LIMIT 1
        """,
        (_as_uuid(id), slug),
    ).fetchone()
    if row is None:
        raise ToolError(
            f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
        )
    did = row["id"]

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
            "FROM review WHERE decision_id = %s ORDER BY due_on", (did,)).fetchall()
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
         ORDER BY pv.created_at LIMIT 1
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
           AND (%(project)s::text IS NULL OR pr.name ILIKE '%%' || %(project)s || '%%')
        """,
        {"owner": owner_email, "project": project},
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
    reviews.sort(key=lambda r: r.overdue_days, reverse=True)

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
```

**Passo 2: testes de leitura.** `server/tests_real/test_app_reads.py`:

```python
from __future__ import annotations

import pytest
from conftest import run

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


def test_ordem_da_busca_nao_depende_do_efeito(server, admin_conn):
    """ADR 0003: ranquear por efeito compara grandezas de origens diferentes."""
    consulta = {"query": "checkout conversão"}
    antes = [i["id"] for i in call(server, "search_evidence", consulta).structured_content["data"]["items"]]
    admin_conn.execute(
        "UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', "
        "to_jsonb((normalized->'effect'->>'point')::numeric * 100 + 10)) "
        "WHERE normalized ? 'effect'")
    try:
        depois = [i["id"] for i in call(server, "search_evidence", consulta).structured_content["data"]["items"]]
    finally:
        admin_conn.execute(
            "UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', "
            "to_jsonb(((normalized->'effect'->>'point')::numeric - 10) / 100)) "
            "WHERE normalized ? 'effect'")
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
    except psycopg.Error as exc:
        ref = uuid.uuid4().hex[:8]
        bug = isinstance(exc, psycopg.errors.InsufficientPrivilege)
        log.error(json.dumps({"event": "db_error", "ref": ref, "bug": bug,
                              "sqlstate": exc.sqlstate, "error": str(exc)}))
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
        summary = (f"{len(items)} de {total} resultados para '{query}'." if items
                   else f"Nenhum resultado para '{query}'.")
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

    # Ordem importa: a identidade é resolvida antes da guarda e da ferramenta.
    server.middleware.append(identity.middleware(audiences))
    server.middleware.append(guard.refuse_expectation)
    return server
```

Atenção a `test_duas_revisoes_vencidas`: sem identidade, `owner` é `None` e a lista não filtra.
Nos testes as chamadas diretas não passam pela middleware, então `current_email()` é `None`.

**Passo 5: rodar e ver passar** (exceto as ferramentas de escrita, que o teste de superfície
ainda não encontra).

```bash
.venv/bin/python -m pytest server/tests_real -q
```
Esperado: `test_sao_exatamente_seis_ferramentas` e `test_descricoes_sao_as_do_mcp_tools` falham
(só três ferramentas); o resto passa. Se algum teste de busca falhar, **não mexa na regra de
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

**Passo 1: testes.** `server/tests_real/test_app_writes.py`:

```python
from __future__ import annotations

import pytest
from conftest import ANA, run

from decision_memory.identity import acting_as
from decision_memory.seed import fid
from decision_memory.server import build_server


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args, as_email=ANA):
    with acting_as(as_email):
        return run(server.call_tool(name, args))


PROPOSTA = {
    "title": "Adotar entrega no mesmo dia na capital",
    "description": "Entregar no mesmo dia para pedidos até as 14h.",
    "decider_email": ANA,
}


def test_proposta_nasce_proposed_e_lista_o_que_falta(server, admin_conn):
    data = call(server, "propose_decision", PROPOSTA).structured_content["data"]
    assert data["state"] == "proposed"
    assert data["attest_url"] is None
    assert set(data["missing"]) == {"alternatives", "evidence", "context"}
    kind, model, principal = admin_conn.execute(
        "SELECT pv.author_kind::text, pv.model, p.email FROM provenance pv "
        "JOIN person p ON p.id = pv.principal_person_id "
        "WHERE pv.object_type = 'decision' AND pv.object_id = %s", (data["decision_id"],)
    ).fetchone()
    assert (kind, model, principal) == ("agent", "unknown", ANA)


def test_sem_identidade_nao_escreve(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", PROPOSTA, as_email=None)
    assert "nada foi gravado" in str(excinfo.value)


def test_conta_nao_cadastrada_nao_escreve(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", PROPOSTA, as_email="estranha@exemplo.com")
    assert "não está cadastrada" in str(excinfo.value)


def test_decisor_desconhecido_nao_cria_registro(server, admin_conn):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": "Decisão fantasma",
                                          "decider_email": "ninguem@exemplo.com"})
    assert "Pessoa não encontrada" in str(excinfo.value)
    assert admin_conn.execute(
        "SELECT count(*) FROM decision WHERE title = 'Decisão fantasma'").fetchone()[0] == 0


def test_erro_no_meio_desfaz_tudo(server, admin_conn):
    with pytest.raises(Exception):
        call(server, "propose_decision", {
            **PROPOSTA, "title": "Decisão com evidência inválida",
            "evidence": [{"evidence_id": "00000000-0000-0000-0000-000000000000",
                          "role": "supports"}]})
    assert admin_conn.execute(
        "SELECT count(*) FROM decision WHERE title = 'Decisão com evidência inválida'"
    ).fetchone()[0] == 0


def test_idempotency_key_devolve_o_mesmo_registro(server):
    args = {**PROPOSTA, "title": "Decisão idempotente", "idempotency_key": "k-123"}
    a = call(server, "propose_decision", args).structured_content["data"]
    b = call(server, "propose_decision", args).structured_content["data"]
    assert a["decision_id"] == b["decision_id"]


def test_slug_repetido_ganha_sufixo(server):
    a = call(server, "propose_decision", {**PROPOSTA, "title": "Título repetido"})
    b = call(server, "propose_decision", {**PROPOSTA, "title": "Título repetido"})
    assert a.structured_content["data"]["slug"] != b.structured_content["data"]["slug"]


def test_evidencia_ja_existente_e_reaproveitada(server):
    dec = call(server, "propose_decision", {**PROPOSTA, "title": "Decisão para anexar"})
    did = dec.structured_content["data"]["decision_id"]
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "outro título", "strength": "causal",
                     "source_system": "internal.lab", "external_id": "exp-2025-0311"},
    }).structured_content["data"]
    assert data["reused_existing"] is True
    assert data["evidence_id"] == str(fid("ev-checkout-pagina-unica"))


def test_evidencia_nova_de_agente_aparece_proposta_na_busca(server):
    dec = call(server, "propose_decision", {**PROPOSTA, "title": "Decisão com evidência nova"})
    did = dec.structured_content["data"]["decision_id"]
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence": {"kind": "analysis", "title": "Análise de entregas zeppelin",
                     "strength": "correlational"},
    }).structured_content["data"]
    assert data["state"] == "proposed"
    busca = call(server, "search_evidence", {"query": "zeppelin"}).structured_content["data"]
    assert [i["state"] for i in busca["items"]] == ["proposed"]


def test_vinculo_repetido_nao_muda_nada(server):
    did = str(fid("dec-remover-confirmacao"))
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "discarded", "evidence_id": str(fid("ev-checkout-confirm")),
    }).structured_content["data"]
    assert data["already_linked"] is True
    assert data["role"] == "supports", "o papel original não pode ser trocado por agente"


def test_experimento_de_plataforma_novo_nao_entra_por_agente(server):
    did = str(fid("dec-remover-confirmacao"))
    with pytest.raises(Exception) as excinfo:
        call(server, "attach_evidence", {
            "decision_id": did, "role": "supports",
            "evidence": {"kind": "experiment", "title": "x", "strength": "causal",
                         "source_system": "growthbook", "external_id": "exp_inedito"}})
    assert "ingestão" in str(excinfo.value)


def test_licao_precisa_de_origem(server):
    with pytest.raises(Exception):
        call(server, "record_learning", {"summary": "algo"})


def test_licao_nasce_proposta(server):
    data = call(server, "record_learning", {
        "summary": "Clientes aceitam frete mais caro quando a entrega é no mesmo dia na capital",
        "decision_ids": [str(fid("dec-frete-gratis-99"))], "tags": ["frete"],
    }).structured_content["data"]
    assert data["state"] == "proposed"
```

**Passo 2: rodar e ver falhar.** Esperado: `Unknown tool: propose_decision` ou equivalente.

**Passo 3: `server/decision_memory/writes.py`.**

```python
"""Escritas por agente. Tudo nasce 'proposed', com procedência author_kind = agent.

Cada função roda dentro da transação aberta por `server.with_db`: se levantar,
nada fica gravado. O papel dm_app não tem UPDATE nem DELETE — nada aqui altera
o que já existe.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import identity
from .config import today
from .guard import fold

EVIDENCE_KINDS = ("experiment", "study", "analysis", "document", "external")
STRENGTHS = ("causal", "correlational", "anecdotal")
ROLES = ("supports", "contradicts", "discarded")

# O MCP não diz qual modelo está do outro lado. Não inventamos: 'unknown', e o
# cliente (User-Agent) vai para source_ref. Ver README, "Divergências".
UNKNOWN_MODEL = "unknown"


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"{what} inválido: {value}. Use search_evidence para obter o id.") from None


def require_principal(conn) -> uuid.UUID:
    email = identity.current_email()
    if email is None:
        raise ToolError(
            "Não consegui identificar sua conta nesta chamada; nada foi gravado. "
            "Conecte pelo gcloud run services proxy (ver README do servidor)."
        )
    row = conn.execute("SELECT id FROM person WHERE lower(email) = %s", (email,)).fetchone()
    if row is None:
        raise ToolError(
            f"Sua conta {email} não está cadastrada no registro. Peça a quem administra "
            "para incluí-la; nada foi gravado."
        )
    return row["id"]


def _provenance(conn, object_type: str, object_id: uuid.UUID, principal: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id, "
        "model, source_ref) VALUES (%s, %s, 'agent', %s, %s, %s)",
        (object_type, object_id, principal, UNKNOWN_MODEL,
         f"mcp; client={identity.current_client() or 'desconhecido'}"),
    )


def _tags(conn, link_table: str, fk: str, object_id: uuid.UUID, tags: list[str] | None) -> None:
    for name in {fold(tag).strip() for tag in tags or []} - {""}:
        conn.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING", (object_id, name))


def _slug(conn, title: str) -> str:
    base = "-".join("".join(c if c.isalnum() else " " for c in fold(title)).split())[:80] or "decisao"
    taken = {r["slug"] for r in conn.execute(
        "SELECT slug FROM decision WHERE slug = %s OR slug LIKE %s", (base, base + "-%")).fetchall()}
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


PROMPTS = {
    "alternatives": "Sem alternativas descartadas: quais opções foram consideradas e por que perderam?",
    "evidence": "Sem evidência vinculada: o que sustentou ou contradisse essa escolha?",
    "context": "Sem contexto: qual era o problema no momento da decisão?",
}


def propose_decision(conn, *, title: str, description: str, decider_email: str,
                     context: str | None, decided_on: str | None, door: str,
                     project: str | None, alternatives: list[dict[str, str]] | None,
                     tags: list[str] | None, evidence: list[dict[str, Any]] | None,
                     idempotency_key: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    missing = [f for f, v in (("alternatives", alternatives), ("evidence", evidence),
                              ("context", context)) if not v]

    if idempotency_key:
        row = conn.execute(
            "SELECT d.id::text AS id, d.slug FROM idempotency_key k JOIN decision d "
            "ON d.id = k.object_id WHERE k.principal_person_id = %s AND k.key = %s",
            (principal, idempotency_key)).fetchone()
        if row:
            return {"decision_id": row["id"], "slug": row["slug"], "missing": missing}, []

    if door not in ("one_way", "two_way"):
        raise ToolError("door deve ser one_way ou two_way.")
    decider = conn.execute("SELECT id FROM person WHERE lower(email) = lower(%s)",
                           (decider_email.strip(),)).fetchone()
    if decider is None:
        raise ToolError("Pessoa não encontrada. Confirme o e-mail com o usuário; "
                        "o registro não foi criado.")
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
    for alt in alternatives or []:
        if not alt.get("description") or not alt.get("rejection_reason"):
            raise ToolError("Cada alternativa precisa de description e rejection_reason.")

    slug = _slug(conn, title)
    decision_id = conn.execute(
        "INSERT INTO decision (slug, title, context, description, door, decided_on, "
        "decider_person_id, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (slug, title, context, description, door,
         date.fromisoformat(decided_on) if decided_on else today(), decider["id"], project_id),
    ).fetchone()["id"]
    for alt in alternatives or []:
        conn.execute("INSERT INTO alternative (decision_id, description, rejection_reason) "
                     "VALUES (%s, %s, %s)", (decision_id, alt["description"], alt["rejection_reason"]))
    _tags(conn, "decision_tag", "decision_id", decision_id, tags)
    for link in evidence or []:
        _link(conn, decision_id, _uuid(link.get("evidence_id"), "evidence_id"),
              link.get("role"), link.get("weight"), link.get("note"))
    _provenance(conn, "decision", decision_id, principal)
    if idempotency_key:
        conn.execute("INSERT INTO idempotency_key (principal_person_id, key, object_type, "
                     "object_id) VALUES (%s, %s, 'decision', %s)",
                     (principal, idempotency_key, decision_id))
    return ({"decision_id": str(decision_id), "slug": slug, "missing": missing},
            [PROMPTS[f] for f in missing])


def _link(conn, decision_id: uuid.UUID, evidence_id: uuid.UUID, role: str | None,
          weight: float | None, note: str | None) -> tuple[str, bool]:
    """Vincula; se já havia vínculo, devolve o papel original sem mudar nada."""
    if role not in ROLES:
        raise ToolError("role deve ser supports, contradicts ou discarded.")
    if weight is not None and not 0 <= weight <= 1:
        raise ToolError("weight vai de 0 a 1.")
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


def attach_evidence(conn, *, decision_id: str, role: str, evidence_id: str | None,
                    evidence: dict[str, Any] | None, weight: float | None,
                    note: str | None) -> dict[str, Any]:
    principal = require_principal(conn)
    did = _uuid(decision_id, "decision_id")
    if conn.execute("SELECT 1 FROM decision WHERE id = %s", (did,)).fetchone() is None:
        raise ToolError(f"Decisão não encontrada: {decision_id}. Use search_evidence para "
                        "localizá-la.")
    if not evidence_id and not evidence:
        raise ToolError("Informe evidence_id de uma evidência existente ou o objeto evidence.")

    reused = False
    if evidence_id:
        eid = _uuid(evidence_id, "evidence_id")
        reused = True
    else:
        system, external = evidence.get("source_system"), evidence.get("external_id")
        if bool(system) != bool(external):
            raise ToolError("source_system e external_id vão juntos: informe os dois ou nenhum.")
        row = conn.execute("SELECT id FROM evidence WHERE source_system = %s AND external_id = %s",
                           (system, external)).fetchone() if system else None
        if row:
            eid, reused = row["id"], True
        else:
            kind, strength = evidence.get("kind"), evidence.get("strength")
            if kind not in EVIDENCE_KINDS:
                raise ToolError(f"kind deve ser um de: {', '.join(EVIDENCE_KINDS)}.")
            if strength not in STRENGTHS:
                raise ToolError(f"strength deve ser um de: {', '.join(STRENGTHS)}.")
            if not evidence.get("title"):
                raise ToolError("A evidência nova precisa de title.")
            if kind == "experiment" and system:
                raise ToolError(
                    "Experimento de plataforma entra pela ingestão do contrato (spec/), não por "
                    "agente. Registre sem source_system/external_id, com a url, ou peça a "
                    "importação a quem administra.")
            eid = conn.execute(
                "INSERT INTO evidence (kind, title, summary, url, source_system, external_id, "
                "strength) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (kind, evidence["title"], evidence.get("summary"), evidence.get("url"),
                 system, external, strength)).fetchone()["id"]
            _provenance(conn, "evidence", eid, principal)

    final_role, already = _link(conn, did, eid, role, weight, note)
    state = conn.execute(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM provenance WHERE object_type = 'evidence' "
        "AND object_id = %s AND attested_at IS NOT NULL) THEN 'attested' ELSE 'proposed' END "
        "AS s", (eid,)).fetchone()["s"]
    return {"decision_id": str(did), "evidence_id": str(eid), "role": final_role,
            "reused_existing": reused, "already_linked": already, "state": state}


def record_learning(conn, *, summary: str, decision_ids: list[str] | None,
                    evidence_ids: list[str] | None, tags: list[str] | None) -> dict[str, Any]:
    principal = require_principal(conn)
    if not decision_ids and not evidence_ids:
        raise ToolError("Uma lição precisa de origem: informe decision_ids ou evidence_ids.")
    dids = [_uuid(d, "decision_id") for d in decision_ids or []]
    eids = [_uuid(e, "evidence_id") for e in evidence_ids or []]
    for d in dids:
        if conn.execute("SELECT 1 FROM decision WHERE id = %s", (d,)).fetchone() is None:
            raise ToolError(f"Decisão não encontrada: {d}. Use search_evidence para localizá-la.")
    for e in eids:
        _evidence_exists(conn, e)
    lid = conn.execute("INSERT INTO learning (summary) VALUES (%s) RETURNING id",
                       (summary,)).fetchone()["id"]
    for d in dids:
        conn.execute("INSERT INTO decision_learning VALUES (%s, %s)", (d, lid))
    for e in eids:
        conn.execute("INSERT INTO evidence_learning VALUES (%s, %s)", (e, lid))
    _tags(conn, "learning_tag", "learning_id", lid, tags)
    _provenance(conn, "learning", lid, principal)
    return {"learning_id": str(lid), "linked_decisions": [str(d) for d in dids],
            "linked_evidence": [str(e) for e in eids]}
```

**Passo 4: registrar em `server.py`.** No ponto "As escritas entram aqui", e acrescente
`writes` ao import `from . import ...`:

```python
    WRITE = dict(read_only_hint=False, destructive_hint=False, open_world_hint=False)

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
            return data, pending.build(conn, context_tags=set(tags or []),
                                       on_this_record=on_this_record)

        data, block = with_db(pool, work)
        body = models.ProposeResponse(data=models.ProposeData(**data), pending=block)
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
        weight: float | None = None,
        note: str | None = None,
    ) -> Annotated[t.CallToolResult, models.AttachResponse]:
        def work(conn):
            data = writes.attach_evidence(conn, decision_id=decision_id, role=role,
                                          evidence_id=evidence_id, evidence=evidence,
                                          weight=weight, note=note)
            tags = {r["name"] for r in conn.execute(
                "SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id "
                "WHERE x.decision_id = %s", (data["decision_id"],)).fetchall()}
            nudge = ["Só há evidência favorável registrada até agora: houve algo que "
                     "contradisse a escolha?"] if data["role"] == "supports" else []
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
            return data, pending.build(conn, context_tags=set(tags or []), on_this_record=nudge)

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
git add server/decision_memory/writes.py server/decision_memory/server.py server/tests_real/test_app_writes.py
git commit -m "feat(server): ferramentas de escrita, tudo nasce proposto"
```

---

### Tarefa 11: transporte HTTP, e o caminho inteiro por HTTP

**Arquivos:**
- Criar: `server/decision_memory/app.py`, `server/decision_memory/__main__.py`
- Criar: `server/tests_real/test_app_http.py`

**Passo 1: teste.** Este é o único teste que passa pela middleware de verdade: identidade
vinda do header, recusa de expectativa, e o ContextVar chegando à ferramenta.

```python
from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient

from conftest import ANA
from decision_memory.app import build_http_app
from decision_memory.guard import EXPECTATION_REFUSAL
from decision_memory.server import build_server

AUD = "https://decision-memory-teste.a.run.app"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def bearer(email: str) -> str:
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"Bearer {enc({'alg': 'RS256'})}.{enc({'email': email, 'aud': AUD})}."


@pytest.fixture(scope="module")
def client(pool):
    app = build_http_app(build_server(pool, (AUD,)), on_cloud_run=False)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c


def rpc(client, method, params, auth=None):
    headers = dict(HEADERS, **({"authorization": auth} if auth else {}))
    r = client.post("/mcp", headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(client, name, args, auth=None):
    return rpc(client, "tools/call", {"name": name, "arguments": args}, auth)


def test_initialize(client):
    res = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "teste", "version": "0"}})
    assert res["serverInfo"]["name"] == "decision-memory"


def test_leitura_sem_identidade_funciona(client):
    res = call(client, "search_evidence", {"query": "checkout"})
    assert res["isError"] is False
    assert res["structuredContent"]["data"]["items"]


def test_escrita_com_token_grava_em_nome_da_pessoa(client):
    res = call(client, "propose_decision",
               {"title": "Proposta via HTTP", "description": "x", "decider_email": ANA},
               auth=bearer(ANA))
    assert res["isError"] is False, res
    assert res["structuredContent"]["data"]["state"] == "proposed"


def test_escrita_sem_token_e_recusada(client):
    res = call(client, "propose_decision",
               {"title": "Proposta anônima", "description": "x", "decider_email": ANA})
    assert res["isError"] is True
    assert "nada foi gravado" in res["content"][0]["text"]


def test_agente_que_tenta_passar_confianca_e_recusado(client):
    res = call(client, "record_learning",
               {"summary": "algo", "decision_ids": ["x"], "confiança": 0.9}, auth=bearer(ANA))
    assert res["isError"] is True
    assert EXPECTATION_REFUSAL in res["content"][0]["text"]
```

Se a resposta vier em SSE em vez de JSON, confira que `build_http_app` passa
`json_response=True`.

**Passo 2: rodar e ver falhar** (import de `decision_memory.app`).

**Passo 3: `server/decision_memory/app.py`.**

```python
"""App ASGI: Streamable HTTP sem estado em /mcp, como pede MCP_TOOLS.md."""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from . import config, db
from .server import build_server


def build_http_app(server: MCPServer, *, on_cloud_run: bool) -> Starlette:
    # A proteção contra DNS rebinding recusa Host fora de localhost. No Cloud Run o
    # Host é o domínio run.app e o serviço é fechado por IAM — um navegador sem
    # token não chega aqui —, então ela é desligada só lá.
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=False)
                if on_cloud_run else None)
    return server.streamable_http_app(stateless_http=True, json_response=True,
                                      transport_security=security)


def build_app(settings: config.Settings | None = None) -> Starlette:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = settings or config.load()
    pool = db.make_pool(settings.database_url, settings.database_password)
    server = build_server(pool, settings.expected_audiences)
    return build_http_app(server, on_cloud_run=settings.on_cloud_run)
```

**Passo 4: `server/decision_memory/__main__.py`.**

```python
"""Entrada: `python -m decision_memory`. Escuta em $PORT (Cloud Run) ou 8080."""

from __future__ import annotations

import os

import uvicorn

from .app import build_app


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
```

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
Esperado: JSON com `"items"` contendo "Checkout sem etapa de confirmação".

**Passo 7: commit.**

```bash
git add server/decision_memory/app.py server/decision_memory/__main__.py server/tests_real/test_app_http.py
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

Pré-requisitos: ter `roles/run.invoker` no serviço e estar cadastrada em `person`.

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
| `DM_EXPECTED_AUDIENCE` | URL(s) do serviço, separadas por vírgula; o `aud` do token precisa bater. Obrigatória no Cloud Run: sem ela o servidor não sobe |
| `DM_TODAY` | Só para testes: data de referência das revisões vencidas |

## Deploy

Uma vez por projeto. Com o [Cloud SQL Auth Proxy](../../README.md#instância-no-cloud-sql) na
porta 5433 e a senha do `dm_admin` em `PGPASSWORD`:

```bash
PROJECT=ufpr-ppgcd
REGION=southamerica-east1
INSTANCE=$PROJECT:$REGION:decision-memory
SA=dm-server@$PROJECT.iam.gserviceaccount.com
ADMIN="host=127.0.0.1 port=5433 dbname=decision_memory user=dm_admin"

# 1. Banco: tabela nova, papel do servidor e senha dele
psql "$ADMIN" -v ON_ERROR_STOP=1 \
  -f db/migrations/2026-09-23-idempotency-key.sql -f db/grants.sql
APP_PASSWORD=$(openssl rand -base64 32)
psql "$ADMIN" -c "ALTER ROLE dm_app PASSWORD '$APP_PASSWORD'"

# 2. Dados: fixtures + pessoas reais (CSV name,email, fora do git)
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
gcloud run deploy decision-memory --source server --project $PROJECT --region $REGION \
  --service-account $SA --no-allow-unauthenticated \
  --add-cloudsql-instances $INSTANCE --min-instances 0 --max-instances 2 \
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
container sem assinatura. O servidor confia nessa validação. Com o serviço aberto, qualquer um
forja o e-mail no token e escreve em nome de outra pessoa. Mesmo fechado, quando vêm
`X-Serverless-Authorization` e `Authorization` juntos o Cloud Run só confere o primeiro; por
isso o servidor, nesse caso, lê a identidade só dele.

## Verificação manual depois do deploy

Não há teste automático contra o GCP. Com o proxy do `gcloud run services proxy` no ar:

```bash
H='-H accept:application/json,text/event-stream -H content-type:application/json'
curl -s localhost:8080/mcp $H -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_evidence","arguments":{"query":"checkout"}}}'
```

1. A resposta traz "Checkout em página única" — leitura e conexão por socket funcionam.
2. No Claude Code, peça para registrar uma decisão de teste. Deve voltar `state: proposed`.
   Se voltar "Não consegui identificar sua conta", o token não chegou ou o `aud` não bateu:
   veja nos logs do serviço e ajuste `DM_EXPECTED_AUDIENCE`.
3. Em `get_decision` da decisão de teste, `provenance.principal` é o seu nome.

## Divergências em relação a `MCP_TOOLS.md`

- **Transporte e identidade.** IAM do Google Cloud via `gcloud run services proxy`, não OAuth
  2.1. Sem URL pública e sem conector no claude.ai.
- **`attest_url` nulo.** Não há superfície de atestação; o que o agente escreve fica
  `proposed`. O loop decisão → expectativa → revisão não fecha neste corte.
- **`provenance.model` é sempre `unknown`.** O MCP não informa o modelo e o `clientInfo` não
  chega em modo sem estado. O `User-Agent` do cliente vai para `source_ref`.
- **Estado da evidência** derivado da procedência ([ADR 0005](../../docs/adr/0005-estado-da-evidencia.md)).
- **Experimento de plataforma novo não entra por agente.** `attach_evidence` com
  `kind = experiment` e `source_system` inédito é recusado: esse caminho é o da ingestão pelo
  contrato. Experimento já importado é reaproveitado normalmente.
- **`list_pending_reviews`**: `unattested` não é filtrado por `owner_email`.
- **Busca**: dicionário `simple`, sem tratamento de acento nem radical — "conversao" não
  encontra "conversão".
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
- **`attach_evidence`:** experimento de plataforma inédito é recusado; entra pela ingestão.
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
