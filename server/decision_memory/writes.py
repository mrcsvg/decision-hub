"""Escritas por agente. Tudo nasce 'proposed', com procedência author_kind = agent.

Cada função roda dentro da transação aberta por `server.with_db`: se levantar,
nada fica gravado. O papel dm_app não tem UPDATE nem DELETE, e nas tabelas
provenance, decision, learning e evidence só pode inserir nas colunas que o
agente preenche (db/grants.sql) — nada aqui altera o que já existe.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import identity
from .config import today
from .guard import fold

EVIDENCE_KINDS = ("experiment", "study", "analysis", "document", "external")
STRENGTHS = ("causal", "correlational", "anecdotal")
ROLES = ("supports", "contradicts", "discarded")

# Constante, não parâmetro: o banco não impede dm_app de gravar 'human' ou
# 'import' em provenance, então é o servidor que garante que escrita por MCP
# é sempre de agente.
AUTHOR_KIND = "agent"

# O MCP não diz qual modelo está do outro lado, e o clientInfo não chega em HTTP
# sem estado. Não inventamos: 'unknown', e o cliente (User-Agent) vai para
# source_ref. Ver README, "Divergências".
UNKNOWN_MODEL = "unknown"

DECIDER_NOT_FOUND = (
    "Pessoa não encontrada. Confirme o e-mail com o usuário; o registro não foi criado."
)

# Mesmas frases do stub: falam só do registro recém-criado.
PROMPTS = {
    "alternatives": "Sem alternativas descartadas: quais opções foram consideradas e por que perderam?",
    "evidence": "Sem evidência vinculada: o que sustentou ou contradisse essa escolha?",
    "context": "Sem contexto: qual era o problema no momento da decisão?",
}


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"{what} inválido: {value}. Use search_evidence para obter o id.") from None


def _lock(conn, *parts: object) -> None:
    """Trava de transação sobre uma chave textual; solta sozinha no commit ou rollback."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (":".join(str(p) for p in parts),))


def require_principal(conn) -> uuid.UUID:
    email = identity.current_email()
    if email is None:
        raise ToolError(
            "Não consegui identificar sua conta nesta chamada; nada foi gravado. "
            "Conecte pelo gcloud run services proxy (ver README do servidor)."
        )
    row = conn.execute("SELECT id FROM person WHERE lower(email) = %s", (email,)).fetchone()
    if row is None:
        raise ToolError(
            f"Sua conta {email} não está cadastrada no registro. Peça a quem administra "
            "para incluí-la; nada foi gravado."
        )
    return row["id"]


def _provenance(conn, object_type: str, object_id: uuid.UUID, principal: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id, "
        "model, source_ref) VALUES (%s, %s, %s, %s, %s, %s)",
        (object_type, object_id, AUTHOR_KIND, principal, UNKNOWN_MODEL,
         f"mcp; client={identity.current_client() or 'desconhecido'}"),
    )


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Minúsculas, sem acento e sem espaço nas pontas; vazias somem, repetidas também.

    Satisfaz o CHECK de tag (name = lower(name) AND name <> '').
    """
    return sorted({fold(tag).strip() for tag in tags or []} - {""})


def _tags(conn, link_table: str, fk: str, object_id: uuid.UUID, tags: list[str] | None) -> None:
    for name in normalize_tags(tags):
        conn.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING", (object_id, name))


def _slug(conn, title: str) -> str:
    base = "-".join("".join(c if c.isalnum() else " " for c in fold(title)).split())[:80] or "decisao"
    # Dois títulos iguais ao mesmo tempo escolheriam o mesmo sufixo.
    _lock(conn, "slug", base)
    taken = {r["slug"] for r in conn.execute(
        "SELECT slug FROM decision WHERE slug = %s OR slug LIKE %s",
        (base, base + "-%")).fetchall()}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _evidence_exists(conn, evidence_id: uuid.UUID) -> None:
    if conn.execute("SELECT 1 FROM evidence WHERE id = %s", (evidence_id,)).fetchone() is None:
        raise ToolError(f"Evidência não encontrada: {evidence_id}. Use search_evidence ou "
                        "envie o objeto evidence.")


def _decision_exists(conn, decision_id: uuid.UUID) -> None:
    if conn.execute("SELECT 1 FROM decision WHERE id = %s", (decision_id,)).fetchone() is None:
        raise ToolError(f"Decisão não encontrada: {decision_id}. Use search_evidence para "
                        "localizá-la.")


def _date(value: str | None) -> date:
    if not value:
        return today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"decided_on inválida: {value}. Use o formato AAAA-MM-DD.") from None


def propose_decision(conn, *, title: str, description: str, decider_email: str,
                     context: str | None, decided_on: str | None, door: str,
                     project: str | None, alternatives: list[dict[str, str]] | None,
                     tags: list[str] | None, evidence: list[dict[str, Any]] | None,
                     idempotency_key: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    missing = [f for f, v in (("alternatives", alternatives), ("evidence", evidence),
                              ("context", context)) if not v]
    prompts = [PROMPTS[f] for f in missing]

    if idempotency_key:
        # Serializa chamadas com a mesma (pessoa, chave): a segunda espera a
        # primeira terminar e então encontra a chave já gravada.
        _lock(conn, "idempotency", principal, idempotency_key)
        row = conn.execute(
            "SELECT d.id::text AS id, d.slug FROM idempotency_key k JOIN decision d "
            "ON d.id = k.object_id WHERE k.principal_person_id = %s AND k.key = %s",
            (principal, idempotency_key)).fetchone()
        if row:
            return {"decision_id": row["id"], "slug": row["slug"], "missing": missing}, prompts

    if door not in ("one_way", "two_way"):
        raise ToolError("door deve ser one_way ou two_way.")
    decider = conn.execute("SELECT id FROM person WHERE lower(email) = lower(%s)",
                           (decider_email.strip(),)).fetchone()
    if decider is None:
        raise ToolError(DECIDER_NOT_FOUND)
    project_id = None
    if project:
        try:
            as_uuid = uuid.UUID(project)
        except ValueError:
            as_uuid = None
        row = conn.execute("SELECT id FROM project WHERE id = %s OR lower(name) = lower(%s)",
                           (as_uuid, project)).fetchone()
        if row is None:
            raise ToolError(f"Projeto não encontrado: {project}. Confirme o nome com o usuário; "
                            "o registro não foi criado.")
        project_id = row["id"]
    for alt in alternatives or []:
        if not alt.get("description") or not alt.get("rejection_reason"):
            raise ToolError("Cada alternativa precisa de description e rejection_reason.")
    decided = _date(decided_on)

    slug = _slug(conn, title)
    decision_id = conn.execute(
        "INSERT INTO decision (slug, title, context, description, door, decided_on, "
        "decider_person_id, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (slug, title, context, description, door, decided, decider["id"], project_id),
    ).fetchone()["id"]
    for alt in alternatives or []:
        conn.execute("INSERT INTO alternative (decision_id, description, rejection_reason) "
                     "VALUES (%s, %s, %s)", (decision_id, alt["description"], alt["rejection_reason"]))
    _tags(conn, "decision_tag", "decision_id", decision_id, tags)
    for link in evidence or []:
        _link(conn, decision_id, _uuid(link.get("evidence_id"), "evidence_id"),
              link.get("role"), link.get("weight"), link.get("note"))
    _provenance(conn, "decision", decision_id, principal)
    if idempotency_key:
        conn.execute("INSERT INTO idempotency_key (principal_person_id, key, object_type, "
                     "object_id) VALUES (%s, %s, 'decision', %s)",
                     (principal, idempotency_key, decision_id))
    return {"decision_id": str(decision_id), "slug": slug, "missing": missing}, prompts


def _link(conn, decision_id: uuid.UUID, evidence_id: uuid.UUID, role: str | None,
          weight: float | None, note: str | None) -> tuple[str, bool]:
    """Vincula; se já havia vínculo, devolve o papel original sem mudar nada."""
    if role not in ROLES:
        raise ToolError("role deve ser supports, contradicts ou discarded.")
    if weight is not None and not 0 <= weight <= 1:
        raise ToolError("weight vai de 0 a 1.")
    _evidence_exists(conn, evidence_id)
    inserted = conn.execute(
        "INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING role::text AS role",
        (decision_id, evidence_id, role, weight, note)).fetchone()
    if inserted:
        return inserted["role"], False
    existing = conn.execute("SELECT role::text AS role FROM decision_evidence "
                            "WHERE decision_id = %s AND evidence_id = %s",
                            (decision_id, evidence_id)).fetchone()
    return existing["role"], True


def attach_evidence(conn, *, decision_id: str, role: str, evidence_id: str | None,
                    evidence: dict[str, Any] | None, weight: float | None,
                    note: str | None) -> dict[str, Any]:
    principal = require_principal(conn)
    did = _uuid(decision_id, "decision_id")
    _decision_exists(conn, did)
    if not evidence_id and not evidence:
        raise ToolError("Informe evidence_id de uma evidência existente ou o objeto evidence.")

    reused = False
    if evidence_id:
        eid = _uuid(evidence_id, "evidence_id")
        reused = True
    else:
        system, external = evidence.get("source_system"), evidence.get("external_id")
        if bool(system) != bool(external):
            raise ToolError("source_system e external_id vão juntos: informe os dois ou nenhum.")
        if system:
            # Duas chamadas com a mesma origem inédita não podem ambas inserir.
            _lock(conn, "evidence", system, external)
        row = conn.execute("SELECT id FROM evidence WHERE source_system = %s AND external_id = %s",
                           (system, external)).fetchone() if system else None
        if row:
            eid, reused = row["id"], True
        else:
            kind, strength = evidence.get("kind"), evidence.get("strength")
            if kind not in EVIDENCE_KINDS:
                raise ToolError(f"kind deve ser um de: {', '.join(EVIDENCE_KINDS)}.")
            if strength not in STRENGTHS:
                raise ToolError(f"strength deve ser um de: {', '.join(STRENGTHS)}.")
            if not evidence.get("title"):
                raise ToolError("A evidência nova precisa de title.")
            if kind == "experiment" and system:
                raise ToolError(
                    "Experimento de plataforma entra pela ingestão do contrato (spec/), não por "
                    "agente. Registre sem source_system/external_id, com a url, ou peça a "
                    "importação a quem administra.")
            eid = conn.execute(
                "INSERT INTO evidence (kind, title, summary, url, source_system, external_id, "
                "strength) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (kind, evidence["title"], evidence.get("summary"), evidence.get("url"),
                 system or None, external or None, strength)).fetchone()["id"]
            _provenance(conn, "evidence", eid, principal)

    final_role, already = _link(conn, did, eid, role, weight, note)
    state = conn.execute(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM provenance WHERE object_type = 'evidence' "
        "AND object_id = %s AND attested_at IS NOT NULL) THEN 'attested' ELSE 'proposed' END "
        "AS s", (eid,)).fetchone()["s"]
    return {"decision_id": str(did), "evidence_id": str(eid), "role": final_role,
            "reused_existing": reused, "already_linked": already, "state": state}


def record_learning(conn, *, summary: str, decision_ids: list[str] | None,
                    evidence_ids: list[str] | None, tags: list[str] | None) -> dict[str, Any]:
    principal = require_principal(conn)
    if not decision_ids and not evidence_ids:
        raise ToolError("Uma lição precisa de origem: informe decision_ids ou evidence_ids.")
    # dict.fromkeys: tira repetidos sem mudar a ordem, e o vínculo não bate na PK.
    dids = list(dict.fromkeys(_uuid(d, "decision_id") for d in decision_ids or []))
    eids = list(dict.fromkeys(_uuid(e, "evidence_id") for e in evidence_ids or []))
    for d in dids:
        _decision_exists(conn, d)
    for e in eids:
        _evidence_exists(conn, e)
    lid = conn.execute("INSERT INTO learning (summary, recorded_on) VALUES (%s, %s) RETURNING id",
                       (summary, today())).fetchone()["id"]
    for d in dids:
        conn.execute("INSERT INTO decision_learning (decision_id, learning_id) VALUES (%s, %s)",
                     (d, lid))
    for e in eids:
        conn.execute("INSERT INTO evidence_learning (evidence_id, learning_id) VALUES (%s, %s)",
                     (e, lid))
    _tags(conn, "learning_tag", "learning_id", lid, tags)
    _provenance(conn, "learning", lid, principal)
    return {"learning_id": str(lid), "linked_decisions": [str(d) for d in dids],
            "linked_evidence": [str(e) for e in eids]}
