"""get_topic_timeline e find_related sobre o banco (ADR 0006).

Outros arquivos da suíte gravam decisões no mesmo banco de sessão; por isso a
linha do tempo é conferida pela ordem relativa dos eventos das fixtures, e não
pela lista exata.
"""

from __future__ import annotations

import json

import pytest
from conftest import run
from psycopg.types.json import Jsonb

from decision_memory.seed import fid
from decision_memory.server import build_server

PAGINA_UNICA = str(fid("dec-checkout-pagina-unica"))
CONFIRMACAO = str(fid("dec-remover-confirmacao"))


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args):
    return run(server.call_tool(name, args))


def _timeline(server, args):
    return call(server, "get_topic_timeline", args).structured_content["data"]


def _related(server, args):
    return call(server, "find_related", args).structured_content["data"]


# ---------------------------------------------------------- linha do tempo


def test_linha_do_tempo_conta_a_trajetoria_em_ordem(server):
    eventos = _timeline(server, {"query": "checkout"})["events"]
    ids = [e["id"] for e in eventos]
    esperados = [PAGINA_UNICA, str(fid("rv-pagina-unica")), str(fid("ln-paginas")),
                 CONFIRMACAO, str(fid("ln-confirmacao"))]
    assert [i for i in ids if i in esperados] == esperados
    assert [e["on"] for e in eventos] == sorted(e["on"] for e in eventos)
    revisao = eventos[ids.index(str(fid("rv-pagina-unica")))]
    assert (revisao["type"], revisao["verdict"], revisao["decision_id"]) == (
        "review", "worse", PAGINA_UNICA)


def test_linha_do_tempo_so_traz_revisao_realizada(server):
    ids = {e["id"] for e in _timeline(server, {"query": "checkout"})["events"]}
    assert str(fid("rv-checkout")) not in ids, "revisão aberta é assunto do pending"


def test_linha_do_tempo_nunca_traz_expectativa(server):
    """ADR 0006: confiança ao lado de desfecho vira calibração informal."""
    data = _timeline(server, {"query": "checkout"})
    texto = json.dumps(data).lower()
    assert CONFIRMACAO in texto, "a decisão atestada, com expectativa, tem de estar na amostra"
    for termo in ("confidence", "expectation", "expected_", "+2 pontos"):
        assert termo not in texto, termo


def test_linha_do_tempo_nao_tem_evidencia_como_evento(server):
    eventos = _timeline(server, {"query": "checkout conversão"})["events"]
    assert eventos
    assert {e["type"] for e in eventos} <= {"decision", "review", "learning"}


def test_linha_do_tempo_sem_resultado_manda_dizer_isso(server):
    data = _timeline(server, {"query": "programa de fidelidade por pontos"})
    assert data["events"] == []
    assert "explicitamente" in data["note"]


def test_filtro_por_tag_na_linha_do_tempo(server):
    eventos = _timeline(server, {"query": "etapas", "tags": ["onboarding"]})["events"]
    assert eventos
    assert all("onboarding" in e["tags"] for e in eventos)


def test_limite_corta_por_relevancia_e_devolve_o_total(server):
    data = _timeline(server, {"query": "checkout", "limit": 1})
    assert data["total"] >= 4
    assert len([e for e in data["events"] if e["type"] != "review"]) == 1


def test_resumo_da_linha_do_tempo(server):
    texto = call(server, "get_topic_timeline", {"query": "checkout"}).content[0].text
    assert "eventos sobre 'checkout', de 2025-08-25" in texto


# ------------------------------------------------------------- relacionadas


def test_relacionada_por_evidencia_e_licao_vem_antes_da_por_tag(server):
    data = _related(server, {"slug": "remover-confirmacao-checkout"})
    assert data["decision_id"] == CONFIRMACAO
    primeira = data["related"][0]
    assert primeira["decision_id"] == PAGINA_UNICA
    assert [(e["evidence_id"], e["role_here"], e["role_there"])
            for e in primeira["shared_evidence"]] == [
        (str(fid("ev-checkout-pagina-unica")), "contradicts", "contradicts")]
    assert [ln["learning_id"] for ln in primeira["shared_learnings"]] == [str(fid("ln-paginas"))]
    assert primeira["shared_tags"] == ["checkout", "conversao"]
    so_por_tag = [r["decision_id"] for r in data["related"]
                  if not r["shared_evidence"] and not r["shared_learnings"]]
    assert so_por_tag[:2] == [str(fid("dec-frete-gratis-99")), str(fid("dec-busca-sinonimos"))], \
        "no empate de vínculos, a decisão mais recente primeiro"


def test_relacionadas_nao_incluem_a_propria_decisao(server):
    data = _related(server, {"id": CONFIRMACAO})
    assert CONFIRMACAO not in {r["decision_id"] for r in data["related"]}


def test_relacionada_inexistente_diz_o_que_fazer(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "find_related", {"slug": "nao-existe"})
    assert "search_evidence" in str(excinfo.value)


def test_papel_oposto_aparece_no_resumo(server, admin_conn):
    """A mesma evidência sustentando uma decisão e contradizendo a outra."""
    ev, dec = fid("ev-checkout-pagina-unica"), fid("dec-checkout-pagina-unica")
    admin_conn.execute("UPDATE decision_evidence SET role = 'supports' "
                       "WHERE decision_id = %s AND evidence_id = %s", (dec, ev))
    try:
        res = call(server, "find_related", {"id": CONFIRMACAO})
    finally:
        admin_conn.execute("UPDATE decision_evidence SET role = 'contradicts' "
                           "WHERE decision_id = %s AND evidence_id = %s", (dec, ev))
    primeira = res.structured_content["data"]["related"][0]["shared_evidence"][0]
    assert (primeira["role_here"], primeira["role_there"]) == ("contradicts", "supports")
    assert "papel oposto" in res.content[0].text


def test_ordem_das_relacionadas_nao_depende_do_efeito(server, admin_conn):
    """ADR 0003: inverter o sinal dos efeitos não muda nada."""
    antes = _related(server, {"id": CONFIRMACAO})["related"]
    pontos = admin_conn.execute(
        "SELECT id, normalized->'effect'->'point' FROM evidence "
        "WHERE normalized->'effect'->>'point' IS NOT NULL").fetchall()
    assert pontos
    try:
        for eid, ponto in pontos:
            admin_conn.execute(
                "UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', %s) "
                "WHERE id = %s", (Jsonb(-float(ponto) * 100), eid))
        depois = _related(server, {"id": CONFIRMACAO})["related"]
    finally:
        for eid, ponto in pontos:
            admin_conn.execute(
                "UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', %s) "
                "WHERE id = %s", (Jsonb(ponto), eid))
    assert antes == depois


def test_bloco_pending_nunca_esta_ausente(server):
    for name, args in [("get_topic_timeline", {"query": "zzz inexistente"}),
                       ("find_related", {"slug": "manter-devolucao-30-dias"})]:
        assert "pending" in call(server, name, args).structured_content, name
