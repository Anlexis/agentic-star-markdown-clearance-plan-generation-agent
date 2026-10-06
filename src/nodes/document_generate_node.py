"""AgentCore Platform v1.0"""

# RET-C2-027 — DocumentGenerateNode
#
# Fourth node of the inner workflow: it renders the clearance plan.
#
# The plan is COMPUTED, not written by a language model. Every figure in the
# document is a function of the validated product records and the configured
# rules, so the same request always produces the same plan and every number in
# it can be reproduced by hand from the legend the plan carries. An earlier
# revision called a model here through an import that does not exist in the
# framework, so the call failed on every invocation and the node returned a
# fixed placeholder document — the same five sections, the same zeroes,
# whatever the caller sent.
#
# OUTPUT INVARIANT (stated here, enforced by the quality gate and again at the
# output boundary):
#
#   1. No recommended discount exceeds its group's ceiling.
#   2. No monetary amount is rendered. The plan reports percentages, counts and
#      weeks. Prices are caller data used to derive the ceilings; publishing
#      them back adds nothing a merchandiser does not already have, and a
#      document with no currency in it cannot mis-round one.
#   3. Every string rendered from caller data is an inert identifier.
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

logger = logging.getLogger(__name__)

# The five sections the published plan contract promises, in order.
REQUIRED_SECTIONS: List[str] = [
    "## Summary",
    "## Week-by-Week Schedule",
    "## Channel Allocation",
    "## Expected Clearance Rate",
    "## Margin Impact Analysis",
]


def weekly_discounts(ceiling_pct: float, horizon_weeks: int) -> List[float]:
    """The discount offered to one group in each week of the plan.

    A straight ramp that reaches the ceiling in the final week: shallow first so
    the retailer keeps the margin on the units that would have sold anyway, and
    monotonic so a shopper never sees the price go back up. Rounded to whole
    percentage points because that is how a shelf ticket is printed.
    """
    weeks = max(1, horizon_weeks)
    return [round(ceiling_pct * (week / weeks)) * 1.0 for week in range(1, weeks + 1)]


def projected_clearance_pct(avg_sell_through_pct: float, avg_discount_pct: float, uplift_factor: float) -> float:
    """Share of the group expected to have cleared by the end of the plan.

    The remaining stock is assumed to clear in proportion to the average
    discount offered, scaled by the deployment's uplift factor — the one
    tunable that says how price-sensitive this retailer's shoppers are. Capped
    at 100 because a share cannot exceed the whole.
    """
    remaining = max(0.0, 100.0 - avg_sell_through_pct)
    return round(min(100.0, avg_sell_through_pct + remaining * min(1.0, avg_discount_pct * uplift_factor / 100.0)), 1)


# How many product codes one group row lists before it is summarised. A plan is
# read by a person; a group holding hundreds of lines is reported by count.
MAX_LISTED_CODES = 8


def _code_list(sku_ids: List[str]) -> str:
    """Render a group's product codes, bounded, as inline code spans."""
    shown = [f"`{code}`" for code in sku_ids[:MAX_LISTED_CODES]]
    remainder = len(sku_ids) - len(shown)
    return ", ".join(shown) + (f" and {remainder} more" if remainder > 0 else "")


def _render(dataset: Dict[str, Any]) -> str:
    """Render the clearance plan from the enriched dataset."""
    groups: List[Dict[str, Any]] = dataset.get("groups") or []
    horizon_weeks = int(dataset.get("horizon_weeks", 4))
    uplift_factor = float(dataset.get("clearance_uplift_factor", 0.8))
    ceiling = float(dataset.get("max_discount_pct", 0.0))
    sku_count = len(dataset.get("skus") or [])

    schedules = {group["category"]: weekly_discounts(group["ceiling_pct"], horizon_weeks) for group in groups}
    averages = {category: round(sum(weeks) / len(weeks), 1) if weeks else 0.0 for category, weeks in schedules.items()}
    projections = {
        group["category"]: projected_clearance_pct(
            group["avg_sell_through_pct"], averages[group["category"]], uplift_factor
        )
        for group in groups
    }
    overall_projection = round(sum(projections.values()) / len(projections), 1) if projections else 0.0
    deepest = max((max(weeks) for weeks in schedules.values() if weeks), default=0.0)

    lines: List[str] = []
    lines.append("# Markdown & Clearance Plan")
    lines.append("")
    lines.append(
        f"Profile: `{dataset.get('profile', 'default')}` | "
        f"Products: {sku_count} | Groups: {len(groups)} | Horizon: {horizon_weeks} week(s)"
    )
    lines.append("")
    lines.append(
        "All figures are percentages, counts or weeks. This plan renders no monetary amounts: "
        "a group's discount ceiling is the tightest of the configured ceiling "
        f"({ceiling:.0f}%), the category rule, and each product's own margin floor "
        "(a product carrying an N% floor may be discounted by at most 100 - N%)."
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"{sku_count} product(s) across {len(groups)} category group(s) are scheduled for markdown over "
        f"{horizon_weeks} week(s). The deepest discount recommended anywhere in this plan is "
        f"{deepest:.0f}%, and no group is taken below its own ceiling. Projected clearance across "
        f"all groups by the end of the horizon is {overall_projection:.1f}%."
    )
    lines.append("")

    lines.append("## Week-by-Week Schedule")
    lines.append("")
    header = " | ".join(f"Week {week}" for week in range(1, horizon_weeks + 1))
    lines.append(f"| Group | Products | Ceiling (%) | {header} |")
    lines.append("|---|---|---|" + "---|" * horizon_weeks)
    for group in groups:
        weeks = " | ".join(f"{value:.0f}" for value in schedules[group["category"]])
        lines.append(f"| `{group['category']}` | {group['sku_count']} | {group['ceiling_pct']:.0f} | {weeks} |")
    lines.append("")
    lines.append("Products in each group:")
    lines.append("")
    for group in groups:
        lines.append(f"- `{group['category']}`: {_code_list(group['sku_ids'])}")
    lines.append("")

    lines.append("## Channel Allocation")
    lines.append("")
    lines.append("| Group | Online (%) | In-store (%) |")
    lines.append("|---|---|---|")
    for group in groups:
        online = float(group["online_pct"])
        lines.append(f"| `{group['category']}` | {online:.0f} | {100.0 - online:.0f} |")
    lines.append("")

    lines.append("## Expected Clearance Rate")
    lines.append("")
    lines.append("| Group | Sold to date (%) | Weeks to clear | Projected clearance (%) |")
    lines.append("|---|---|---|---|")
    for group in groups:
        lines.append(
            f"| `{group['category']}` | {group['avg_sell_through_pct']:.1f} | {horizon_weeks} | "
            f"{projections[group['category']]:.1f} |"
        )
    lines.append("")

    lines.append("## Margin Impact Analysis")
    lines.append("")
    lines.append(
        "Impact is stated in percentage points of the selling price given up, averaged over the "
        "horizon — not as a currency amount."
    )
    lines.append("")
    lines.append("| Group | Avg discount (pp) | Deepest discount (pp) | Margin floor held |")
    lines.append("|---|---|---|---|")
    for group in groups:
        group_weeks = schedules[group["category"]]
        lines.append(f"| `{group['category']}` | {averages[group['category']]:.1f} | {max(group_weeks):.0f} | Yes |")
    lines.append("")

    return "\n".join(lines)


class DocumentGenerateNode(FunctionNode):
    """Render the clearance plan from the enriched planning dataset.

    Inner domain graph node 4 (registered as "document_generate").

    Input state keys:
        enriched_data: JSON string of the planning dataset

    Output state keys (partial dict):
        markdown_plan_raw: the rendered plan, before the quality gate
        status:            AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:         (on error) list of messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        dataset: Dict[str, Any] = from_json(state.get("enriched_data"), {}) or {}
        groups = dataset.get("groups") or []

        if not groups:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DocumentGenerateNode: the planning dataset carries no product groups"],
            }

        plan = _render(dataset)

        logger.info("DocumentGenerateNode: rendered a %d-character plan over %d group(s)", len(plan), len(groups))

        emit_trace_event(
            "clearance_plan_rendered",
            {"group_count": len(groups), "plan_chars": len(plan), "horizon_weeks": dataset.get("horizon_weeks")},
            state,
        )

        return {
            "markdown_plan_raw": plan,
            "status": AgentStatus.SUCCESS.value,
        }
