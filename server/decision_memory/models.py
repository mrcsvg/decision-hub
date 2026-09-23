"""Modelos de saída das seis ferramentas do servidor real.

Mesmo formato de MCP_TOOLS.md: cada ferramenta declara `outputSchema` a partir
destes modelos e responde com `structuredContent` no formato `{data, pending}`.

Copiados do stub de propósito. O README do stub diz que nada dele serve de
base além das descrições e do corpus, mas estes modelos não são do stub: são o
formato de MCP_TOOLS.md, e divergir dele aqui seria mudar o contrato.

Nenhum modelo de entrada tem campo de confiança ou expectativa, e isso é
deliberado: o que não está no schema o agente não é convidado a preencher.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReviewDue(BaseModel):
    decision_id: str
    title: str
    due_on: str
    overdue_days: int


class Pending(BaseModel):
    """O lembrete embutido. Vem vazio, nunca ausente."""

    on_this_record: list[str] = Field(
        default_factory=list,
        description="O que falta fechar no registro recém-criado. Só em respostas de escrita.",
    )
    reviews_due: list[ReviewDue] = Field(
        default_factory=list,
        description="No máximo três, priorizando quem compartilha tag com a consulta ou o registro.",
    )
    unattested_count: int = Field(
        0, description="Registros propostos aguardando atestação humana."
    )
    hint: str | None = Field(
        None, description="Uma frase dirigida ao agente. Nunca manda chamar ferramenta de outro servidor."
    )


class SourceRef(BaseModel):
    system: str | None = None
    external_id: str | None = None
    url: str | None = None


class SearchItem(BaseModel):
    type: str = Field(description="evidence, learning ou decision")
    id: str
    title: str
    summary: str | None = None
    strength: str | None = Field(None, description="causal, correlational ou anecdotal")
    source: SourceRef | None = None
    tags: list[str] = Field(default_factory=list)
    state: str


class SearchData(BaseModel):
    query: str
    items: list[SearchItem] = Field(default_factory=list)
    total: int = 0
    note: str | None = Field(
        None,
        description="Aviso quando a busca não encontra nada, para o agente dizer isso em voz alta.",
    )


class SearchResponse(BaseModel):
    data: SearchData
    pending: Pending


class Alternative(BaseModel):
    description: str
    rejection_reason: str


class DecisionEvidence(BaseModel):
    evidence_id: str
    title: str
    kind: str
    role: str = Field(description="supports, contradicts ou discarded")
    weight: float | None = None
    strength: str | None = None
    source: SourceRef | None = None
    note: str | None = None


class DecisionLearning(BaseModel):
    learning_id: str
    summary: str
    state: str


class DecisionReview(BaseModel):
    review_id: str
    due_on: str
    done_on: str | None = None
    verdict: str | None = None
    notes: str | None = None


class Expectation(BaseModel):
    """Só aparece em decisão atestada, e nunca é escrita por ferramenta alguma."""

    recorded_by: str
    recorded_at: str
    confidence: float
    expected_metric: str
    expected_magnitude: str
    due_on: str


class Provenance(BaseModel):
    author_kind: str
    model: str | None = None
    principal: str | None = None
    source_ref: str | None = None
    attested_by: str | None = None
    attested_at: str | None = None


class DecisionData(BaseModel):
    decision_id: str
    slug: str
    title: str
    context: str | None = None
    description: str
    door: str
    decided_on: str
    decider: str | None = None
    project: str | None = None
    state: str
    tags: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    evidence: list[DecisionEvidence] = Field(default_factory=list)
    learnings: list[DecisionLearning] = Field(default_factory=list)
    reviews: list[DecisionReview] = Field(default_factory=list)
    expectation: Expectation | None = Field(
        None, description="Incluída apenas se a decisão estiver atestada."
    )
    provenance: Provenance | None = None


class DecisionResponse(BaseModel):
    data: DecisionData
    pending: Pending


class ProposeData(BaseModel):
    decision_id: str
    slug: str
    state: str = "proposed"
    attest_url: str | None = Field(
        None, description="Nulo enquanto não houver superfície de atestação."
    )
    missing: list[str] = Field(
        default_factory=list, description="Campos que tornariam o registro útil."
    )
    reused: bool = Field(
        False,
        description=(
            "true quando a idempotency_key já tinha registrado esta decisão: volta o registro "
            "gravado e o conteúdo desta chamada não foi aplicado."
        ),
    )


class ProposeResponse(BaseModel):
    data: ProposeData
    pending: Pending


class AttachData(BaseModel):
    decision_id: str
    evidence_id: str
    role: str
    reused_existing: bool = Field(
        False, description="true quando (source_system, external_id) já existia."
    )
    already_linked: bool = Field(
        False, description="true quando a evidência já estava vinculada a essa decisão; nada mudou."
    )
    state: str = Field("proposed", description="Estado da evidência, derivado da procedência.")


class AttachResponse(BaseModel):
    data: AttachData
    pending: Pending


class LearningData(BaseModel):
    learning_id: str
    state: str = "proposed"
    linked_decisions: list[str] = Field(default_factory=list)
    linked_evidence: list[str] = Field(default_factory=list)


class LearningResponse(BaseModel):
    data: LearningData
    pending: Pending


class PendingReviewItem(BaseModel):
    review_id: str
    decision_id: str
    slug: str
    title: str
    due_on: str
    overdue_days: int
    owner: str | None = None
    project: str | None = None


class UnattestedItem(BaseModel):
    type: str
    id: str
    title: str
    created_on: str


class PendingReviewsData(BaseModel):
    reviews: list[PendingReviewItem] = Field(default_factory=list)
    unattested: list[UnattestedItem] = Field(default_factory=list)
    overdue_count: int = 0


class PendingReviewsResponse(BaseModel):
    data: PendingReviewsData
    pending: Pending
