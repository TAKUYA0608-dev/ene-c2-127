# ENE-C2-127 — Integration: full outer Graph().invoke() across a multi-anomaly extract

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph

_SUCCESS = AgentStatus.SUCCESS.value


def _invoke(user_input: str):
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return Graph().invoke(user_input, ctx=ctx)


def _row(rid, slot, value, area="tokyo", metric="price", source="jepx:e"):
    r = {"source_row_id": rid, "area": area, "metric": metric, "slot": slot, "value": value}
    if source is not None:
        r["source"] = source
    return r


def test_multi_anomaly_extract_prioritised_and_grounded():
    payload = {
        "quality_validation_status": "passed",
        "extract_version": "jepx-2026-07-01-v1",
        "scope": "apac-day-ahead",
        "source_rows": [
            _row("r0", 0, 10.0), _row("r1", 1, 12.0), _row("r2", 2, 15.0),
            _row("r4", 4, 60.0),     # gap at slot 3 → missing_interval; step 15→60 → discontinuity
            _row("r5", 5, 250.0),    # threshold_exception; step 60→250 → discontinuity (high)
        ],
    }
    out = _invoke(json.dumps(payload))
    assert out["status"] == _SUCCESS
    env = json.loads(out["output"])
    assert env["status_kind"] == "market_extract_anomaly_worklist"
    assert env["anomaly_groups"] and len(env["citations"]) == len(env["anomaly_groups"])
    # threshold breach is the highest-priority anomaly type → ranked first
    assert env["anomaly_groups"][0]["anomaly_type"] == "threshold_exception"
    types = {g["anomaly_type"] for g in env["anomaly_groups"]}
    assert {"missing_interval", "discontinuity", "threshold_exception"} <= types
    assert env["triage_summary"]["total_anomalies"] == len(env["anomaly_groups"])
    assert env["human_review"]["required"] is True
    assert "DRAFT" in env["disclaimer"]


def test_mixed_cited_and_uncited_blocks_whole_worklist():
    """Per-anomaly citation completeness: one uncited anomaly fails-closed the whole grounded worklist."""
    payload = {"quality_validation_status": "passed", "source_rows": [
        _row("a", 0, 250.0, area="tokyo", source="jepx:a"),        # cited threshold breach
        _row("b", 0, 250.0, area="kansai", source=None),           # uncited threshold breach
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "needs_review"
    assert env["anomaly_groups"] == [] and env["citations"] == []


def test_forged_source_row_id_surrogate_rehashed():
    # ★ a caller value SHAPED like an internal surrogate (row:deadbeef) is re-hashed at S-1 (no syntactic
    # passthrough), so it can never forge an internal join key / reference another row.
    payload = {"quality_validation_status": "passed",
               "source_rows": [_row("row:deadbeef", 10, 250.0, source="jepx:a")]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "market_extract_anomaly_worklist"
    tok = env["anomaly_groups"][0]["cited_source_rows"][0]
    assert tok.startswith("row:") and tok != "row:deadbeef"   # re-hashed, not passthrough
    assert "row:deadbeef" not in out["output"]


def test_empty_object_is_out_of_scope():
    out = _invoke(json.dumps({"quality_validation_status": "passed", "source_rows": []}))
    env = json.loads(out["output"])
    assert env["status_kind"] == "out_of_scope" and env["citations"] == []
