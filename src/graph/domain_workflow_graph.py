"""AgentCore Platform v1.0"""

# RET-C2-027 — DomainWorkflowGraph (inner graph)
#
# The inner half of the two-layer nested Cat 2 architecture. It encapsulates the
# whole clearance-planning workflow:
#
#   START -> input_parse -> template_select -> data_enrich -> document_generate
#         -> quality_check -> END
#
# Called by MarkdownPlanGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   - inherits BaseGraph (fully custom topology — no forced backbone)
#   - implements every BaseGraph abstract method
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - register_nodes() instantiates every domain node with NO ctor args
#   - does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() designed together with MarkdownPlanGraphNode.merge_output()
#   - no platform SDK imports

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_contract
from src.nodes.data_enrich_node import DataEnrichNode
from src.nodes.document_generate_node import DocumentGenerateNode
from src.nodes.input_parse_node import InputParseNode
from src.nodes.quality_check_node import QualityCheckNode
from src.nodes.template_select_node import TemplateSelectNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-027.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by MarkdownPlanGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          -> input_parse       (InputParseNode)       — read the validated contract
          -> template_select   (TemplateSelectNode)   — resolve the plan profile
          -> data_enrich       (DataEnrichNode)       — ceilings, groups, projections
          -> document_generate (DocumentGenerateNode) — render the plan
          -> quality_check     (QualityCheckNode)     — enforce the plan invariants
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_027_clearance_plan_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The forwarded plan settings are already shape-checked by
        MarkdownPlanGraphNode._parent_config(), which drops anything malformed
        rather than passing it on, so there is nothing left to reject here. An
        absent block is not an error either: every setting has a built-in
        default and the plan renders without any of them.
        """

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner state with everything that cannot travel as a string.

        The framework passes only the summary string into a nested graph, so two
        things are seeded here instead:

        `plan_config` — the live settings forwarded by
        MarkdownPlanGraphNode._parent_config(). Domain nodes take no config
        parameter, so state seeding is the only route runtime config can reach
        DataEnrichNode and DocumentGenerateNode.

        `caller_contract` — the validated product contract stashed on the bridge
        by MarkdownPlanGraphNode.extract_input() one step earlier. It carries the
        SKU records and the requested horizon, already bounds-checked by the
        request boundary.

        Both are stored as JSON strings rather than mappings, matching the
        serialization rule the shared state schema documents.
        """
        plan = (self.config or {}).get("configurable", {}).get("plan") or {}
        return {
            "plan_config": to_json(plan),
            "caller_contract": to_json(get_caller_contract()),
        }

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.

        Every node is instantiated with NO constructor arguments — a domain node
        implements execute(self, state) -> dict and reads its settings from
        seeded state, never from a constructor argument or a per-call config
        parameter. Every key registered here is referenced in add_edges().
        """
        self._nodes["input_parse"] = InputParseNode()
        self._nodes["template_select"] = TemplateSelectNode()
        self._nodes["data_enrich"] = DataEnrichNode()
        self._nodes["document_generate"] = DocumentGenerateNode()
        self._nodes["quality_check"] = QualityCheckNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear clearance-planning topology.

        Each step passes its partial-dict output into the shared State. The
        topology is intentionally linear — there is no branch, so
        add_conditional_edges() is not used and route() is never called at
        runtime.
        """
        self._sg.add_edge(START, "input_parse")
        self._sg.add_edge("input_parse", "template_select")
        self._sg.add_edge("template_select", "data_enrich")
        self._sg.add_edge("data_enrich", "document_generate")
        self._sg.add_edge("document_generate", "quality_check")
        self._sg.add_edge("quality_check", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Required by the BaseGraph contract; unused on a linear topology.

        Annotated with this graph's OWN State rather than the framework base:
        the graph runtime reads a path callable's annotation as its input schema
        and projects away every field the annotation does not carry, so a
        callable annotated with the base state would be handed a state with the
        domain fields missing. Returns END on error so an unexpected call cannot
        re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "quality_check"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by MarkdownPlanGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together so the field names cannot drift apart:

            Inner get_output()   emits: "markdown_plan", "clearance_metadata",
                                        "status", "trace_id", "correlation_id"
            Outer merge_output() reads: the first three

        On any non-success status the plan is withheld rather than surfaced: the
        outer node treats an inner error as fatal, and a partial plan that never
        passed the quality gate must not reach a merge path.
        """
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "markdown_plan": state.get("markdown_plan") if succeeded else None,
            "clearance_metadata": state.get("clearance_metadata") if succeeded else None,
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
