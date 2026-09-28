"""ENE-C2-127 — inner domain workflow graph (Cat 2).

Instantiated by MarketExtractAnomalyTriageWorkflowGraphNode.get_subgraph() in graph.py. Linear topology with
per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary):

    START → anomaly_detect → source_row_reference → worklist_compose → human_gate → END

On rejected / not-quality-validated / 0-anomaly input, anomaly_detect sets detected_count=0 (+error_code);
source_row_reference and human_gate no-op and worklist_compose emits the out-of-scope safe answer — no
fabricated worklist.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.anomaly_detect_node import AnomalyDetectNode
from src.nodes.human_gate_node import HumanGateNode
from src.nodes.source_row_reference_node import SourceRowReferenceNode
from src.nodes.worklist_compose_node import WorklistComposeNode
from src.schemas.state import State


class MarketExtractAnomalyTriageWorkflow(BaseGraph):
    """Inner graph: anomaly_detect → source_row_reference → worklist_compose → human_gate."""

    @property
    def name(self) -> str:
        return "MarketExtractAnomalyTriageWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["anomaly_detect"] = AnomalyDetectNode()
        self._nodes["source_row_reference"] = SourceRowReferenceNode()
        self._nodes["worklist_compose"] = WorklistComposeNode()
        self._nodes["human_gate"] = HumanGateNode()

    def add_edges(self) -> None:
        # Static linear backbone; the not-quality-validated / 0-anomaly / rejected skip is handled by
        # per-node guards.
        self._sg.add_edge(START, "anomaly_detect")
        self._sg.add_edge("anomaly_detect", "source_row_reference")
        self._sg.add_edge("source_row_reference", "worklist_compose")
        self._sg.add_edge("worklist_compose", "human_gate")
        self._sg.add_edge("human_gate", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("detected_count", 0) == 0:
            return "worklist_compose"
        return "source_row_reference"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "detected_count": state.get("detected_count", 0),
            "human_review_required": state.get("human_review_required", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
