"""Testes do stub: as regras do projeto viram asserção.

O que está aqui não é cobertura de linha, é guarda-corpo: seis ferramentas,
nenhuma expectativa vinda de agente, nenhum ranqueamento por efeito e um bloco
`pending` que nunca some.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from decision_memory_stub.corpus import Corpus, load_corpus
from conftest import client_session
from decision_memory_stub.server import EXPECTATION_REFUSAL, build_server

ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_TOOLS = {
    "search_evidence",
    "get_decision",
    "propose_decision",
    "attach_evidence",
    "record_learning",
    "list_pending_reviews",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def server():
    return build_server()


@pytest.fixture(scope="module")
def tools(server):
    return {tool.name: tool for tool in run(server.list_tools())}


# --------------------------------------------------------------- superfície


def test_sao_exatamente_seis_ferramentas(tools):
    assert set(tools) == EXPECTED_TOOLS
    assert len(tools) == 6, "não crie a sétima ferramenta sem ADR"


def test_toda_ferramenta_declara_output_schema(tools):
    sem_schema = [name for name, tool in tools.items() if not tool.output_schema]
    assert sem_schema == []


def test_anotacoes_batem_com_mcp_tools(tools):
    somente_leitura = {"search_evidence", "get_decision", "list_pending_reviews"}
    for name, tool in tools.items():
        annotations = tool.annotations
        assert annotations is not None, name
        assert annotations.open_world_hint is False, name
        assert annotations.read_only_hint is (name in somente_leitura), name
        if name not in somente_leitura:
            assert annotations.destructive_hint is False, name


def test_nenhum_schema_de_entrada_pede_confianca_ou_expectativa(tools):
    proibidos = ("confidence", "confianca", "expectation", "expectativa", "certeza")
    for name, tool in tools.items():
        campos = " ".join(tool.input_schema.get("properties", {})).lower()
        for termo in proibidos:
            assert termo not in campos, f"{name} expõe '{termo}' no schema de entrada"


def test_agente_que_tenta_passar_confianca_e_recusado(server):
    async def cenario():
        async with client_session(server) as session:
            return await session.call_tool(
                "propose_decision",
                {
                    "title": "Remover etapa",
                    "description": "x",
                    "decider_email": "ana@exemplo.com.br",
                    "confidence": 0.8,
                },
            )

    res = run(cenario())
    assert res.is_error is True
    assert EXPECTATION_REFUSAL in res.content[0].text


def test_recusa_vale_para_variantes_acentuadas_e_em_portugues(server):
    campos = ("confiança", "expectativa", "resultado_esperado", "magnitude_esperada")

    async def cenario():
        async with client_session(server) as session:
            return [
                await session.call_tool(
                    "record_learning",
                    {"summary": "algo", "decision_ids": ["dec-x"], campo: "alto"},
                )
                for campo in campos
            ]

    for campo, res in zip(campos, run(cenario())):
        assert res.is_error is True, campo
        assert EXPECTATION_REFUSAL in res.content[0].text, campo


def test_recusa_nao_atrapalha_chamada_limpa(server):
    """A guarda não pode transformar uso legítimo em erro."""

    async def cenario():
        async with client_session(server) as session:
            return await session.call_tool("search_evidence", {"query": "checkout"})

    res = run(cenario())
    assert res.is_error is False
    assert res.structured_content["data"]["items"]


# ------------------------------------------------------------------- busca


def test_busca_traz_a_evidencia_que_contradiz(server):
    res = run(server.call_tool("search_evidence", {"query": "checkout confirmação"}))
    ids = [item["id"] for item in res.structured_content["data"]["items"]]
    assert "ev-checkout-confirm" in ids
    assert "ev-checkout-pagina-unica" in ids, (
        "a evidência que contradiz a tese tem de aparecer na mesma busca"
    )


def test_busca_sem_resultado_manda_dizer_isso_em_voz_alta(server):
    res = run(server.call_tool("search_evidence", {"query": "programa de fidelidade por pontos"}))
    data = res.structured_content["data"]
    assert data["items"] == []
    assert data["note"] and "explicitamente" in data["note"]


def test_ordem_da_busca_nao_depende_do_efeito():
    """ADR 0003: ranquear por efeito compara grandezas de origens diferentes."""
    original = load_corpus()
    inflado = Corpus(
        people=original.people,
        projects=original.projects,
        evidence=copy.deepcopy(original.evidence),
        decisions=original.decisions,
        learnings=original.learnings,
        reviews=original.reviews,
    )
    for item in inflado.evidence:
        effect = item.get("record", {}).get("effect")
        if effect:
            effect["point"] = effect["point"] * 100 + 10  # efeitos absurdamente maiores

    consulta = {"query": "checkout conversão"}
    antes = [i["id"] for i in run(build_server(original).call_tool("search_evidence", consulta)).structured_content["data"]["items"]]
    depois = [i["id"] for i in run(build_server(inflado).call_tool("search_evidence", consulta)).structured_content["data"]["items"]]
    assert antes == depois, "mudar o efeito mudou a ordem: isso é ranqueamento por efeito"


def test_filtro_por_origem_e_por_tipo(server):
    res = run(
        server.call_tool(
            "search_evidence",
            {"query": "checkout", "kinds": ["experiment"], "source_system": "growthbook"},
        )
    )
    itens = res.structured_content["data"]["items"]
    assert itens
    assert all(i["source"]["system"] == "growthbook" for i in itens)


# ---------------------------------------------------------------- decisão


def test_decisao_atestada_traz_expectativa(server):
    res = run(server.call_tool("get_decision", {"slug": "remover-confirmacao-checkout"}))
    data = res.structured_content["data"]
    assert data["state"] == "attested"
    assert data["expectation"] is not None
    assert data["expectation"]["confidence"] == 0.7
    papeis = {e["role"] for e in data["evidence"]}
    assert "contradicts" in papeis


def test_decisao_proposta_nao_traz_expectativa(server):
    res = run(server.call_tool("get_decision", {"id": "dec-push-diario"}))
    data = res.structured_content["data"]
    assert data["state"] == "proposed"
    assert data["expectation"] is None, "expectativa só existe depois da atestação humana"


def test_decisao_inexistente_diz_o_que_fazer(server):
    with pytest.raises(Exception) as excinfo:
        run(server.call_tool("get_decision", {"id": "dec-nao-existe"}))
    assert "search_evidence" in str(excinfo.value)


# ---------------------------------------------------------------- escrita


def test_proposta_nasce_proposed_e_lista_o_que_falta(server):
    res = run(
        server.call_tool(
            "propose_decision",
            {
                "title": "Adotar entrega no mesmo dia na capital",
                "description": "Entregar no mesmo dia para pedidos até as 14h.",
                "decider_email": "ana@exemplo.com.br",
            },
        )
    )
    data = res.structured_content["data"]
    assert data["state"] == "proposed"
    assert set(data["missing"]) == {"alternatives", "evidence", "context"}
    assert data["attest_url"].endswith(data["decision_id"])
    assert res.structured_content["pending"]["on_this_record"]


def test_proposta_com_email_desconhecido_nao_cria_registro(server):
    with pytest.raises(Exception) as excinfo:
        run(
            server.call_tool(
                "propose_decision",
                {"title": "x", "description": "y", "decider_email": "ninguem@exemplo.com.br"},
            )
        )
    assert "não foi criado" in str(excinfo.value)


def test_idempotency_key_devolve_o_mesmo_registro(server):
    payload = {
        "title": "Rever política de cupons",
        "description": "Limitar cupom a um por pedido.",
        "decider_email": "bruno@exemplo.com.br",
        "idempotency_key": "cupons-2026-09",
    }
    primeiro = run(server.call_tool("propose_decision", payload)).structured_content["data"]
    segundo = run(server.call_tool("propose_decision", payload)).structured_content["data"]
    assert primeiro["decision_id"] == segundo["decision_id"]


def test_evidencia_ja_existente_e_reaproveitada(server):
    res = run(
        server.call_tool(
            "attach_evidence",
            {
                "decision_id": "dec-remover-confirmacao",
                "role": "supports",
                "evidence": {
                    "kind": "experiment",
                    "title": "Checkout sem etapa de confirmação (reimportado)",
                    "source_system": "growthbook",
                    "external_id": "exp_checkout_no_confirm",
                    "strength": "causal",
                },
            },
        )
    )
    data = res.structured_content["data"]
    assert data["reused_existing"] is True
    assert data["evidence_id"] == "ev-checkout-confirm"


def test_evidencia_favoravel_puxa_pergunta_pela_contraria(server):
    res = run(
        server.call_tool(
            "attach_evidence",
            {"decision_id": "dec-busca-sinonimos", "role": "supports", "evidence_id": "ev-busca-sinonimos"},
        )
    )
    lembretes = " ".join(res.structured_content["pending"]["on_this_record"])
    assert "contradisse" in lembretes


def test_licao_precisa_de_origem(server):
    with pytest.raises(Exception) as excinfo:
        run(server.call_tool("record_learning", {"summary": "algo genérico e solto"}))
    assert "origem" in str(excinfo.value)


# ---------------------------------------------------------------- pendências


def test_duas_revisoes_vencidas(server):
    res = run(server.call_tool("list_pending_reviews", {"overdue_only": True}))
    data = res.structured_content["data"]
    assert data["overdue_count"] == 2
    assert {r["decision_id"] for r in data["reviews"]} == {
        "dec-onboarding-tres-etapas",
        "dec-remover-confirmacao",
    }


def test_bloco_pending_nunca_esta_ausente(server, tools):
    chamadas = {
        "search_evidence": {"query": "checkout"},
        "get_decision": {"id": "dec-busca-sinonimos"},
        "list_pending_reviews": {},
        "propose_decision": {
            "title": "t",
            "description": "d",
            "decider_email": "ana@exemplo.com.br",
        },
        "attach_evidence": {
            "decision_id": "dec-busca-sinonimos",
            "role": "contradicts",
            "evidence_id": "ev-busca-zero-resultados",
        },
        "record_learning": {"summary": "uma afirmação reutilizável qualquer", "decision_ids": ["d"]},
    }
    assert set(chamadas) == EXPECTED_TOOLS
    for name, args in chamadas.items():
        res = run(server.call_tool(name, args))
        assert "pending" in res.structured_content, name
        assert res.content and res.content[0].text, f"{name} não devolveu texto curto"


def test_pending_prioriza_revisao_da_mesma_tag(server):
    res = run(server.call_tool("search_evidence", {"query": "onboarding ativação"}))
    due = res.structured_content["pending"]["reviews_due"]
    assert due[0]["decision_id"] == "dec-onboarding-tres-etapas"


# ---------------------------------------------------------------- fixtures


def test_fixtures_tem_integridade_referencial():
    corpus = load_corpus()
    ids_evidencia = {e["id"] for e in corpus.evidence}
    ids_licao = {ln["id"] for ln in corpus.learnings}
    ids_decisao = {d["id"] for d in corpus.decisions}
    ids_pessoa = {p["id"] for p in corpus.people}
    ids_projeto = {p["id"] for p in corpus.projects}

    for decision in corpus.decisions:
        assert decision["decider"] in ids_pessoa, decision["id"]
        assert decision["project"] in ids_projeto, decision["id"]
        for link in decision.get("evidence", []):
            assert link["evidence_id"] in ids_evidencia, decision["id"]
        for learning_id in decision.get("learnings", []):
            assert learning_id in ids_licao, decision["id"]
        if decision["state"] == "proposed":
            assert "expectation" not in decision, "registro proposto não tem expectativa"

    for learning in corpus.learnings:
        for decision_id in learning.get("from_decisions", []):
            assert decision_id in ids_decisao, learning["id"]
        for evidence_id in learning.get("from_evidence", []):
            assert evidence_id in ids_evidencia, learning["id"]

    for review in corpus.reviews:
        assert review["decision_id"] in ids_decisao, review["id"]


def test_registros_de_experimento_valem_contra_o_contrato():
    jsonschema = pytest.importorskip("jsonschema")
    validador = jsonschema.Draft202012Validator(
        json.loads((ROOT / "spec" / "experiment-record-v0.schema.json").read_text(encoding="utf-8")),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    registros = [e for e in load_corpus().evidence if "record" in e]
    assert len(registros) >= 5
    for item in registros:
        erros = list(validador.iter_errors(item["record"]))
        assert not erros, f"{item['id']}: {[e.message for e in erros]}"
