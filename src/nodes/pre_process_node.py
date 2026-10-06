"""AgentCore Platform v1.0"""

# RET-C2-027 — PreProcessNode
#
# The request boundary. Everything a caller can send is validated here, against
# src/services/caller_contract.py, before any planning code runs:
#
#   - the free-text field, which may be a plain merchandising note or a JSON
#     payload carrying the whole SKU list;
#   - the structured invocation parameters, which may carry the same SKU list as
#     real fields.
#
# The template owns this guarantee rather than delegating it to the framework:
# the framework's own input gate is configurable and absent in some deployments,
# and a screen that only runs when the platform happens to enable it fails open
# on exactly the request it exists to refuse. So the refusal is enforced in the
# node that owns the caller contract, and the tests call execute() directly with
# no framework wrapper in front of it.
#
# Node contract:
#   - extend FunctionNode; implement execute(state) -> dict
#   - return ONLY the fields this node changes (never the full state)
#   - return AgentStatus values, never plain strings
#   - read input_context via state.get("input_context", {}) — read-only

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress
from src.schemas.state import to_json
from src.services.caller_contract import (
    MAX_NOTE_CHARS,
    CallerContentRefused,
    CallerDataError,
    parse_note,
    parse_request,
    screen_text,
)

logger = logging.getLogger(__name__)


class PreProcessNode(FunctionNode):
    """Validate the caller's request and build the plan contract.

    Outer backbone pre_process slot.

    Input state keys:
        user_input:     free-text note, or a JSON payload of product records
        input_context:  structured invocation parameters (read-only)

    Output state keys (partial dict):
        caller_contract: JSON string of the validated contract
        validated_input: short, personal-data-stripped summary of the request
        status:          AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:       (on error) one message naming the field, never its value
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        # The free-text field is screened even when it carries no payload: a
        # plain note still reaches the plan header, and a directive hidden in it
        # is the cheapest attack on this entry point.
        if isinstance(user_input, str) and user_input.strip():
            hit = screen_text(user_input)
            if hit:
                # Terminal: a refusal, not a correctable value. Rewording the
                # request must not be presented as a route past it.
                return self._refuse(f"input contains a disallowed instruction pattern ({hit})", code="")

        try:
            contract = parse_request(user_input, input_context)
        except CallerContentRefused as exc:
            # Caught BEFORE CallerDataError: the subclass is what separates a
            # refusal from a correctable value, and the broad clause below would
            # otherwise swallow it and report it as one.
            return self._refuse(str(exc), code="")
        except CallerDataError as exc:
            return self._refuse(str(exc), code=exc.code)

        skus = contract.get("skus") or []
        if not skus:
            return self._refuse(
                "no product records were supplied — send them as input_context.skus " "or as a JSON array in input",
                code="EMPTY_INPUT",
            )

        try:
            note = parse_note(user_input if isinstance(user_input, str) else "", field="input")
        except CallerDataError:
            # An over-long note is not worth refusing the plan for; the records
            # are what the plan is built from.
            note = ""
        summary = (
            f"{len(skus)} product record(s); note: {note[:MAX_NOTE_CHARS]}"
            if note
            else f"{len(skus)} product record(s)"
        )

        emit_trace_event(
            "clearance_request_accepted",
            {"sku_count": len(skus), "categories": sorted({sku["category"] for sku in skus})},
            state,
        )

        return {
            "caller_contract": to_json(contract),
            "validated_input": summary,
            "status": AgentStatus.SUCCESS.value,
        }

    def _refuse(self, reason: str, code: str = "INVALID_REQUEST") -> Dict[str, Any]:
        """Stop the request, naming the field but never repeating its value.

        Two ways to stop, and the caller can act on only one of them. A value
        the caller can correct completes the run carrying ``code``, so the
        reason reaches the caller and a corrected request can be sent on the
        same conversation. Content the agent refuses outright passes ``code=""``
        and terminates, so a refusal is never presented as something a reworded
        request would get past.

        The branch is chosen by the call site through ``code``, never by reading
        *reason*: the screen refuses directly, and everything raised out of the
        contract parser carries its own reason class (``CallerDataError.code``,
        which ``CallerContentRefused`` sets to ``""``).
        """
        logger.warning("PreProcessNode: request refused — %s", reason)
        if code:
            # A value the caller can correct: the run COMPLETES carrying the
            # reason so the request can be sent again on the same conversation.
            emit_progress(INPUT_REJECTED)
            return {
                "caller_contract": None,
                "validated_input": None,
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"PreProcessNode: {reason}"],
            }
        return {
            "caller_contract": None,
            "validated_input": None,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {reason}"],
        }
