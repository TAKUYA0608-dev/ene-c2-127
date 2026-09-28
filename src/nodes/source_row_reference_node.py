"""ENE-C2-127 — inner workflow step 2: source_row_reference.

Deterministically retrieves the cited anomaly-policy clause + baseline_ref + threshold_ref
(``<clause_id>@<version>`` from the seeded approved policy) for each detected anomaly so the triage worklist
is grounded in authorized clauses. Skips (no-op) on rejected / not-quality-validated / 0-anomaly input, after
emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractAnomalyService
from src.utils.audit import emit_trace_event


class SourceRowReferenceNode(FunctionNode):
    """Retrieve cited anomaly-policy + baseline + threshold clauses per detected anomaly."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("detected_count", 0) == 0:
            emit_trace_event("source_row_reference.skip", {"reason": state.get("error_code") or "no_anomalies"}, state)
            return {}

        anomalies = json.loads(state.get("detected_anomalies") or "[]")
        references = {a["anomaly_id"]: MarketExtractAnomalyService.retrieve_references(a) for a in anomalies}
        emit_trace_event(
            "source_row_reference.complete",
            {"anomalies": len(references), "policy_ref_count": sum(len(r["policy_refs"]) for r in references.values())},
            state,
        )
        return {"anomaly_references": json.dumps(references, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}
