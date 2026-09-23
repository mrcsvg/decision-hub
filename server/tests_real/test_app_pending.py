"""O bloco `pending` calculado a partir do banco, com DM_TODAY=2026-09-22.

Outros arquivos da suíte gravam no mesmo banco de sessão; por isso o que pode
mudar com a ordem dos testes (contagem de não atestados, revisões vencidas) é
conferido contra uma consulta independente, e não contra um número fixo.
"""

from __future__ import annotations

from datetime import date

from decision_memory import pending
from decision_memory.models import Pending, ReviewDue
from decision_memory.seed import fid

TODAY = date(2026, 9, 22)
ONBOARDING = str(fid("dec-onboarding-tres-etapas"))
CHECKOUT = str(fid("dec-remover-confirmacao"))


def _overdue_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT decision_id::text AS id FROM review WHERE done_on IS NULL AND due_on <= %s",
        (TODAY,),
    ).fetchall()
    return {r["id"] for r in rows}


def test_bloco_traz_as_revisoes_vencidas_em_aberto(pool):
    with pool.connection() as conn:
        block = pending.build(conn)
        expected = _overdue_ids(conn)
    ids = [r.decision_id for r in block.reviews_due]
    assert {ONBOARDING, CHECKOUT} <= expected
    assert len(ids) == min(len(expected), pending.MAX_REVIEWS_DUE)
    assert set(ids) <= expected
    by_id = {r.decision_id: r for r in block.reviews_due}
    assert by_id[ONBOARDING].overdue_days == 7
    assert by_id[ONBOARDING].due_on == "2026-09-15"
    assert by_id[CHECKOUT].overdue_days == 4
    # Sem contexto, a mais vencida vem primeiro.
    assert ids[0] == ONBOARDING
    # Revisão feita (rv-busca) e ainda não vencidas (rv-frete, rv-devolucao) ficam de fora.
    assert str(fid("dec-busca-sinonimos")) not in ids
    assert str(fid("dec-frete-gratis-99")) not in ids
    assert str(fid("dec-manter-devolucao-30-dias")) not in ids


def test_tag_do_contexto_passa_na_frente(pool):
    with pool.connection() as conn:
        checkout = pending.build(conn, context_tags={"checkout"})
        onboarding = pending.build(conn, context_tags={"onboarding"})
    assert checkout.reviews_due[0].decision_id == CHECKOUT
    assert onboarding.reviews_due[0].decision_id == ONBOARDING


def test_contagem_de_nao_atestados_bate_com_consulta_independente(pool):
    independent = """
        SELECT (SELECT count(*) FROM decision WHERE state = 'proposed')
             + (SELECT count(*) FROM learning WHERE state = 'proposed')
             + (SELECT count(*) FROM evidence e
                 WHERE NOT EXISTS (SELECT 1 FROM provenance p
                                    WHERE p.object_type = 'evidence' AND p.object_id = e.id
                                      AND p.attested_at IS NOT NULL)) AS n
    """
    with pool.connection() as conn:
        block = pending.build(conn)
        assert block.unattested_count == conn.execute(independent).fetchone()["n"]
        # Pelo menos dec-push-diario e ln-push-proposta, que vêm propostos no corpus.
        assert block.unattested_count >= 2


def test_evidencia_criada_pelo_agente_conta_como_nao_atestada(pool):
    with pool.connection() as conn:
        before = pending.build(conn).unattested_count
        with conn.transaction(force_rollback=True):
            evidence_id = conn.execute(
                "INSERT INTO evidence (kind, title) VALUES ('analysis', 'rascunho') RETURNING id"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO provenance"
                " (object_type, object_id, author_kind, model, principal_person_id)"
                " VALUES ('evidence', %s, 'agent', 'teste', %s)",
                (evidence_id, fid("p-ana")),
            )
            assert pending.build(conn).unattested_count == before + 1
        assert pending.build(conn).unattested_count == before


def test_hint_com_revisao_vencida_pede_para_mencionar(pool):
    with pool.connection() as conn:
        block = pending.build(conn, on_this_record=["alternatives"])
    assert block.on_this_record == ["alternatives"]
    assert "revis" in block.hint
    assert "Mencione ao usuário" in block.hint


def test_hint_so_cita_tag_compartilhada_com_revisao_vencida(pool):
    with pool.connection() as conn:
        checkout = pending.build(conn, context_tags={"checkout"})
        frete = pending.build(conn, context_tags={"frete"})
    assert "relacionada à tag checkout" in checkout.hint
    # frete tem revisão em aberto, mas não vencida: citar a tag enganaria o agente.
    assert "tag" not in frete.hint
    assert "Mencione ao usuário" in frete.hint


def test_hint_singular_e_plural():
    one = [ReviewDue(decision_id="a", title="A", due_on="2026-09-01", overdue_days=21)]
    assert "1 revisão vencida." in pending._hint(one, 0, set())
    assert "2 revisões vencidas" in pending._hint(one * 2, 0, set())


def test_sem_nada_pendente_o_bloco_vem_vazio_e_nao_ausente():
    assert pending._hint([], 0, set()) is None
    assert "3 registros aguardam atestação" in pending._hint([], 3, set())
    empty = Pending()
    assert empty.model_dump() == {
        "on_this_record": [], "reviews_due": [], "unattested_count": 0, "hint": None,
    }
