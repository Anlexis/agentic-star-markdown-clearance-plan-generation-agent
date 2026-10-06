"""AgentCore Platform v1.0"""

# Caller-request bridge across the outer/inner graph boundary.
#
# Why it exists: the framework invokes a nested graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)`. Only the free-text
# string crosses — neither the outer state nor the caller's structured
# invocation parameters are forwarded. So the validated product contract the
# pre_process node produces (the SKU records, the requested horizon, the
# requested ceiling) would never reach the planning pipeline on its own.
#
# The two sanctioned subclass hooks bridge it:
#
#   MarkdownPlanGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_contract(<validated contract>)
#   DomainWorkflowGraph._extra_initial_state()   [runs INSIDE subgraph.invoke]
#       -> seeds the contract into the inner state
#
# What crosses is the VALIDATED contract only — every value has already passed
# its bounded, inert shape check. The raw request body never travels.
#
# Smuggling the SKU list inside the free-text string instead is not viable: the
# framework masks that field at every node boundary, and product codes that look
# like account numbers trip the masking heuristics, so the pipeline would plan
# from corrupted data. This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread and per task, so concurrent
# invocations inside one process cannot see each other's request.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CALLER_CONTRACT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ret_c2_027_caller_contract", default=None)


def set_caller_contract(contract: Optional[Dict[str, Any]]) -> None:
    """Stash the validated caller contract for the imminent inner-graph invoke."""
    _CALLER_CONTRACT.set(dict(contract) if contract else {})


def get_caller_contract() -> Dict[str, Any]:
    """Read (without consuming) the stashed contract; {} when none was set."""
    return _CALLER_CONTRACT.get() or {}
