"""ENE-C2-127 — Agent state (Electricity Market Extract Anomaly Triage Agent, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored
as JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests an already-approved, quality-checked JEPX-derived electricity-market
extract (30-min-granularity × area source rows + free-text metadata), screens it against the seeded,
organisation-owned anomaly policy + baseline period + threshold policy for policy-defined anomalies
(missing intervals / duplicates / discontinuities / threshold exceptions), and produces an
**AnalystTriageWorklist** deliverable of cited anomaly groups — it never forecasts, infers a cause (RCA),
advises a bid / trade / procurement, sends a transaction, or mutates the source data. The final
triage / data-quality decision is always an authorized human analyst's, and the worklist output is
candidate / needs-review only.

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the market-extract anomaly detection + analyst-worklist triage workflow."""

    # ── pre_process (ExtractIngest + SourceContainment; S-1 + S-2 pre-LLM) ──────
    validated_input: str  # JSON: {source_rows[], extract_version, quality_validation_status, scope}
    input_format: str  # "json" | "text" | "empty" | "rejected"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (anomaly_detect → source_row_reference → worklist_compose → human_gate) ─
    detected_anomalies: str  # JSON: [{anomaly_id, anomaly_type, area, metric, cited_source_rows[], citation}]
    detected_count: int  # anomalies detected (0 → out-of-scope safe answer, no fabricated triage)
    anomaly_references: str  # JSON: {anomaly_id: {policy_refs[], baseline_ref, threshold_ref}}
    result: str  # JSON: assembled AnalystTriageWorklist (incl. human_review)
    human_review_required: bool  # True once the HumanApprovalGate flags material findings
    review_status: str  # "pending_human_approval" | "not_required"

    # ── post_process (OutputSanitise — S-3 gate + S-4 audit) ──────────────────
    formatted_output: str  # JSON: final response envelope (worklist + disclaimer)
    disclaimer: str  # mandatory DRAFT / advisory-only disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ───
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NOT_QUALITY_VALIDATED | NO_ANOMALIES | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail
