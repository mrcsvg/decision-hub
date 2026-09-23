from __future__ import annotations

import pytest
from conftest import run

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
