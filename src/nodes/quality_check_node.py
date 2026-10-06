"""AgentCore Platform v1.0"""

# RET-C2-027 — QualityCheckNode
#
# Final node of the inner workflow: it holds the rendered plan to the invariant
# the plan itself states, and refuses to pass one that breaks it.
#
# Three checks, each on the DOCUMENT rather than on the dataset it came from. A
# check that re-reads the model would only prove the model is self-consistent;
# these read the text that is about to be released, which is the thing the
# invariant is about.
#
#   1. Every promised section is present and carries content.
#   2. No percentage figure in the schedule exceeds its group's ceiling, and no
#      ceiling exceeds the plan-wide ceiling.
#   3. No monetary amount is rendered. This is the invariant that makes the
#      question of rounding currency moot: there is no currency in the document
#      to round. It is enforced rather than assumed, because a future change to
#      the renderer would otherwise reintroduce amounts silently.
#
# Node contract:
#   - extend FunctionNode; implement execute(state) -> dict
#   - return ONLY the fields this node changes (never the full state)
#   - return AgentStatus values, never plain strings

import logging
import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.nodes.document_generate_node import REQUIRED_SECTIONS
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Every number in a schedule row. The rows are rendered by this template, so the
# grammar is exact rather than heuristic: a leading group cell in backticks,
# then the numeric cells.
_SCHEDULE_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]{1,32})`\s*\|(.+)\|\s*$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# A monetary amount is a figure carrying a currency marker on either side,
# attached or separated: the ISO code form and the symbol form. The guards keep
# the marker from matching inside a word, so an inert product code such as
# `sku_usd_12` is not read as an amount — the plan's own identifiers render in
# lowercase over [a-z0-9_], and an ISO code is uppercase, so the two alphabets
# do not overlap in the first place.
_CURRENCY_SYMBOLS = "¥￥$€£円₩"
_ISO_CODES = r"JPY|USD|EUR|GBP|KRW|CNY|AUD|CAD|CHF|SGD|HKD|TWD|THB|VND|IDR|MYR|PHP|INR"
_MONETARY_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + _ISO_CODES + r")(?![A-Za-z0-9_])[ \t]*[+-]?\d"
    r"|[" + _CURRENCY_SYMBOLS + r"][ \t]*[+-]?\d"
    r"|\d[ \t]*[" + _CURRENCY_SYMBOLS + r"]"
    r"|\d[ \t]*(?<![A-Za-z0-9_])(?:" + _ISO_CODES + r")(?![A-Za-z0-9_])"
)

# Tolerance on the schedule comparison. Weekly figures are rendered as whole
# percentage points, so a ceiling of 67.5 legitimately prints a final week of
# 68 without breaching anything.
_ROUNDING_TOLERANCE_PCT = 0.5


def missing_sections(content: str) -> List[str]:
    """Promised sections absent from the rendered plan."""
    return [heading for heading in REQUIRED_SECTIONS if heading not in content]


def empty_sections(content: str) -> List[str]:
    """Promised sections present but carrying no content line."""
    empty: List[str] = []
    for heading in REQUIRED_SECTIONS:
        if heading not in content:
            continue
        body = section_body(content, heading)
        if not any(line.strip() and not line.strip().startswith("#") for line in body.splitlines()):
            empty.append(heading)
    return empty


def section_body(content: str, heading: str) -> str:
    """The lines under one heading, up to the next heading of the same level."""
    start = content.find(heading)
    if start == -1:
        return ""
    body = content[start + len(heading) :]
    following = re.search(r"\n##\s", body)
    return body[: following.start()] if following else body


# Which cells of a group row carry a DISCOUNT, per section. The tables are
# rendered by this template, so the column layout is known rather than guessed:
# reading every numeric cell as a discount would flag the product count in the
# schedule and the projected clearance rate, neither of which is one.
_DISCOUNT_CELLS = {
    # | group | products | ceiling | week 1 | ... |
    "## Week-by-Week Schedule": 1,
    # | group | avg discount | deepest discount | floor held |
    "## Margin Impact Analysis": 0,
}


def discount_breaches(content: str, ceilings: Dict[str, float], plan_ceiling: float) -> List[str]:
    """Group rows whose figures exceed the ceiling the group was planned to.

    Returns group labels, never the offending figures: a violation message is
    written into the log and the log is not a place to publish the number that
    should not have been published.
    """
    breaches: List[str] = []
    for heading, first_discount_cell in _DISCOUNT_CELLS.items():
        for line in section_body(content, heading).splitlines():
            match = _SCHEDULE_ROW_RE.match(line.strip())
            if not match:
                continue
            group = match.group(1)
            ceiling = ceilings.get(group)
            if ceiling is None:
                continue
            limit = min(ceiling, plan_ceiling) + _ROUNDING_TOLERANCE_PCT
            values = [float(value) for value in _NUMBER_RE.findall(match.group(2))]
            if any(value > limit for value in values[first_discount_cell:]):
                breaches.append(group)
    return sorted(set(breaches))


def monetary_amounts(content: str) -> bool:
    """Does the rendered plan contain a monetary amount?"""
    return bool(_MONETARY_RE.search(content))


class QualityCheckNode(FunctionNode):
    """Enforce the plan invariants on the rendered document.

    Inner domain graph node 5 / final (registered as "quality_check").

    Input state keys:
        markdown_plan_raw: the rendered plan
        enriched_data:     JSON string of the planning dataset

    Output state keys (partial dict):
        markdown_plan:      the plan, once it has passed every check
        clearance_metadata: JSON string of the plan summary statistics
        status:             AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:          (on error) list of messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        plan = state.get("markdown_plan_raw") or ""
        dataset: Dict[str, Any] = from_json(state.get("enriched_data"), {}) or {}

        if not str(plan).strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["QualityCheckNode: no plan was rendered"],
            }

        groups: List[Dict[str, Any]] = dataset.get("groups") or []
        ceilings = {group["category"]: float(group["ceiling_pct"]) for group in groups}
        plan_ceiling = float(dataset.get("max_discount_pct", 100.0))

        errors: List[str] = []

        absent = missing_sections(plan)
        if absent:
            errors.append(f"QualityCheckNode: the plan is missing required section(s): {', '.join(absent)}")

        blank = empty_sections(plan)
        if blank:
            errors.append(f"QualityCheckNode: required section(s) present but empty: {', '.join(blank)}")

        breaches = discount_breaches(plan, ceilings, plan_ceiling)
        if breaches:
            errors.append(
                f"QualityCheckNode: recommended discount exceeds the group ceiling for: {', '.join(breaches)}"
            )

        if monetary_amounts(plan):
            errors.append("QualityCheckNode: the plan renders a monetary amount, which its output contract forbids")

        if errors:
            logger.error("QualityCheckNode: plan rejected — %d finding(s)", len(errors))
            emit_trace_event("clearance_plan_rejected", {"finding_count": len(errors)}, state)
            return {
                "markdown_plan": None,
                "status": AgentStatus.ERROR.value,
                "error_log": errors,
            }

        deepest = max(ceilings.values(), default=0.0)
        metadata: Dict[str, Any] = {
            "sku_count": len(dataset.get("skus") or []),
            "group_count": len(groups),
            "max_discount_pct": round(plan_ceiling, 2),
            "horizon_weeks": int(dataset.get("horizon_weeks", 0)),
            "deepest_ceiling_pct": round(deepest, 2),
            "profile": dataset.get("profile", "default"),
            "sections_present": list(REQUIRED_SECTIONS),
        }

        logger.info(
            "QualityCheckNode: plan accepted — %d group(s), ceiling %.2f%%",
            len(groups),
            plan_ceiling,
        )

        emit_trace_event(
            "clearance_plan_accepted",
            {"group_count": len(groups), "sku_count": metadata["sku_count"], "plan_chars": len(plan)},
            state,
        )

        return {
            "markdown_plan": plan,
            "clearance_metadata": to_json(metadata),
            "status": AgentStatus.SUCCESS.value,
        }
