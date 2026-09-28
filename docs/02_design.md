# Template Design Specification — ENE-C2-127

Electricity Market Extract Anomaly Triage Agent (Cat 2, GraphNode-in-main).

## Position in AgentCore Architecture

- **Agent Class**: `ElectricityMarketExtractAnomalyTriageAgent` (module-level alias of `Graph`)
- **L1 Base**: AgentBaseGraph (L1 direct — Cat 2 GraphNode-in-main; **not** AutonomousBaseGraph). The
  `ChatAgent` L2 pattern named in the proposal is a node-backbone / design reference only; the workflow is
  implemented directly on AgentBaseGraph (2026-05-18 L2-deprecation ruling), keeping the
  MarketExtractAnomalyTriage pattern inside this template.
- **Category**: Cat 2 — orchestrates a fixed multi-step workflow to produce one job-to-be-done deliverable
  (an AnalystTriageWorklist for an already-approved, quality-checked JEPX-derived market extract).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — no `config` param)
  - Graph: composition (`register_nodes()` for node substitution; domain complexity behind a `GraphNode`)
- **LLM**: none. The template is **fully deterministic** (interval-sequence gap / duplicate / step-change
  detection + threshold banding against a seeded, organisation-owned anomaly policy / baseline period /
  threshold policy, plus keyed clause composition). There is no model in `config/agent.yaml`, no LLM
  dependency in `pyproject.toml`, and no LLM call anywhere in `src/`. "pre-LLM" in the S-2 discussion below
  therefore means "before any downstream node reads the extract's free-text metadata"; that metadata is
  treated strictly as quoted data / schema-constrained input and is never interpreted semantically.

## Architecture Overview

### Node Configuration (outer 5-slot backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema/session/trust setup | user_input | caller_trust_level, session_id | InitializeNode (default) |
| pre_process | `ExtractIngest` + `SourceContainment` — S-1 normalisation (NFKC, size cap, required-field parse, quality-validation-status parse) + S-2 pre-LLM containment. **injection/oversize → degraded `SUCCESS + error_code`, offending body discarded (never `status=ERROR`)**. Field-level input hygiene (credential/My-Number/email/phone redaction); **PII display fields (analyst/owner/contact name/email/phone) dropped**; identifiers (`source_row_id`/`extract_id`) **UNCONDITIONALLY tokenized to opaque, non-reversible surrogates** (a bare name is opaque like any value, no surrogate→raw rejoin map kept); **provenance `source` resolved to a citation ONLY if it names an authorized market system of record (privacy-tokenized `src:<sha8>`), else dropped to `None`** — S-3 then blocks; `area`/`metric` constrained to a safe vocab; `extract_version`/`scope` hygiened | user_input | validated_input, input_format, enriched_context, (error_code) | PreProcessNode (FunctionNode) |
| main | `MarketExtractAnomalyTriageWorkflowGraphNode` — wraps inner `MarketExtractAnomalyTriageWorkflow` (composition criterion #9) | validated_input | result, detected_count, human_review_required, (error_code), status | GraphNode (subgraph) |
| post_process | `OutputSanitise` — S-3 output gate: **fail-closed per-anomaly citation completeness** (any ungrounded anomaly group → `needs_review` degrade, worklist body withheld, `error_code=CITATION_INCOMPLETE`) + analyst-name/company/phone/email/credential/My-Number re-redaction + DRAFT disclaimer, S-4 no-persist audit | result | formatted_output, disclaimer, audit_logged, (error_code) | PostProcessNode (FunctionNode) |
| finalize | build response envelope | formatted_output | output, status | FinalizeNode (default) |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (RETRY, max 3)
                                          pre_process
```

### Inner workflow (`src/graph/domain_workflow_graph.py` — BaseGraph, linear + per-node skip guard)

```
START → anomaly_detect → source_row_reference → worklist_compose → human_gate → END
```

| Inner Node | Responsibility | Skip guard |
|------|---------------|-----------|
| anomaly_detect | Enforce the **quality-validation-status acceptance gate** (an extract lacking a passed upstream quality-validation status is not triaged → `error_code=NOT_QUALITY_VALIDATED` → out-of-scope safe answer). Deterministic ingest + normalize of the supplied source rows against the seeded baseline / threshold policy; per (area, metric) group detect policy-defined anomalies (missing_interval / duplicate / discontinuity / threshold_exception, or unclassified) with matched drivers + evidence + severity banding; set `detected_count`. **0 valid rows / 0 anomalies → `error_code` → out-of-scope safe answer** (no fabricated triage). Free-text metadata is never interpreted semantically | — (first node; emits `.skip` on rejected / not-quality-validated / no-anomaly input) |
| source_row_reference | Deterministic retrieval of the cited anomaly-policy clause + baseline_ref + threshold_ref (`<clause_id>@<version>`) for each detected anomaly, grounding the worklist in authorized clauses | no-op `return {}` (after `.skip` emit) on `error_code` / `detected_count == 0` |
| worklist_compose | Compose the AnalystTriageWorklist deliverable: per anomaly group — the anomaly type, area/metric, cited source rows, cited policy/baseline/threshold clauses, a needs-review mark, and the source citation; ordered by priority (threshold/critical first). On 0-anomaly/rejected → out-of-scope safe answer | emits safe answer on `error_code` / no anomalies |
| human_gate | Deterministic **HumanApprovalGate**: mark `human_review_required=True` + `review_status="pending_human_approval"`, record material findings (every detected anomaly requires an authorized analyst's sign-off before any triage / data-quality action) into the worklist. The triage decision is **never** made by the agent | no-op `return {}` (after `.skip` emit) on `error_code` / `detected_count == 0` (safe answer needs no human gate) |

`MarketExtractAnomalyTriageWorkflowGraphNode.get_subgraph()` caches the compiled inner workflow on the
**class attribute** (`MarketExtractAnomalyTriageWorkflowGraphNode._subgraph`, not `self` — avoids mutable
node-instance state per §9; built once; `BaseGraph.invoke()` `_ensure_compiled` is idempotent).
`extract_input()` passes `validated_input` into the inner graph; `merge_output()` surfaces `result /
detected_count / human_review_required / error_code / status` — with **`error_code` OUTER-first**
(`state.get("error_code") or sub_result.get("error_code")`) so a pre-stage rejection survives to the
terminal S-4 audit (the inner workflow runs on the discarded body and would otherwise overwrite it with
`NO_ANOMALIES`).

## Security Model (S-1 … S-5)

- **S-1 (input normalisation + field hygiene + acceptance parse)**: NFKC + control-char strip + size cap;
  every string written into `validated_input` is passed through credential/My-Number/email/phone redaction;
  identifiers are tokenized to opaque surrogates; provenance resolved exactly once here; the
  `quality_validation_status` slot is parsed so the workflow can enforce the licensed/approved +
  quality-validated acceptance gate. **Injection detection/blocking is not attributed to S-1** — it is a
  degraded reject in the S-2 layer / execute (below), surfaced at the S-3 output gate.
- **S-2 (pre-LLM containment, market-data)**: two independent layers run before any node reads the extract's
  free-text metadata: (a) sensitive-data minimisation — PII display fields (analyst/owner/contact
  name/email/phone) are dropped, identifiers tokenized; (b) pre-LLM containment — the free-text metadata
  (source note / area label) is treated as quoted data / schema-constrained input and never followed as an
  instruction. Opaque IDs are non-reversible one-way hashes with **no surrogate→raw rejoin map in graph
  state**; the S-4 audit references only minimised counts. **Injection containment is pre-LLM**:
  prompt-injection markers or oversize input degrade to a safe out-of-scope answer *without any semantic
  execution of the offending text*.
  - **Degraded contract (SDK 1.0.0)**: an S-2 rejection is surfaced as **`status=SUCCESS` + `error_code`**
    (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) with the offending body discarded — it is
    **never `status=ERROR`** (which would short-circuit `route()` straight to `finalize`, skipping
    `post_process` and thus the disclaimer / S-3 redaction / S-4 audit). `post_process` therefore always
    runs and always delivers the out-of-scope safe answer + disclaimer + audit. `_extra_security_gate_input`
    MUST NOT raise and MUST `return dict(state)`; `execute()` re-checks the same conditions because the
    local stub framework does not invoke the `@final` hook.
- **S-3 (output gate, fail-closed)**: enforce per-anomaly citation completeness — a grounded
  AnalystTriageWorklist in which any anomaly group lacks a verifiable `source` citation (with an exact
  matching top-level `{anomaly_id, source}` entry) is **never presented**; it degrades to `needs_review`
  with the worklist body withheld (`error_code=CITATION_INCOMPLETE`, still SUCCESS). Re-redact any leaked
  secret/contact/name/company pattern (defense-in-depth). Append the mandatory DRAFT advisory disclaimer.
  `_extra_security_gate_output` receives the `execute()` result delta and MAY raise to block an output
  missing the disclaimer.
- **S-4 (audit, no-persist)**: every node `execute()` path — including every skip/0-count/degraded branch —
  emits a count-only domain trace event via `src.utils.audit.emit_trace_event`; payloads carry counts /
  anomaly-type distribution / policy-type keys / error codes only (no analyst name, free-text metadata, or
  raw extract row). The raw extract is not retained.
- **S-5 (rate limit / abuse)**: enforced at the platform entry point; the agent is read-only and performs
  no external write.

## Framework Utilization

- **S-2** `_extra_security_gate_input()` — size cap + injection markers (degraded SUCCESS + error_code, no raise).
- **S-3** `_extra_security_gate_output()` — DRAFT-disclaimer preservation (may raise).
- **S-4** `emit_trace_event()` — one count-only domain event on every `execute()` path (including skips).

> **S-2/S-3 gate behaviour by node type (ADR-017):** inner `MarketExtractAnomalyTriageWorkflowGraphNode` is a
> `GraphNode` (gate no-op — upstream `pre_process` already applied S-1/S-2); `pre_process` / `post_process`
> are `FunctionNode` subclasses whose `@final` gates always run.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (`src/` uses the `src.` prefix; PB-4)

## Read-only / non-execution boundary

The agent **never** forecasts, infers a cause (RCA), advises a bid / trade / procurement, sends a
transaction, or mutates the source data. All output is **candidate / needs-review**, and the final
triage / data-quality decision is always an authorized human analyst's, gated by the HumanApprovalGate (the
worklist proposes cited anomaly groups; it does not decide).

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step workflow, no autonomous loop |
| Composition pattern | Standalone | GraphNode (subgraph) | **GraphNode (subgraph)** | Cat 2 domain complexity behind `main` GraphNode |
| Detection engine | LLM | Deterministic | **Deterministic** | Auditable interval/threshold/step-change screening; Agent value = bounded interpretation + citation + worklist grouping |
| Provenance model | format passthrough | authorized-registry, resolved once at S-1 | **authorized-registry @ S-1** | Forged-surrogate defence; downstream trusts S-1 output verbatim |

## Open Items (Stage ③ implementation plan)

The design MR ships `docs/02` + `src/schemas/state.py` (+ a `docs/01` reconciliation) only. The Stage ③
implementation MR adds: the six node implementations (pre_process, the four inner nodes, post_process), the
inner/outer graph wiring (`get_subgraph` class-level caching + `merge_output` outer-first error_code), the
deterministic `MarketExtractAnomalyService` (seeded anomaly policy + baseline / threshold policy +
provenance/opaque-id helpers), `src/utils/audit.py` (S-4 shim), the
`ElectricityMarketExtractAnomalyTriageAgent = Graph` registry alias, and the unit / integration / real-invoke
tests (including the forged-surrogate, quality-status-gate, and PII-tokenization regressions). Seeded anomaly
policy / baseline / threshold policy are CoE-calibratable via a change-controlled engineer MR + specialist
review — they are not runtime-editable operational actions.
