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
