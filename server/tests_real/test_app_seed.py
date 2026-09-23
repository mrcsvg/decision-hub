from __future__ import annotations

from decision_memory import seed

FIXTURES = seed.DEFAULT_FIXTURES


def _counts(conn):
    tables = ("person", "project", "evidence", "decision", "alternative", "learning",
              "review", "expectation", "provenance", "decision_evidence")
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
