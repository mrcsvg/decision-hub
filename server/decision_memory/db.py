"""Pool de conexões. Cada chamada de ferramenta usa uma conexão e uma transação:
sai com commit se deu certo, com rollback se levantou exceção."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(url: str, password: str | None = None) -> ConnectionPool:
    kwargs: dict = {"row_factory": dict_row}
    if password:
        kwargs["password"] = password
    return ConnectionPool(url, kwargs=kwargs, min_size=1, max_size=4, open=True)
