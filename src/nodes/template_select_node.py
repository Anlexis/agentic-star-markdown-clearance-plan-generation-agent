"""AgentCore Platform v1.0"""

# RET-C2-027 — TemplateSelectNode
#
# Second node of the inner workflow. It resolves the plan PROFILE: the category
# whose margin rule and channel split govern the plan as a whole, taken from the
# category carrying the most records.
#
# The profile is an inert label rather than a file path. An earlier revision
# resolved a template file here and read it from disk in the next node; a path
# that did not exist degraded to an empty string and the plan rendered without
# the policy it claimed to apply, silently. The rules now live in the runtime
# configuration and travel through seeded state, so an absent rule is a visible
# fall-back to the default profile rather than an empty document.
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
from src.schemas.state import from_json
from src.services.caller_contract import dominant_category

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"


class TemplateSelectNode(FunctionNode):
    """Resolve the plan profile from the dominant product category.

    Inner domain graph node 2 (registered as "template_select").

    Input state keys:
        parsed_skus: JSON string of the ordered product records
        plan_config: JSON string of the live plan settings

    Output state keys (partial dict):
        selected_profile: inert profile key ("apparel", "default", ...)
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:        (on error) list of messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        skus: List[Dict[str, Any]] = from_json(state.get("parsed_skus"), []) or []
        if not skus:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["TemplateSelectNode: no product records to plan for"],
            }

        plan_config: Dict[str, Any] = from_json(state.get("plan_config"), {}) or {}
        margin_rules: Dict[str, Any] = plan_config.get("margin_rules") or {}

        category = dominant_category(skus)
        profile = category if category in margin_rules else DEFAULT_PROFILE

        logger.info("TemplateSelectNode: dominant_category=%s profile=%s", category, profile)

        emit_trace_event(
            "clearance_profile_selected",
            {"dominant_category": category, "profile": profile},
            state,
        )

        return {
            "selected_profile": profile,
            "status": AgentStatus.SUCCESS.value,
        }
