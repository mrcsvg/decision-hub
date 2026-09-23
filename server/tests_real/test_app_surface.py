from __future__ import annotations

import re

import pytest
from conftest import ROOT, run

from decision_memory.server import build_server

EXPECTED_TOOLS = {"search_evidence", "get_decision", "propose_decision",
                  "attach_evidence", "record_learning", "list_pending_reviews"}


@pytest.fixture(scope="module")
def tools(pool):
    return {tool.name: tool for tool in run(build_server(pool).list_tools())}


def descricoes_do_mcp_tools() -> dict[str, str]:
    texto = (ROOT / "MCP_TOOLS.md").read_text(encoding="utf-8")
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"^### `(\w+)`\n\n> (.+)$", texto, re.MULTILINE)}


@pytest.mark.xfail(strict=True, reason="escritas entram na tarefa 10")
def test_sao_exatamente_seis_ferramentas(tools):
    assert set(tools) == EXPECTED_TOOLS, "não crie a sétima ferramenta sem ADR"


def test_descricoes_sao_as_do_mcp_tools(tools):
    esperadas = descricoes_do_mcp_tools()
    assert set(esperadas) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description == esperadas[name], f"{name} divergiu de MCP_TOOLS.md"


def test_toda_ferramenta_declara_output_schema(tools):
    assert [n for n, t in tools.items() if not t.output_schema] == []


def test_anotacoes_batem_com_mcp_tools(tools):
    somente_leitura = {"search_evidence", "get_decision", "list_pending_reviews"}
    for name, tool in tools.items():
        assert tool.annotations.open_world_hint is False, name
        assert tool.annotations.read_only_hint is (name in somente_leitura), name


def test_nenhum_schema_de_entrada_pede_confianca_ou_expectativa(tools):
    for name, tool in tools.items():
        campos = " ".join(tool.input_schema.get("properties", {})).lower()
        for termo in ("confidence", "confianca", "expectation", "expectativa", "certeza"):
            assert termo not in campos, f"{name} expõe '{termo}'"
