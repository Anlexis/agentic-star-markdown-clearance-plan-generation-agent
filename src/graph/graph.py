"""AgentCore Platform v1.0"""

# RET-C2-027 — outer graph (two-layer nested Cat 2 architecture)
#
# Markdown & Clearance Plan Generator — turns a retailer's slow-moving product
# list into a week-by-week markdown and clearance plan.
#
# Architecture:
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (retry, bounded by max_retry)
#                                             -> pre_process
#
#   The `main` slot is a GraphNode subclass (MarkdownPlanGraphNode) that
#   delegates the whole domain workflow to DomainWorkflowGraph (inner graph:
#   input_parse -> template_select -> data_enrich -> document_generate ->
#   quality_check).
#
#   Domain complexity is fully encapsulated inside the inner graph; the outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- validated caller contract across the boundary
#
# Class-name contract:
#   graph.py class:           RetC2027Agent (this file)
#   config/agent.yaml class:  "src.graph.graph.RetC2027Agent"
#   src/api/server.py import: from src.graph.graph import RetC2027Agent
#
# Rules enforced:
#   - RetC2027Agent inherits AgentBaseGraph (direct framework inheritance)
#   - super().register_nodes() called first (fills initialize + finalize)
#   - MarkdownPlanGraphNode assigned to self._nodes["main"]
#   - _parent_config() forwards the LIVE runtime config (never {})
#   - merge_output() returns only changed keys
#   - add_edges() NOT overridden on the outer graph
#   - No platform SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.utils.config_loader import load_agent_config
from src.graph.context_bridge import set_caller_contract
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json
from src.services.caller_contract import (
    HORIZON_WEEKS_MAX,
    HORIZON_WEEKS_MIN,
    PERCENT_MAX,
    CallerDataError,
    parse_int,
    parse_label,
    parse_number,
)

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors config/config.yaml, so the plan settings are never empty even where
# the config file is unreadable in an exotic deployment layout.
_FALLBACK_PLAN: Dict[str, Any] = {
    "max_discount_pct": 70.0,
    "horizon_weeks": 4,
    "clearance_uplift_factor": 0.8,
    "margin_rules": {
        "default": {"max_discount_pct": 50.0, "margin_floor_pct": 20.0},
    },
    "channel_split": {
        "default": {"online_pct": 50.0},
    },
}

# Upper bound on the number of category rules a deployment may configure. A
# configuration file is not caller data, but it is still input, and an unbounded
# table would be rendered into the plan legend.
MAX_CATEGORY_RULES = 64
UPLIFT_FACTOR_MAX = 5.0


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The registry loads this file and passes it to the graph constructor; the
    standalone HTTP entry point does the same, so `max_retry` and the plan
    settings are live in both deployments rather than declared and ignored.

    Reading the static manifest (config/agent.yaml) here instead would return
    nothing: the manifest carries identity and compile-time requirements only,
    and a reader pointed at it degrades silently to defaults.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


def _validated_category_table(raw: object, *, field: str, keys: Dict[str, float]) -> Dict[str, Dict[str, float]]:
    """Validate a per-category settings table from the configuration file.

    Category keys are rendered into the plan as group labels, so they go through
    the same inert-label check caller data does, and every figure goes through
    the same finite + bounded parser. An entry that fails any check is dropped
    rather than raised: a malformed configuration file must not stop the agent
    from compiling, and the built-in table is always a correct answer.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    table: Dict[str, Dict[str, float]] = {}
    for index, (category, settings) in enumerate(raw.items(), start=1):
        if index > MAX_CATEGORY_RULES or not isinstance(settings, dict):
            continue
        try:
            label = parse_label(category, field=field)
            entry = {
                name: parse_number(
                    settings.get(name, fallback), field=f"{field}.{name}", minimum=0.0, maximum=PERCENT_MAX
                )
                for name, fallback in keys.items()
            }
        except CallerDataError:
            continue
        table[label] = entry
    return table


class MarkdownPlanGraphNode(GraphNode):
    """The `main` slot: wraps the inner clearance-planning workflow.

    Contracts:
      get_subgraph()    - instantiate DomainWorkflowGraph with the forwarded
                          runtime config (_parent_config())
      extract_input()   - hand the validated summary line to the inner graph and
                          stash the validated caller contract on the bridge
      merge_output()    - map sub_result fields into the outer state delta
      error_strategy    - "propagate": re-raise inner errors (fail fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail fast).
    # "handle": call on_subgraph_error() instead — for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: human-review interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the live plan settings to the inner graph.

        Returns the settings under config["configurable"] — never an empty dict.
        The inner graph republishes them into inner state
        (DomainWorkflowGraph._extra_initial_state()) so the enrichment and
        rendering nodes read live values: node execute() methods take no config
        parameter, so state seeding is the only route config can travel.
        """
        plan = runtime_config().get("plan")
        if not isinstance(plan, dict) or not plan:
            plan = dict(_FALLBACK_PLAN)

        try:
            max_discount_pct = parse_number(
                plan.get("max_discount_pct", _FALLBACK_PLAN["max_discount_pct"]),
                field="plan.max_discount_pct",
                minimum=0.0,
                maximum=PERCENT_MAX,
            )
        except CallerDataError:
            max_discount_pct = float(_FALLBACK_PLAN["max_discount_pct"])
        try:
            horizon_weeks = parse_int(
                plan.get("horizon_weeks", _FALLBACK_PLAN["horizon_weeks"]),
                field="plan.horizon_weeks",
                minimum=HORIZON_WEEKS_MIN,
                maximum=HORIZON_WEEKS_MAX,
            )
        except CallerDataError:
            horizon_weeks = int(_FALLBACK_PLAN["horizon_weeks"])
        try:
            uplift = parse_number(
                plan.get("clearance_uplift_factor", _FALLBACK_PLAN["clearance_uplift_factor"]),
                field="plan.clearance_uplift_factor",
                minimum=0.0,
                maximum=UPLIFT_FACTOR_MAX,
            )
        except CallerDataError:
            uplift = float(_FALLBACK_PLAN["clearance_uplift_factor"])

        margin_rules = _validated_category_table(
            plan.get("margin_rules"),
            field="plan.margin_rules",
            keys={"max_discount_pct": max_discount_pct, "margin_floor_pct": 0.0},
        ) or dict(_FALLBACK_PLAN["margin_rules"])
        channel_split = _validated_category_table(
            plan.get("channel_split"),
            field="plan.channel_split",
            keys={"online_pct": 50.0},
        ) or dict(_FALLBACK_PLAN["channel_split"])

        settings: Dict[str, Any] = {
            "max_discount_pct": max_discount_pct,
            "horizon_weeks": horizon_weeks,
            "clearance_uplift_factor": uplift,
            "margin_rules": margin_rules,
            "channel_split": channel_split,
        }
        return {"configurable": {"plan": settings}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        Imported inside the method to avoid circular-import risk at module load
        time. The inner graph receives the runtime-derived config through its
        constructor; its domain nodes still take no constructor arguments and
        read config per call from seeded state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the summary line, and bridge the validated caller contract.

        The framework hands only a string to the inner graph, so the structured
        part of the request travels on the bridge instead — set here, one step
        before the inner invoke, and read by the inner graph's initial-state
        hook. Only the contract the pre_process node already validated crosses.
        """
        set_caller_contract(from_json(state.get("caller_contract"), {}) or {})
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner result back into the outer state delta (changed keys only).

        Key coupling, designed together with DomainWorkflowGraph.get_output():

          Inner get_output() emits  -> "markdown_plan", "clearance_metadata", "status"
          This merge_output() reads -> the same three keys

        `result` is set as well as `markdown_plan`: the post_process slot and the
        response envelope both read state["result"], so without that mapping the
        gated output would always be empty.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "markdown_plan": sub_result.get("markdown_plan"),
            "result": sub_result.get("markdown_plan"),
            "clearance_metadata": sub_result.get("clearance_metadata"),
            "status": sub_result.get("status"),
        }


class RetC2027Agent(AgentBaseGraph):
    """Outer graph for RET-C2-027.

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    MarkdownPlanGraphNode (main slot), which delegates to DomainWorkflowGraph.

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY topology override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (trust gate + caller-contract validation)
      - main:         MarkdownPlanGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "MarkdownClearancePlanGenerator"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize node (schema version, session id, trust
        level) and finalize node (response metadata, total elapsed time).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = MarkdownPlanGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — earlier releases imported the outer graph as `Graph`.
Graph = RetC2027Agent
