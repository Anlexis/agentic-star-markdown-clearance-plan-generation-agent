"""AgentCore Platform v1.0"""

# RET-C2-027 — DataEnrichNode
#
# Third node of the inner workflow, and the one that decides what the plan is
# allowed to recommend. For every product it computes the DEEPEST discount that
# may be recommended, as the tightest of four independent limits:
#
#   * the deployment's global ceiling            (plan.max_discount_pct)
#   * the product category's ceiling             (plan.margin_rules.<category>)
#   * the caller's own requested ceiling, if any (never wider than the above)
#   * the product's margin floor                 (100 - margin_floor_pct)
#
# The margin-floor limit reads the floor as a floor on the share of the price
# that must survive the markdown: a product carrying a 20% floor may be
# discounted by at most 80%. That is the reading the plan states in its own
# legend, so a reader can reproduce every ceiling in the document by hand.
#
# The settings come from seeded state, not from a per-call config parameter: the
# graph runtime calls execute(state) with one argument, so a node that read a
# `config` argument would silently see None on every real invocation and fall
# back to its module defaults while its unit tests, which pass one explicitly,
# stayed green.
#
# Node contract:
#   - extend FunctionNode; implement execute(state) -> dict
#   - return ONLY the fields this node changes (never the full state)
#   - return AgentStatus values, never plain strings

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Used only when the runtime configuration carries no plan block at all. The
# outer graph substitutes its own fall-back before that happens, so reaching
# these means the deployment is running without a configuration file.
DEFAULT_MAX_DISCOUNT_PCT = 70.0
DEFAULT_HORIZON_WEEKS = 4
DEFAULT_UPLIFT_FACTOR = 0.8
DEFAULT_ONLINE_PCT = 50.0


def _category_rule(margin_rules: Dict[str, Any], category: str) -> Dict[str, float]:
    """The rule governing one category, falling back to the default entry."""
    rule = margin_rules.get(category) or margin_rules.get("default") or {}
    return rule if isinstance(rule, dict) else {}


def _effective_ceiling(sku: Dict[str, Any], category_cap: float, global_cap: float) -> float:
    """The deepest discount this product may be offered at.

    The margin floor is the limit that cannot be configured away: a category
    rule may be looser than a product's own floor, and the product wins.
    """
    margin_limit = max(0.0, 100.0 - float(sku.get("margin_floor_pct", 0.0)))
    return round(min(global_cap, category_cap, margin_limit), 2)


class DataEnrichNode(FunctionNode):
    """Compute per-product ceilings and the category groups the plan renders.

    Inner domain graph node 3 (registered as "data_enrich").

    Input state keys:
        parsed_skus:      JSON string of the ordered product records
        plan_config:      JSON string of the live plan settings
        caller_contract:  JSON string of the validated contract (horizon, ceiling)
        selected_profile: inert profile key from TemplateSelectNode

    Output state keys (partial dict):
        enriched_data: JSON string of the planning dataset
        status:        AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:     (on error) list of messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        skus: List[Dict[str, Any]] = from_json(state.get("parsed_skus"), []) or []
        if not skus:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DataEnrichNode: no product records to plan for"],
            }

        plan_config: Dict[str, Any] = from_json(state.get("plan_config"), {}) or {}
        contract: Dict[str, Any] = from_json(state.get("caller_contract"), {}) or {}

        margin_rules: Dict[str, Any] = plan_config.get("margin_rules") or {}
        channel_split: Dict[str, Any] = plan_config.get("channel_split") or {}
        global_cap = float(plan_config.get("max_discount_pct", DEFAULT_MAX_DISCOUNT_PCT))
        horizon_weeks = int(plan_config.get("horizon_weeks", DEFAULT_HORIZON_WEEKS))
        uplift_factor = float(plan_config.get("clearance_uplift_factor", DEFAULT_UPLIFT_FACTOR))

        # A caller may ask for a tighter plan than the deployment allows, never
        # a looser one: the configured ceiling is the retailer's policy.
        requested_cap = contract.get("max_discount_pct")
        if requested_cap is not None:
            global_cap = min(global_cap, float(requested_cap))
        requested_horizon = contract.get("horizon_weeks")
        if requested_horizon is not None:
            horizon_weeks = int(requested_horizon)

        enriched: List[Dict[str, Any]] = []
        for sku in skus:
            record = dict(sku)
            rule = _category_rule(margin_rules, record["category"])
            category_cap = float(rule.get("max_discount_pct", global_cap))
            record["category_max_discount_pct"] = round(category_cap, 2)
            record["effective_max_discount_pct"] = _effective_ceiling(record, category_cap, global_cap)
            enriched.append(record)

        groups: List[Dict[str, Any]] = []
        for category in sorted({record["category"] for record in enriched}):
            members = [record for record in enriched if record["category"] == category]
            split = channel_split.get(category) or channel_split.get("default") or {}
            online_pct = (
                float(split.get("online_pct", DEFAULT_ONLINE_PCT)) if isinstance(split, dict) else DEFAULT_ONLINE_PCT
            )
            groups.append(
                {
                    "category": category,
                    "sku_count": len(members),
                    "sku_ids": [record["sku_id"] for record in members],
                    # The group is planned to the tightest ceiling any of its
                    # members carries, so no product in the group can be offered
                    # below its own margin floor by following the group row.
                    "ceiling_pct": round(min(record["effective_max_discount_pct"] for record in members), 2),
                    "avg_sell_through_pct": round(
                        sum(record["sell_through_pct"] for record in members) / len(members), 2
                    ),
                    "avg_days_on_shelf": round(sum(record["days_on_shelf"] for record in members) / len(members), 1),
                    "online_pct": round(min(max(online_pct, 0.0), 100.0), 1),
                }
            )

        enriched_data: Dict[str, Any] = {
            "skus": enriched,
            "groups": groups,
            "profile": state.get("selected_profile") or "default",
            "max_discount_pct": round(global_cap, 2),
            "horizon_weeks": horizon_weeks,
            "clearance_uplift_factor": uplift_factor,
        }

        logger.info(
            "DataEnrichNode: %d product(s) in %d group(s); ceiling=%.2f%%; horizon=%d week(s)",
            len(enriched),
            len(groups),
            global_cap,
            horizon_weeks,
        )

        emit_trace_event(
            "clearance_dataset_enriched",
            {
                "sku_count": len(enriched),
                "group_count": len(groups),
                "max_discount_pct": round(global_cap, 2),
                "horizon_weeks": horizon_weeks,
            },
            state,
        )

        return {
            "enriched_data": to_json(enriched_data),
            "status": AgentStatus.SUCCESS.value,
        }
