from __future__ import annotations

import psycopg
import pytest


def test_servidor_conecta_como_dm_app(pool):
    with pool.connection() as conn:
        assert conn.execute("SELECT current_user AS u").fetchone()["u"] == "dm_app"


def test_dm_app_nao_escreve_expectativa(pool):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pool.connection() as conn:
            conn.execute("DELETE FROM expectation")
