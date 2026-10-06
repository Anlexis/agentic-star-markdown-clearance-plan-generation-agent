"""AgentCore Platform v1.0"""

# RET-C2-027 — PostProcessNode: the output boundary of the agent.
#
# The stated output invariant is the one the plan prints in its own header: the
# document carries percentages, counts and weeks — no monetary amounts, no
# shopper personal data, and nothing credential-shaped. This node enforces that
# on the whole released surface, the plan AND the summary statistics the
# deployment monitors, with the module-level scan below.
#
# The scan is RECURSIVE. The summary statistics are a structured value, and a
# gate that only looked at top-level strings would report zero findings on a
# payload whose leak sits one level down.
#
# Two independent layers, each with its own audit event:
#   * inbound — the request boundary rewrites personal-data shapes out of caller
#     text before it is ever stored (src/services/caller_contract.py);
#   * outbound — this gate refuses to release anything still matching. Both
#     directions read ONE pattern definition, so they cannot drift apart.
#
# Credential shapes are checked with the FRAMEWORK's own detector rather than a
# local pattern list. A local list that is narrower anywhere is a containment
# bypass: the framework raises on a value it catches and this node misses, the
# node wrapper turns that into a bare error result carrying no cleared fields,
# and because partial results are MERGED into state the previous, un-gated plan
# survives in state["result"] and is released inside the error envelope. Using
# the same detector makes the two sets identical by construction.
#
# Containment on violation: returning an error is not enough on its own. The
# response envelope resolves to formatted_output or result whatever the status,
# so a gate that raised — or that set an error status without clearing the
# fields — would still ship the un-gated plan. This node therefore CLEARS every
# output-bearing field as it blocks, and the notice it substitutes is TRUTHY: an
# empty string in formatted_output would activate the fallback to result and
# produce the exact leak the clearing exists to prevent.
#
# Violation messages name the pattern and the field, never the matched value.
# The framework's own gate scans every value of every result, so echoing a
# matched credential into an error message makes that gate raise and discards
# this node's whole delta — including the clearing.
#
# No _extra_security_gate_input/_output methods are defined here: the framework
# auto-wraps such hooks, and defining them would change the node's call pipeline.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.nodes.quality_check_node import monetary_amounts
from src.services.caller_contract import find_personal_data

logger = logging.getLogger(__name__)

# Credential shapes the framework detector does not carry. These WIDEN the
# framework set; they never replace it, so this gate can only ever block more
# than the framework does — which is the property that keeps the framework from
# raising on something this node let through.
_EXTRA_CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # Publishable/anonymous key prefixes alongside the secret one the framework
    # already knows.
    ("api_key", re.compile(r"\b(?:pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # A secret written as an assignment rather than as a recognisable key shape.
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
]

_BLOCKED_NOTICE = (
    "[OUTPUT BLOCKED — disallowed content detected in the generated clearance plan. "
    "Remove credential-like strings and shopper personal data from the submitted "
    "product records and retry.]"
)

# Every state field that can carry released text. On a violation each one is
# overwritten, so no path out of the graph — including the envelope's fallback
# to state["result"] — can reach the un-gated plan.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "markdown_plan",
    "markdown_plan_raw",
    "clearance_metadata",
)


def security_gate_output(content: Any) -> Optional[str]:
    """Scan released content and name the first violation, or None if clean.

    Walks nested mappings and sequences, scanning every leaf. Returns the name
    of the matched pattern — never the matched text, which would put the leak
    into the log that reports it, and into a result the framework's own gate
    then refuses wholesale.
    """
    if content is None:
        return None
    if isinstance(content, dict):
        for value in content.values():
            hit = security_gate_output(value)
            if hit:
                return hit
        return None
    if isinstance(content, (list, tuple)):
        for item in content:
            hit = security_gate_output(item)
            if hit:
                return hit
        return None
    text = str(content)
    findings = detect_credentials(text)
    if findings:
        return str(findings[0]["type"])
    for name, pattern in _EXTRA_CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return name
    personal = find_personal_data(text)
    if personal:
        return personal
    return "monetary_amount" if monetary_amounts(text) else None


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Output boundary: refuse to release a plan that breaks the stated invariant."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
                "result": message,
            }
        result = state.get("result") or ""

        if not str(result).strip():
            # Nothing was generated — there is nothing to gate.
            return {
                "formatted_output": result,
                "status": AgentStatus.SUCCESS.value,
            }

        # The summary statistics are released into the same envelope as the
        # plan, so they are gated with it rather than after it.
        released = {
            "result": str(result),
            "clearance_metadata": state.get("clearance_metadata"),
        }
        violation = security_gate_output(released)
        if violation:
            logger.error("PostProcessNode: output blocked — violation type: %s", violation)
            emit_trace_event("clearance_plan_blocked", {"violation": violation}, state)
            blocked: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
            blocked.update(
                {
                    "formatted_output": _BLOCKED_NOTICE,
                    "result": _BLOCKED_NOTICE,
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PostProcessNode: output blocked — disallowed content detected ({violation})"],
                }
            )
            return blocked

        emit_trace_event("clearance_plan_emitted", {"plan_chars": len(str(result))}, state)

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
