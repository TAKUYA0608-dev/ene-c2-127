"""ENE-C2-127 — inner workflow step 4: human_gate (HumanApprovalGate).

Deterministic human-in-the-loop gate. It does **not** execute anything and it never triages — it flags the
material findings (every detected anomaly requires an authorized analyst's sign-off before any triage /
data-quality action) that require human review, records them + the review status into the worklist, and sets
``human_review_required``. Skips (no-op) on the rejected / not-quality-validated / 0-anomaly safe-answer
branch (no human gate needed) after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event


class HumanGateNode(FunctionNode):
    """Flag material anomaly findings requiring authorized analyst approval; set human_review_required."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report = json.loads(state.get("result") or "{}")
        if (
            state.get("error_code")
            or state.get("detected_count", 0) == 0
            or report.get("status_kind") != "market_extract_anomaly_worklist"
        ):
            emit_trace_event("human_gate.skip", {"reason": state.get("error_code") or "no_worklist"}, state)
            return {
                "human_review_required": False,
                "review_status": "not_required",
                "status": AgentStatus.SUCCESS.value,
            }

        material: list[dict[str, Any]] = []
        for group in report.get("anomaly_groups", []):
            # Every detected anomaly needs an authorized analyst before any triage / data-quality action;
            # high-severity / threshold breaches are always flagged. The agent proposes; it never decides.
            material.append(
                {
                    "anomaly_id": group["anomaly_id"],
                    "anomaly_type": group["anomaly_type"],
                    "area": group["area"],
                    "metric": group["metric"],
                    "severity": group["severity"],
                    "reason": "Candidate anomaly triage / policy-defined finding — requires authorized analyst "
                    "sign-off before any triage or data-quality action",
                }
            )

        required = bool(material)
        review = {
            "required": required,
            "status": "pending_human_approval" if required else "not_required",
            "note": "Anomaly triage and any data-quality action must be confirmed by an authorized human "
            "analyst. This agent produces a candidate cited worklist only.",
            "material_findings": material,
        }
        report["human_review"] = review
        emit_trace_event(
            "human_gate.complete", {"review_required": required, "material_finding_count": len(material)}, state
        )
        return {
            "result": json.dumps(report, ensure_ascii=False),
            "human_review_required": required,
            "review_status": review["status"],
            "status": AgentStatus.SUCCESS.value,
        }
