"""ENE-C2-127 — post_process node: OutputSanitise (S-3 output gate + S-4 audit).

S-3 (fail-closed): **enforce** per-anomaly citation completeness — a grounded AnalystTriageWorklist whose
anomaly groups are not all cited to a verifiable authorized source is never presented; it degrades to a safe
``needs_review`` answer with the worklist body withheld (``error_code=CITATION_INCOMPLETE``, still SUCCESS so
post/S-4/disclaimer run). Re-redact any credential / My-Number / email / phone / analyst- or company-name
leakage (defense-in-depth), and append the mandatory DRAFT advisory disclaimer — the worklist is a decision
aid, not a triage / data-quality decision; the final decision is a human analyst's, gated by the
HumanApprovalGate. S-4 (no-persist): emit an audit event (counts / type distribution / review flag /
error_code only — never an analyst name, free-text metadata, or raw source-row value); the raw extract is not
retained. Runs on the full worklist, the citation-blocked branch, and the out-of-scope safe branch.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event

_DISCLAIMER = (
    "本ワークリストは提供された承認済み・品質検査済みデータに基づく参考用の DRAFT（候補・要レビュー）"
    "電力市場 extract 異常トリアージ案であり、需給予測、根本原因（RCA）推論、bid/trade/procurement 助言、"
    "取引の送信、source データの改変を行うものではありません。最終的なトリアージ／データ品質判断は、必ず"
    "認可された人手のアナリストによる承認（HumanApprovalGate）を経てください。本エージェントは異常検出と"
    "cited worklist の草案を生成するのみで、実行は行いません。"
)

_CITATION_INCOMPLETE_MSG = (
    "ワークリストの一部の異常グループに検証可能な出典（provenance）が確認できなかったため、"
    "根拠不十分な異常トリアージ案の提示を差し控えました。各 source_row に認可済みシステムの参照（source）を"
    "付与のうえ再実行してください。"
)
_NEEDS_REVIEW_NOTE = (
    "Grounding could not be verified for every anomaly group; the draft triage worklist is withheld pending "
    "valid provenance and authorized human analyst review."
)

# S-3 defense-in-depth: re-redact secrets / contact info / analyst-or-company names that could leak into any
# free-text field of the worklist (applied to the whole serialized report before it becomes the output envelope).
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|\d{12})\b")
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
# Person / company names: an English name run ending in a corporate suffix, or a Japanese company form.
_NAME = re.compile(
    r"(?:[A-Z][A-Za-z0-9&.\-]*\s){1,4}(?:Inc|Corp|Corporation|Ltd|LLC|LLP|GmbH|PLC|K\.?K|KK)\b\.?"
    r"|[^\s\"',]{1,24}(?:株式会社|有限会社|合同会社)"
    r"|(?:株式会社|有限会社|合同会社)[^\s\"',]{1,24}"
)
_REDACTORS = (_SECRET, _EMAIL, _PHONE, _NAME)


def _redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Serialize → redact secret / contact / name patterns → deserialize (whole-report defense)."""
    text = json.dumps(report, ensure_ascii=False)
    for pattern in _REDACTORS:
        text = pattern.sub("[REDACTED]", text)
    return cast(dict[str, Any], json.loads(text))


class PostProcessNode(FunctionNode):
    """Verify citations, redact leakage, append the DRAFT advisory disclaimer, emit S-4 audit."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3 preservation check: the DRAFT advisory disclaimer must be present in the output envelope.

        SDK 1.0.0 contract: receives the **result dict from ``execute()``**; returns the (possibly filtered)
        result. MAY raise to block an output missing the mandatory disclaimer.
        """
        out = result.get("formatted_output", "")
        if out and "参考" not in out and "DRAFT" not in out:
            raise ValueError("S-3: DRAFT advisory disclaimer missing from output")
        return dict(result)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = _redact_report(json.loads(state.get("result", "{}") or "{}"))

        grounded = report.get("status_kind") == "market_extract_anomaly_worklist"
        citations = report.get("citations", [])
        anomaly_groups = report.get("anomaly_groups", [])
        # S-3 per-entry authoritative correspondence: every anomaly group must carry BOTH its own local
        # citation AND an exact top-level {anomaly_id, source} citation for the same anomaly (not merely a
        # non-empty citation list — a partially ungrounded worklist, or a top-level citation belonging to a
        # different anomaly, must fail closed).
        cited_sources = {c.get("anomaly_id"): c.get("source") for c in citations if c.get("source")}
        citation_complete = (not grounded) or (
            bool(anomaly_groups)
            and all(g.get("citation") for g in anomaly_groups)
            and all(cited_sources.get(g.get("anomaly_id")) == g.get("citation") for g in anomaly_groups)
        )

        # S-3 fail-closed: an ungrounded worklist (any anomaly missing a verifiable citation) is never
        # presented. Degrade to a safe needs-review answer (SUCCESS + error_code), withhold the worklist body,
        # and still run the disclaimer + terminal S-4 audit.
        if grounded and not citation_complete:
            error_code = state.get("error_code") or "CITATION_INCOMPLETE"
            blocked: dict[str, Any] = {
                "status_kind": "needs_review",
                "extract_version": report.get("extract_version"),
                "scope": report.get("scope"),
                "triage_summary": {},
                "anomaly_groups": [],  # incomplete worklist body withheld
                "human_review": {"required": True, "status": "pending_human_approval", "note": _NEEDS_REVIEW_NOTE},
                "citations": [],
                "citation_complete": False,
                "message": _CITATION_INCOMPLETE_MSG,
                "disclaimer": _DISCLAIMER,
            }
            emit_trace_event(
                "output_sanitise.citation_blocked",
                {"anomaly_count": len(anomaly_groups), "error_code": error_code},
                state,
            )
            return {
                "formatted_output": json.dumps(blocked, ensure_ascii=False),
                "disclaimer": _DISCLAIMER,
                "audit_logged": True,
                "error_code": error_code,
                "status": AgentStatus.SUCCESS.value,
            }

        human_review = report.get("human_review", {"required": False, "status": "not_required"})

        formatted = {
            "status_kind": report.get("status_kind"),
            "extract_version": report.get("extract_version"),
            "scope": report.get("scope"),
            "triage_summary": report.get("triage_summary", {}),
            "anomaly_groups": anomaly_groups,
            "human_review": human_review,
            "citations": citations,
            "citation_complete": citation_complete,
            "message": report.get("message"),
            "disclaimer": _DISCLAIMER,
        }
        emit_trace_event(
            "output_sanitise.complete",
            {
                "status_kind": report.get("status_kind"),
                "anomaly_count": len(anomaly_groups),
                "review_required": human_review.get("required", False),
                "citation_complete": citation_complete,
                "error_code": state.get("error_code"),
            },
            state,
        )
        return {
            "formatted_output": json.dumps(formatted, ensure_ascii=False),
            "disclaimer": _DISCLAIMER,
            "audit_logged": True,
            "status": AgentStatus.SUCCESS.value,
        }
