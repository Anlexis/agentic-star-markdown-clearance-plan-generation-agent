"""AgentCore Platform v1.0"""

# RET-C2-027 — InputParseNode
#
# First node of the inner workflow. It reads the validated product contract that
# the request boundary produced and the caller bridge carried across the
# outer/inner graph boundary, and republishes the records as the working list
# the rest of the pipeline reads.
#
# It does NOT re-validate: there is exactly one place that decides what a caller
# may send (src/services/caller_contract.py), and a second parser here would
# eventually disagree with it. What this node adds is the deterministic ordering
# the plan is rendered in — oldest stock first, then by product code — so two
# identical requests produce byte-identical plans.
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


def _markdown_priority(sku: Dict[str, Any]) -> float:
    """Score how urgently one product needs marking down.

    Shelf age and unsold share both push the score up, and the two are combined
    rather than ranked so a fast-selling old line does not outrank a new line
    that is not moving at all. The score is reported in the plan, so the reader
    can see why the schedule ordered the groups the way it did.
    """
    age_share = min(float(sku["days_on_shelf"]) / 180.0, 1.0)
    unsold_share = max(0.0, 100.0 - float(sku["sell_through_pct"])) / 100.0
    return round(100.0 * (0.6 * age_share + 0.4 * unsold_share), 2)


class InputParseNode(FunctionNode):
    """Republish the validated product records in deterministic plan order.

    Inner domain graph node 1 (registered as "input_parse").

    Input state keys:
        caller_contract: JSON string of the validated contract (seeded by the
                         inner graph's initial-state hook)

    Output state keys (partial dict):
        parsed_skus: JSON string of the ordered product records
        status:      AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:   (on error) list of messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        contract = from_json(state.get("caller_contract"), {}) or {}
        skus: List[Dict[str, Any]] = list(contract.get("skus") or [])

        if not skus:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputParseNode: the validated contract carries no product records"],
            }

        ordered: List[Dict[str, Any]] = []
        for sku in skus:
            record = dict(sku)
            record["markdown_priority"] = _markdown_priority(record)
            ordered.append(record)
        ordered.sort(key=lambda item: (-item["markdown_priority"], item["sku_id"]))

        logger.info("InputParseNode: ordered %d product record(s) for planning", len(ordered))

        emit_trace_event(
            "clearance_records_ordered",
            {"sku_count": len(ordered), "category_count": len({item["category"] for item in ordered})},
            state,
        )

        return {
            "parsed_skus": to_json(ordered),
            "status": AgentStatus.SUCCESS.value,
        }
