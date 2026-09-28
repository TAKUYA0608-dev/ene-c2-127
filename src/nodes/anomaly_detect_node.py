"""ENE-C2-127 — inner workflow step 1: anomaly_detect.

Enforces the **quality-validation-status acceptance gate** (an extract lacking a passed upstream
quality-validation status is not triaged), then deterministically ingests + normalizes the supplied source
rows and screens each (area, metric) group against the seeded anomaly / baseline / threshold policy for
policy-defined anomalies (missing_interval / duplicate / discontinuity / threshold_exception), with matched
drivers + evidence + severity. Sets ``detected_count``. **A not-quality-validated / rejected / 0-row / 0-anomaly
input routes to the out-of-scope safe answer** — the agent never fabricates a triage for data it did not
receive or is not authorized to triage. The free-text metadata is never interpreted semantically, so
prompt-like text in a supplied field cannot influence detection.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractAnomalyService, is_quality_validated
from src.utils.audit import emit_trace_event


class AnomalyDetectNode(FunctionNode):
    """Acceptance gate + deterministic ingest/normalize + policy-defined anomaly detection."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Rows arrive already validated + provenance-resolved by pre_process (S-1): each `source` is a grounded
        # citation `src:<sha8>` or None (a forged surrogate was dropped at S-1). We do not re-run provenance
        # here — normalize trusts that single upstream resolution.
        slots = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(slots, dict):
            slots = {}
        canonical = json.dumps(slots, ensure_ascii=False)
        rows = slots.get("source_rows") if isinstance(slots.get("source_rows"), list) else []

        # No data (or a pre-stage rejection carried in validated_input="{}") → out-of-scope safe answer.
        if state.get("error_code") or not rows:
            emit_trace_event("anomaly_detect.skip", {"reason": state.get("error_code") or "no_source_rows"}, state)
            return {
                "validated_input": canonical,
                "detected_anomalies": "[]",
                "detected_count": 0,
                "error_code": state.get("error_code") or "NO_ANOMALIES",
                "status": AgentStatus.SUCCESS.value,
            }

        # S-1 acceptance gate: an extract without a passed upstream quality-validation status is not triaged.
        if not is_quality_validated(slots.get("quality_validation_status")):
            emit_trace_event("anomaly_detect.skip", {"reason": "not_quality_validated"}, state)
            return {
                "validated_input": canonical,
                "detected_anomalies": "[]",
                "detected_count": 0,
                "error_code": "NOT_QUALITY_VALIDATED",
                "status": AgentStatus.SUCCESS.value,
            }

        normalized = MarketExtractAnomalyService.normalize(rows)
        if not normalized:
            emit_trace_event("anomaly_detect.skip", {"reason": "all_malformed"}, state)
            return {
                "validated_input": canonical,
                "detected_anomalies": "[]",
                "detected_count": 0,
                "error_code": "NO_ANOMALIES",
                "status": AgentStatus.SUCCESS.value,
            }

        anomalies = MarketExtractAnomalyService.detect(normalized)
        if not anomalies:
            # A quality-validated extract with no policy-defined anomalies → nothing to triage (clean).
            emit_trace_event("anomaly_detect.no_anomalies", {"rows": len(normalized)}, state)
            return {
                "validated_input": canonical,
                "detected_anomalies": "[]",
                "detected_count": 0,
                "error_code": "NO_ANOMALIES",
                "status": AgentStatus.SUCCESS.value,
            }

        distribution: dict[str, int] = {}
        for a in anomalies:
            distribution[a["anomaly_type"]] = distribution.get(a["anomaly_type"], 0) + 1
        emit_trace_event(
            "anomaly_detect.complete",
            {"rows": len(normalized), "detected": len(anomalies), "type_distribution": distribution},
            state,
        )
        return {
            "validated_input": canonical,
            "detected_anomalies": json.dumps(anomalies, ensure_ascii=False),
            "detected_count": len(anomalies),
            "status": AgentStatus.SUCCESS.value,
        }
