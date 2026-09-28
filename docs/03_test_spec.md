# ENE-C2-127 — Test Specification

## Strategy

Three layers, all deterministic (no LLM, no network):

- **unit** — `tests/unit/test_nodes.py`: the deterministic `MarketExtractAnomalyService` (privacy tokenize
  vs provenance resolution, quality-status acceptance, normalize, detect each policy-defined anomaly type +
  severities, clause retrieval, worklist composition, triage summary) and each node in isolation (S-1/S-2
  hygiene + degrade, inner-node skip guards with S-4 emit, S-3 fail-closed + disclaimer gate).
- **unit (graph + real invoke)** — `tests/unit/test_graph.py`: outer GraphNode wiring (alias, class-level
  subgraph cache, extract/merge, outer-first error_code), inner-workflow route/registration, and
  **end-to-end through the real `Graph().invoke()`** for grounded / out-of-scope / not-quality-validated /
  injection-degrade / oversize-degrade / missing-provenance / unsafe-source / PII-tokenised /
  unknown-caller-field / forged-surrogate paths.
- **integration** — `tests/integration/test_end_to_end.py`: multi-anomaly extract prioritisation +
  grounding, mixed cited/uncited fail-closed, forged-surrogate id re-hash, empty → out-of-scope.

## Local result (local SDK stub)

- Core suites (`tests/unit/test_nodes.py`, `tests/unit/test_graph.py`, `tests/integration/`): **108 passed,
  1 skipped** (server import skipped when the platform module is unavailable in a local stub env),
  **coverage = 95%** (`--cov=src`, target ≥ 80%).
- Full `tests/` run: **110 passed, 3 skipped, 3 known env-diff failures** (`test_pb_invoke_order`,
  `test_framework_compliance_tc06_tc07::tc06/tc07`). These three assert framework-level `@final` /
  `emit_trace_event` enforcement that the local SDK stub shim does not implement; they **pass under the real SDK in
  CI** and are the unchanged scaffold conditional-stub / compliance files (byte-identical to the shipped
  scaffold and to the reference a sibling template).

## Key security test cases (real `Graph().invoke()`)

| # | Case | Expectation |
|---|------|-------------|
| TC-01 | Grounded threshold breach (authorized source, quality-validated) | `status=SUCCESS`, `status_kind=market_extract_anomaly_worklist`, `threshold_exception` ranked first, cited policy/baseline/threshold refs, `human_review.required=True`, DRAFT disclaimer |
| TC-02 | NL text / empty | out-of-scope safe answer, `citations=[]`, disclaimer present |
| TC-03 | Extract without a passed `quality_validation_status` | acceptance gate declines → out-of-scope, S-4 `error_code=NOT_QUALITY_VALIDATED`, quality message |
| TC-04 | Injection payload | degraded `SUCCESS` (never ERROR), post ran (`PostProcessNode` in `node_history`), out-of-scope envelope, marker body absent, terminal S-4 audit carries `error_code=INJECTION_REJECTED` |
| TC-05 | Oversize (> 200 000 chars) | degraded `SUCCESS`, S-4 audit carries `error_code=INPUT_TOO_LONG` |
| TC-06 | Missing provenance | S-3 fail-closed → `needs_review`, worklist body withheld, S-4 `error_code=CITATION_INCOMPLETE` |
| TC-07 | Unverifiable / unsafe source (name, phone, `roster:x`) | `needs_review`, raw source never in output |
| TC-08 | Forged surrogate source (`src:1a2b3c4d` / `row:deadbeef` / `acct:deadbeef`) | `needs_review`, `citations=[]`, forged value never in output |
| TC-09 | PII / no-space-name `source_row_id` (`Alice` / `TaroYamada`) | tokenized `row:<sha8>`, name never in output, referential integrity across worklist ↔ citations |
| TC-10 | Scope free text (company / phone / email) | redacted at S-3 — none appear in a grounded output |
| TC-11 | Unknown caller field carrying PII | whitelist-by-construction — never reaches output |
| TC-12 | Mixed cited + uncited anomalies | whole grounded worklist fails-closed to `needs_review` |

## Reproduce

```bash
source .venv/bin/activate
python -m pytest tests/unit/test_nodes.py tests/unit/test_graph.py tests/integration/ -q --cov=src --cov-report=term
ruff check src tests/unit/test_nodes.py tests/unit/test_graph.py tests/integration
python scripts/check_trust_level.py src/
python scripts/check_cat_consistency.py
python scripts/check_dep_pinning.py
```
