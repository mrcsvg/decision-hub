"""Escritas por agente. Tudo nasce 'proposed', com procedência author_kind = agent.

Cada função roda dentro da transação aberta por `server.with_db`: se levantar,
nada fica gravado. O papel dm_app não tem UPDATE nem DELETE, e nas tabelas
provenance, decision, learning e evidence só pode inserir nas colunas que o
agente preenche (db/grants.sql) — nada aqui altera o que já existe.
"""

from __future__ import annotations

import re
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

# Padrão de tag do contrato de ingestão (spec/experiment-record-v0.schema.json);
# há teste que confere que é o mesmo. Tag de agente segue a mesma regra da
# tag importada.
TAG_PATTERN = "^[a-z0-9][a-z0-9 _./-]*$"
# Sem as âncoras, para fullmatch: o '$' aceitaria um '\n' no fim.
_TAG_RE = re.compile(TAG_PATTERN.removeprefix("^").removesuffix("$"))

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

ORIGIN_FROM_INGESTION = (
    "Identidade de origem (source_system + external_id) só entra pela ingestão do contrato "
    "(spec/), não por agente; nada foi gravado. Registre a evidência sem source_system e "
    "external_id, com a url, ou peça a importação a quem administra."
)

# Mesmas frases do stub: falam só do registro recém-criado.
PROMPTS = {
    "alternatives": "Sem alternativas descartadas: quais opções foram consideradas e por que perderam?",
    "evidence": "Sem evidência vinculada: o que sustentou ou contradisse essa escolha?",
    "context": "Sem contexto: qual era o problema no momento da decisão?",
}
ONLY_SUPPORTS = (
    "Só há evidência favorável registrada até agora: houve algo que contradisse a escolha?"
)


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"{what} inválido: {value}. Use search_evidence para obter o id.") from None


# Validação de tipo. O schema de entrada já barra boa parte, mas não o que vem
# dentro de objetos livres (evidence, itens de evidence); e um tipo errado que
# chegasse ao SQL viraria o erro interno genérico, que não diz o que corrigir.


def _opt_text(value: Any, what: str) -> str | None:
    """Texto opcional; vazio (depois do strip) vale como ausente."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{what} deve ser texto.")
    return value if value.strip() else None


def _req_text(value: Any, what: str) -> str:
    text = _opt_text(value, what)
    if text is None:
        raise ToolError(f"{what} é obrigatório e não pode ficar em branco.")
    return text


def _text_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{what} deve ser uma lista de textos.")
    return value


def _weight(value: Any) -> float | None:
    if value is None:
        return None
    # bool é int em Python; aqui não é peso.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolError("weight deve ser um número de 0 a 1.")
    if not 0 <= value <= 1:
        raise ToolError("weight vai de 0 a 1.")
    return float(value)


def _role(value: Any) -> str:
    if value not in ROLES:
        raise ToolError("role deve ser supports, contradicts ou discarded.")
    return value


def _lock(conn, *parts: object) -> None:
    """Trava de transação sobre uma chave textual; solta sozinha no commit ou rollback.

    A espera pela trava conta no statement_timeout (5 s, db.py): se a outra
    transação demorar mais que isso, esta sai com o erro interno genérico, sem
    gravar nada. Aceitável na v0, em que cada escrita leva milissegundos.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (":".join(str(p) for p in parts),))


def require_principal(conn) -> uuid.UUID:
    email = identity.current_email()
    if email is None:
        raise ToolError(
            "Não consegui identificar sua conta nesta chamada; nada foi gravado. "
            "Conecte pelo gcloud run services proxy (ver README do servidor)."
        )
    row = conn.execute("SELECT id FROM person WHERE lower(email) = lower(%s)",
                       (email,)).fetchone()
    if row is None:
        raise ToolError(
            # Sem o e-mail no texto: o SDK registra a mensagem de erro no log, e
            # quem chama sabe com que conta entrou.
            "Sua conta não está cadastrada no registro. Peça a quem administra "
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

    O que sobra tem de casar com TAG_PATTERN, o padrão do contrato — o que
    também satisfaz o CHECK de tag (name = lower(name) AND name <> ''). Fora
    dele (tab, caractere invisível, emoji, '#'), recusa dizendo qual tag.
    """
    names = {fold(tag).strip() for tag in tags or []} - {""}
    for name in sorted(names):
        if not _TAG_RE.fullmatch(name):
            raise ToolError(f"Tag fora do padrão {TAG_PATTERN}: {name!r}. Use letras "
                            "minúsculas, dígitos, espaço e _ . / -; nada foi gravado.")
    return sorted(names)


def _tags(conn, link_table: str, fk: str, object_id: uuid.UUID, tags: list[str] | None) -> None:
    for name in normalize_tags(tags):
        conn.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING", (object_id, name))


def slug_base(title: str) -> str:
    """Título dobrado, só letras e dígitos separados por hífen, até 80 caracteres."""
    words = "".join(c if c.isalnum() else " " for c in fold(title)).split()
    return "-".join(words)[:80].rstrip("-") or "decisao"


def _slug(conn, title: str) -> str:
    base = slug_base(title)
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


def _stored_decision(conn, principal: uuid.UUID, key: str) -> dict[str, Any] | None:
    """O registro já gravado com essa chave, com o que falta calculado dele mesmo."""
    row = conn.execute(
        "SELECT d.id::text AS id, d.slug, d.state::text AS state, "
        "coalesce(btrim(d.context), '') = '' AS no_context, "
        "NOT EXISTS (SELECT 1 FROM alternative a WHERE a.decision_id = d.id) AS no_alternatives, "
        "NOT EXISTS (SELECT 1 FROM decision_evidence x WHERE x.decision_id = d.id) AS no_evidence "
        "FROM idempotency_key k JOIN decision d ON d.id = k.object_id "
        "WHERE k.principal_person_id = %s AND k.key = %s", (principal, key)).fetchone()
    if row is None:
        return None
    missing = [f for f, absent in (("alternatives", row["no_alternatives"]),
                                   ("evidence", row["no_evidence"]),
                                   ("context", row["no_context"])) if absent]
    return {"decision_id": row["id"], "slug": row["slug"], "state": row["state"],
            "missing": missing, "reused": True}


def _alternatives(value: Any) -> list[tuple[str, str]]:
    bad = ToolError("Cada alternativa precisa de description e rejection_reason, ambos texto "
                    "não vazio.")
    if value is None:
        return []
    if not isinstance(value, list):
        raise bad
    out = []
    for alt in value:
        if not isinstance(alt, dict):
            raise bad
        try:
            out.append((_req_text(alt.get("description"), "description"),
                        _req_text(alt.get("rejection_reason"), "rejection_reason")))
        except ToolError:
            raise bad from None
    return out


def _evidence_links(value: Any) -> list[tuple[uuid.UUID, str, float | None, str | None]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ToolError("evidence deve ser uma lista de objetos {evidence_id, role}.")
    return [(_uuid(_req_text(link.get("evidence_id"), "evidence_id"), "evidence_id"),
             _role(link.get("role")), _weight(link.get("weight")),
             _opt_text(link.get("note"), "note")) for link in value]


def propose_decision(conn, *, title: str, description: str, decider_email: str,
                     context: str | None, decided_on: str | None, door: str,
                     project: str | None, alternatives: list[dict[str, str]] | None,
                     tags: list[str] | None, evidence: list[dict[str, Any]] | None,
                     idempotency_key: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    title = _req_text(title, "title")
    description = _req_text(description, "description")
    context = _opt_text(context, "context")
    alts = _alternatives(alternatives)
    links = _evidence_links(evidence)
    tags = _text_list(tags, "tags")
    normalize_tags(tags)  # recusa tag fora do padrão antes de qualquer escrita
    if idempotency_key is not None and _opt_text(idempotency_key, "idempotency_key") is None:
        # Chave em branco não vira "sem chave" em silêncio: o agente achou que
        # tinha proteção contra duplicata.
        raise ToolError("idempotency_key em branco: envie uma chave não vazia ou omita o "
                        "campo; nada foi gravado.")

    if idempotency_key:
        # Serializa chamadas com a mesma (pessoa, chave): a segunda espera a
        # primeira terminar e então encontra a chave já gravada. O que volta é
        # o registro gravado; o conteúdo desta chamada não é aplicado.
        _lock(conn, "idempotency", principal, idempotency_key)
        stored = _stored_decision(conn, principal, idempotency_key)
        if stored:
            return stored, [PROMPTS[f] for f in stored["missing"]]

    if door not in ("one_way", "two_way"):
        raise ToolError("door deve ser one_way ou two_way.")
    decider = conn.execute(
        "SELECT id FROM person WHERE lower(btrim(email)) = lower(%s)",
        ((_opt_text(decider_email, "decider_email") or "").strip(),)).fetchone()
    if decider is None:
        raise ToolError(DECIDER_NOT_FOUND)
    project = _opt_text(project, "project")
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
    decided = _date(_opt_text(decided_on, "decided_on"))

    slug = _slug(conn, title)
    decision_id = conn.execute(
        "INSERT INTO decision (slug, title, context, description, door, decided_on, "
        "decider_person_id, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (slug, title, context, description, door, decided, decider["id"], project_id),
    ).fetchone()["id"]
    for alt_description, rejection_reason in alts:
        conn.execute("INSERT INTO alternative (decision_id, description, rejection_reason) "
                     "VALUES (%s, %s, %s)", (decision_id, alt_description, rejection_reason))
    _tags(conn, "decision_tag", "decision_id", decision_id, tags)
    for evidence_id, role, weight, note in links:
        _link(conn, decision_id, evidence_id, role, weight, note)
    _provenance(conn, "decision", decision_id, principal)
    if idempotency_key:
        conn.execute("INSERT INTO idempotency_key (principal_person_id, key, object_type, "
                     "object_id) VALUES (%s, %s, 'decision', %s)",
                     (principal, idempotency_key, decision_id))
    missing = [f for f, v in (("alternatives", alts), ("evidence", links),
                              ("context", context)) if not v]
    return ({"decision_id": str(decision_id), "slug": slug, "state": "proposed",
             "missing": missing, "reused": False}, [PROMPTS[f] for f in missing])


def _link(conn, decision_id: uuid.UUID, evidence_id: uuid.UUID, role: str,
          weight: float | None, note: str | None) -> tuple[str, bool]:
    """Vincula; se já havia vínculo, devolve o papel original sem mudar nada."""
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


def _new_evidence(conn, evidence: Any, principal: uuid.UUID) -> tuple[uuid.UUID, bool]:
    """Resolve o objeto evidence: reaproveita pela origem ou cria sem origem.

    Agente nunca cria evidência com (source_system, external_id): essa
    identidade vem só da ingestão pelo contrato. Chaves que o agente mandar
    além das previstas (conformance_level, normalized, ...) são ignoradas.
    """
    if not isinstance(evidence, dict):
        raise ToolError("evidence deve ser um objeto {kind, title, strength, ...}.")
    title = _opt_text(evidence.get("title"), "title")
    summary = _opt_text(evidence.get("summary"), "summary")
    url = _opt_text(evidence.get("url"), "url")
    system = _opt_text(evidence.get("source_system"), "source_system")
    external = _opt_text(evidence.get("external_id"), "external_id")
    if (system is None) != (external is None):
        raise ToolError("source_system e external_id vão juntos: informe os dois ou nenhum.")
    if system is not None:
        row = conn.execute("SELECT id FROM evidence WHERE source_system = %s AND external_id = %s",
                           (system, external)).fetchone()
        if row is None:
            raise ToolError(ORIGIN_FROM_INGESTION)
        return row["id"], True

    kind, strength = evidence.get("kind"), evidence.get("strength")
    if kind not in EVIDENCE_KINDS:
        raise ToolError(f"kind deve ser um de: {', '.join(EVIDENCE_KINDS)}.")
    if strength not in STRENGTHS:
        raise ToolError(f"strength deve ser um de: {', '.join(STRENGTHS)}.")
    if title is None:
        raise ToolError("A evidência nova precisa de title, texto não vazio.")
    eid = conn.execute(
        "INSERT INTO evidence (kind, title, summary, url, strength) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (kind, title, summary, url, strength)).fetchone()["id"]
    _provenance(conn, "evidence", eid, principal)
    return eid, False


def attach_evidence(conn, *, decision_id: str, role: str, evidence_id: str | None,
                    evidence: dict[str, Any] | None, weight: float | None,
                    note: str | None) -> tuple[dict[str, Any], list[str]]:
    principal = require_principal(conn)
    did = _uuid(decision_id, "decision_id")
    _decision_exists(conn, did)
    if evidence_id and evidence:
        raise ToolError("Informe um dos dois, não ambos: evidence_id de uma evidência existente "
                        "ou o objeto evidence.")
    if not evidence_id and not evidence:
        raise ToolError("Informe evidence_id de uma evidência existente ou o objeto evidence.")
    role = _role(role)
    weight = _weight(weight)
    note = _opt_text(note, "note")

    if evidence_id:
        eid, reused = _uuid(_req_text(evidence_id, "evidence_id"), "evidence_id"), True
    else:
        eid, reused = _new_evidence(conn, evidence, principal)

    final_role, already = _link(conn, did, eid, role, weight, note)
    row = conn.execute(
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM provenance WHERE object_type = 'evidence' "
        "AND object_id = %s AND attested_at IS NOT NULL) THEN 'attested' ELSE 'proposed' END "
        "AS state, EXISTS (SELECT 1 FROM decision_evidence WHERE decision_id = %s "
        "AND role = 'contradicts') AS contested", (eid, did)).fetchone()
    # Só cobra a evidência contrária quando este vínculo é novo, favorável, e a
    # decisão ainda não tem nenhuma contrária.
    nudge = ([ONLY_SUPPORTS] if final_role == "supports" and not already
             and not row["contested"] else [])
    return ({"decision_id": str(did), "evidence_id": str(eid), "role": final_role,
             "reused_existing": reused, "already_linked": already, "state": row["state"]},
            nudge)


def record_learning(conn, *, summary: str, decision_ids: list[str] | None,
                    evidence_ids: list[str] | None, tags: list[str] | None) -> dict[str, Any]:
    principal = require_principal(conn)
    summary = _req_text(summary, "summary")
    decision_ids = _text_list(decision_ids, "decision_ids")
    evidence_ids = _text_list(evidence_ids, "evidence_ids")
    tags = _text_list(tags, "tags")
    normalize_tags(tags)  # recusa tag fora do padrão antes de qualquer escrita
    if not decision_ids and not evidence_ids:
        raise ToolError("Uma lição precisa de origem: informe decision_ids ou evidence_ids.")
    # dict.fromkeys: tira repetidos sem mudar a ordem, e o vínculo não bate na PK.
    dids = list(dict.fromkeys(_uuid(d, "decision_id") for d in decision_ids))
    eids = list(dict.fromkeys(_uuid(e, "evidence_id") for e in evidence_ids))
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
