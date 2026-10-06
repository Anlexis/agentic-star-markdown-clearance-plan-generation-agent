"""AgentCore Platform v1.0"""

# State is a flat TypedDict — never a Pydantic model. Graph checkpoints are
# serialized with msgpack, which cannot round-trip arbitrary objects, so a model
# in a State field corrupts silently. Extend AgentState with agent-specific
# fields only, and never put credentials or secrets in one.
#
# Msgpack safety: structured fields (dict / list[dict]) are stored as JSON
# STRINGS, not bare Python containers. Producers serialize with to_json() on
# write; consumers deserialize with from_json() on read.
#
# RET-C2-027 — Markdown & Clearance Plan Generator
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). Fields below cover both layers.
#
# Personal-data note: shopper identifiers — e-mail addresses, telephone numbers,
# loyalty and payment card numbers — are rewritten out of caller text at the
# request boundary (src/services/caller_contract.py) before any field below is
# written, and fields named for shopper data are withheld wholesale. Downstream
# nodes never see raw shopper identifiers, and the output boundary refuses to
# release anything still matching those shapes.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable from
    an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input gives the supplied ``default``, so a missing
    or corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for RET-C2-027.

    All shared fields (user_input, status, session_id, node_history, error_log,
    hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode / MarkdownPlanGraphNode.merge_output
    # ------------------------------------------------------------------

    # Personal-data-stripped summary line produced by PreProcessNode. Raw input
    # is NOT persisted beyond PreProcessNode.
    validated_input: Optional[str]

    # JSON STRING (to_json) of the validated caller contract built at the
    # request boundary: skus, channel, horizon_weeks, max_discount_pct. Every
    # value has already passed its bounded, inert shape check. Written by
    # PreProcessNode, carried across the outer/inner boundary by the caller
    # bridge, and re-seeded into inner state by
    # DomainWorkflowGraph._extra_initial_state().
    caller_contract: Optional[str]

    # JSON STRING (to_json) of the live plan settings forwarded from
    # config/config.yaml by MarkdownPlanGraphNode._parent_config().
    # Deserialised shape: {"max_discount_pct": float, "horizon_weeks": int,
    #                      "clearance_uplift_factor": float,
    #                      "margin_rules": {category: {...}},
    #                      "channel_split": {category: {"online_pct": float}}}
    plan_config: Optional[str]

    # Final validated markdown clearance plan document. Written by
    # QualityCheckNode inside the inner graph; surfaced via merge_output.
    markdown_plan: Optional[str]

    # `result` and `formatted_output` are inherited from AgentState. The response
    # envelope reads formatted_output first and falls back to result, so
    # PostProcessNode writes both.

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # InputParseNode output
    # JSON STRING (to_json) of the normalised product records. Deserialised
    # shape: list[dict], each {"sku_id", "category", "days_on_shelf",
    # "sell_through_pct", "current_price", "margin_floor_pct", "note"}.
    parsed_skus: Optional[str]

    # TemplateSelectNode output
    # Inert key of the plan profile chosen from the dominant product category
    # (for example "apparel"). Rendered into the plan header.
    selected_profile: Optional[str]

    # DataEnrichNode output
    # JSON STRING (to_json) of the merged planning dataset. Deserialised shape:
    # {"skus": [...], "groups": [...], "max_discount_pct": float,
    #  "horizon_weeks": int, "clearance_uplift_factor": float}
    enriched_data: Optional[str]

    # DocumentGenerateNode output
    # Rendered markdown clearance plan, before the quality gate has passed it.
    markdown_plan_raw: Optional[str]

    # QualityCheckNode output
    # JSON STRING (to_json) of the plan summary statistics. Deserialised shape:
    # {"sku_count": int, "group_count": int, "max_discount_pct": float,
    #  "horizon_weeks": int, "deepest_discount_pct": float,
    #  "projected_clearance_pct": float, "sections_present": [str]}
    clearance_metadata: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
