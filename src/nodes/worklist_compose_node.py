"""ENE-C2-127 — inner workflow step 3: worklist_compose.

Composes the **AnalystTriageWorklist** deliverable: a triage summary, and a per-anomaly group (policy-defined
anomaly type, area/metric, cited source rows, cited anomaly-policy / baseline / threshold clauses,
needs-review mark), each cited to its authorized provenance. Anomaly groups are ordered by priority
(threshold breach / critical first). The worklist is candidate / advisory only — it never triages, mutates
the source data, or makes a data-quality decision. On the 0-anomaly / not-quality-validated / rejected branch
it emits the out-of-scope safe answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractAnomalyService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "トリアージ可能な policy-defined 異常が入力の電力市場 extract に見つかりませんでした。"
    "source_rows 配列に source_row_id・area・metric(price/volume)・slot(0-47)・value・source を含む "
    "JSON をご指定いただくか、対象範囲・期間を明確にしてください。"
)
_NOT_QUALITY_VALIDATED = (
    "入力 extract に upstream の品質検証ステータス(quality_validation_status=passed 等)が確認できなかったため、"
    "異常トリアージを実施しませんでした。承認済み・品質検査済み(quality-validation status 保持)の extract のみ "
    "triage 対象です。品質検証済みステータスを付与のうえ再実行してください。"
)


class WorklistComposeNode(FunctionNode):
    """Compose the AnalystTriageWorklist deliverable with citations (or safe answer on 0-anomaly)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        anomalies = json.loads(state.get("detected_anomalies") or "[]")
        if state.get("error_code") or not anomalies:
            reason = state.get("error_code") or "no_anomalies"
            message = _NOT_QUALITY_VALIDATED if reason == "NOT_QUALITY_VALIDATED" else _OUT_OF_SCOPE
            emit_trace_event("worklist_compose.safe", {"reason": reason}, state)
            report: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": message,
                "triage_summary": {},
                "anomaly_groups": [],
                "citations": [],
            }
            return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

        references = json.loads(state.get("anomaly_references") or "{}")
        groups: list[dict[str, Any]] = []
        citations: list[dict[str, str]] = []
        for a in anomalies:
            refs = references.get(a["anomaly_id"], {"policy_refs": [], "baseline_ref": None, "threshold_ref": None})
            groups.append(MarketExtractAnomalyService.compose_group(a, refs))
            citations.append({"anomaly_id": a["anomaly_id"], "source": a["citation"]})
        groups.sort(key=lambda g: (-g["priority_rank"], -g["severity_score"], g["anomaly_id"]))

        summary = MarketExtractAnomalyService.triage_summary(groups)
        report = {
            "status_kind": "market_extract_anomaly_worklist",
            "extract_version": self._extract_version(state),
            "scope": self._scope(state),
            "triage_summary": summary,
            "anomaly_groups": groups,
            "citations": citations,
        }
        emit_trace_event(
            "worklist_compose.complete", {"anomaly_count": len(groups), "citation_count": len(citations)}, state
        )
        return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _slots(state: dict[str, Any]) -> dict[str, Any]:
        slots = json.loads(state.get("validated_input") or "{}")
        return slots if isinstance(slots, dict) else {}

    @classmethod
    def _extract_version(cls, state: dict[str, Any]) -> Any:
        return cls._slots(state).get("extract_version")

    @classmethod
    def _scope(cls, state: dict[str, Any]) -> Any:
        return cls._slots(state).get("scope")
