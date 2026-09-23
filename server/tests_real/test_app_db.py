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
