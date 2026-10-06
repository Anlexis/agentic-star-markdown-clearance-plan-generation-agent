# Unit tests for the planning nodes.
#
# Every node is called through execute(state) with ONE argument, which is how
# the graph runtime calls it. A node that read settings from a second `config`
# argument would pass a test that supplied one and then silently fall back to
# module defaults on every real invocation; these tests cannot be written that
# way, so that failure cannot hide here.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.data_enrich_node import DataEnrichNode
from src.nodes.document_generate_node import (
    REQUIRED_SECTIONS,
    DocumentGenerateNode,
    projected_clearance_pct,
    weekly_discounts,
)
from src.nodes.input_parse_node import InputParseNode
from src.nodes.quality_check_node import (
    QualityCheckNode,
    discount_breaches,
    empty_sections,
    missing_sections,
    monetary_amounts,
)
from src.nodes.template_select_node import TemplateSelectNode

PLAN_CONFIG = {
    "max_discount_pct": 70.0,
    "horizon_weeks": 4,
    "clearance_uplift_factor": 0.8,
    "margin_rules": {
        "default": {"max_discount_pct": 50.0, "margin_floor_pct": 20.0},
        "apparel": {"max_discount_pct": 70.0, "margin_floor_pct": 15.0},
        "electronics": {"max_discount_pct": 40.0, "margin_floor_pct": 25.0},
    },
    "channel_split": {"default": {"online_pct": 50.0}, "apparel": {"online_pct": 60.0}},
}

CONTRACT = {
    "skus": [
        {
            "sku_id": "a001",
            "category": "apparel",
            "days_on_shelf": 150,
            "sell_through_pct": 30.0,
            "current_price": 49.99,
            "margin_floor_pct": 20.0,
            "note": "",
        },
        {
            "sku_id": "e001",
            "category": "electronics",
            "days_on_shelf": 40,
            "sell_through_pct": 70.0,
            "current_price": 199.99,
            "margin_floor_pct": 25.0,
            "note": "",
        },
    ]
}


def run_pipeline(contract=None, plan_config=None):
    """Drive the five inner nodes in graph order over one accumulating state."""
    state = {
        "caller_contract": json.dumps(contract if contract is not None else CONTRACT),
        "plan_config": json.dumps(plan_config if plan_config is not None else PLAN_CONFIG),
    }
    for node in (InputParseNode(), TemplateSelectNode(), DataEnrichNode(), DocumentGenerateNode(), QualityCheckNode()):
        state.update(node.execute(state))
        if state.get("status") == AgentStatus.ERROR.value:
            break
    return state


class TestOrdering:
    def test_records_are_ordered_by_markdown_urgency(self):
        state = InputParseNode().execute({"caller_contract": json.dumps(CONTRACT)})
        skus = json.loads(state["parsed_skus"])
        assert [sku["sku_id"] for sku in skus] == ["a001", "e001"]
        assert skus[0]["markdown_priority"] > skus[1]["markdown_priority"]

    def test_an_empty_contract_is_an_error_not_an_empty_plan(self):
        state = InputParseNode().execute({"caller_contract": json.dumps({"skus": []})})
        assert state["status"] == AgentStatus.ERROR.value


class TestProfileSelection:
    def test_dominant_category_with_a_rule_becomes_the_profile(self):
        state = run_pipeline()
        assert state["selected_profile"] in {"apparel", "electronics"}

    def test_a_category_with_no_rule_falls_back_to_default(self):
        contract = {"skus": [dict(CONTRACT["skus"][0], category="garden_furniture")]}
        state = {"caller_contract": json.dumps(contract), "plan_config": json.dumps(PLAN_CONFIG)}
        state.update(InputParseNode().execute(state))
        state.update(TemplateSelectNode().execute(state))
        assert state["selected_profile"] == "default"


class TestCeilings:
    def test_the_tightest_of_the_four_limits_wins(self):
        state = run_pipeline()
        dataset = json.loads(state["enriched_data"])
        by_id = {sku["sku_id"]: sku for sku in dataset["skus"]}
        # apparel rule 70, margin floor 20 -> 80 -> global 70 wins
        assert by_id["a001"]["effective_max_discount_pct"] == 70.0
        # electronics rule 40 is tighter than both the global cap and the floor
        assert by_id["e001"]["effective_max_discount_pct"] == 40.0

    def test_a_product_margin_floor_overrides_a_looser_category_rule(self):
        contract = {"skus": [dict(CONTRACT["skus"][0], margin_floor_pct=85.0)]}
        state = run_pipeline(contract=contract)
        dataset = json.loads(state["enriched_data"])
        assert dataset["skus"][0]["effective_max_discount_pct"] == 15.0

    def test_a_caller_may_tighten_the_ceiling_but_not_widen_it(self):
        tighter = run_pipeline(contract=dict(CONTRACT, max_discount_pct=10.0))
        wider = run_pipeline(contract=dict(CONTRACT, max_discount_pct=99.0))
        assert json.loads(tighter["enriched_data"])["max_discount_pct"] == 10.0
        assert json.loads(wider["enriched_data"])["max_discount_pct"] == 70.0

    def test_a_group_is_planned_to_its_tightest_member(self):
        contract = {
            "skus": [
                dict(CONTRACT["skus"][0], sku_id="a001", margin_floor_pct=10.0),
                dict(CONTRACT["skus"][0], sku_id="a002", margin_floor_pct=60.0),
            ]
        }
        state = run_pipeline(contract=contract)
        groups = json.loads(state["enriched_data"])["groups"]
        assert groups[0]["ceiling_pct"] == 40.0


class TestSchedule:
    def test_the_ramp_is_monotonic_and_lands_on_the_ceiling(self):
        weeks = weekly_discounts(70.0, 4)
        assert weeks == sorted(weeks)
        assert weeks[-1] == 70.0

    def test_a_single_week_horizon_offers_the_ceiling_once(self):
        assert weekly_discounts(35.0, 1) == [35.0]

    def test_no_uplift_reports_todays_sell_through_unchanged(self):
        assert projected_clearance_pct(40.0, 30.0, 0.0) == 40.0

    def test_projection_is_capped_at_the_whole(self):
        assert projected_clearance_pct(90.0, 100.0, 5.0) == 100.0


class TestRenderedPlan:
    def test_the_plan_is_computed_from_caller_data_not_a_placeholder(self):
        one = run_pipeline()["markdown_plan"]
        other = run_pipeline(contract={"skus": [dict(CONTRACT["skus"][0], sku_id="z999", category="apparel")]})[
            "markdown_plan"
        ]
        assert one != other
        assert "z999" in other and "z999" not in one

    def test_every_promised_section_is_present_and_populated(self):
        plan = run_pipeline()["markdown_plan"]
        assert missing_sections(plan) == []
        assert empty_sections(plan) == []
        for heading in REQUIRED_SECTIONS:
            assert heading in plan

    def test_the_plan_renders_no_monetary_amount(self):
        assert monetary_amounts(run_pipeline()["markdown_plan"]) is False

    def test_a_long_group_lists_a_bounded_number_of_codes(self):
        contract = {"skus": [dict(CONTRACT["skus"][0], sku_id=f"a{index:03d}") for index in range(20)]}
        plan = run_pipeline(contract=contract)["markdown_plan"]
        assert "and 12 more" in plan

    def test_identical_requests_render_identical_plans(self):
        assert run_pipeline()["markdown_plan"] == run_pipeline()["markdown_plan"]


class TestQualityGate:
    def test_a_missing_section_is_rejected(self):
        state = run_pipeline()
        state["markdown_plan_raw"] = state["markdown_plan"].replace("## Channel Allocation", "## Channels")
        result = QualityCheckNode().execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "markdown_plan" in result and result["markdown_plan"] is None

    def test_a_present_but_empty_section_is_rejected(self):
        plan = "\n".join(f"{heading}\n" for heading in REQUIRED_SECTIONS)
        assert set(empty_sections(plan)) == set(REQUIRED_SECTIONS)

    def test_a_discount_over_the_group_ceiling_is_rejected(self):
        state = run_pipeline()
        breached = state["markdown_plan"].replace("| `electronics` | 1 | 40 |", "| `electronics` | 1 | 95 |")
        state["markdown_plan_raw"] = breached
        result = QualityCheckNode().execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "electronics" in result["error_log"][0]

    def test_a_rendered_monetary_amount_is_rejected(self):
        state = run_pipeline()
        state["markdown_plan_raw"] = state["markdown_plan"] + "\nTotal markdown: JPY 1234\n"
        result = QualityCheckNode().execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "monetary" in result["error_log"][-1]

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Amounts, in every marker form the renderer could produce.
            ("JPY 1234", True),
            ("1234 JPY", True),
            ("$49.99", True),
            ("49.99 EUR", True),
            ("\uffe5 3000", True),
            ("\u00a53000", True),
            ("JPY-9999", True),
            ("JPY\t9999", True),
            # The identifier-collision direction. Product codes and categories
            # render over [a-z0-9_], and a code may legitimately contain a
            # currency code as a word part. None of these is an amount.
            ("`sku_usd_12`", False),
            ("`usd_1234`", False),
            ("`jpy_9999`", False),
            ("`sku_48210`", False),
            ("`48210`", False),
            ("`a001`", False),
            ("`4901234567894`", False),
            ("70%", False),
            ("Week 4", False),
            ("Horizon: 4 week(s)", False),
        ],
    )
    def test_the_monetary_scan_reads_amounts_not_product_codes(self, text, expected):
        assert monetary_amounts(text) is expected

    def test_a_plan_whose_codes_are_named_after_currencies_still_renders(self):
        contract = {
            "skus": [
                dict(CONTRACT["skus"][0], sku_id="usd_0001", category="usd"),
                dict(CONTRACT["skus"][0], sku_id="jpy_0002", category="jpy_lines"),
            ]
        }
        state = run_pipeline(contract=contract)
        assert state["status"] == AgentStatus.SUCCESS.value
        assert "usd_0001" in state["markdown_plan"]

    def test_the_breach_scan_ignores_columns_that_are_not_discounts(self):
        # The projected clearance rate legitimately exceeds the discount ceiling.
        plan = run_pipeline()["markdown_plan"]
        assert discount_breaches(plan, {"apparel": 70.0, "electronics": 40.0}, 70.0) == []
