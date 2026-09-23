"""Banco de teste de verdade: schema, grants e fixtures carregados do zero.

Exige DM_TEST_ADMIN_URL apontando para um banco vazio cujo nome termina em
_test, conectado como superusuário. A sessão recria o schema public, então a
suíte se recusa a rodar em qualquer outro banco.

A instância Postgres inteira tem de ser dedicada aos testes: papéis valem para
o cluster todo, e a suíte aplica grants.sql e troca a senha do papel dm_app.
Por isso ela também se recusa a rodar fora da máquina local (localhost,
127.0.0.1, ::1 ou socket Unix).
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
