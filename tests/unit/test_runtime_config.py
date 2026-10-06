# The declared runtime configuration must REACH the graph and change what it
# does. A value that is declared, loaded, and then ignored leaves the suite
# green and the deployment running on module defaults.

import json

from src.graph.graph import MarkdownPlanGraphNode, runtime_config


class TestRuntimeConfig:
    def test_the_config_file_is_the_source_of_the_runtime_parameters(self):
        config = runtime_config()
        assert config["max_retry"] == 3
        assert config["timeout_s"] == 30
        assert isinstance(config["plan"], dict)

    def test_the_forwarded_settings_are_never_empty(self):
        forwarded = MarkdownPlanGraphNode()._parent_config()["configurable"]["plan"]
        assert forwarded["max_discount_pct"] > 0
        assert forwarded["horizon_weeks"] >= 1
        assert forwarded["margin_rules"]
        assert forwarded["channel_split"]

    def test_the_declared_ceiling_is_the_forwarded_ceiling(self):
        declared = runtime_config()["plan"]["max_discount_pct"]
        forwarded = MarkdownPlanGraphNode()._parent_config()["configurable"]["plan"]["max_discount_pct"]
        assert forwarded == float(declared)

    def test_the_inner_graph_seeds_the_forwarded_settings_into_its_state(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        node = MarkdownPlanGraphNode()
        inner = DomainWorkflowGraph(config=node._parent_config())
        seeded = inner._extra_initial_state()
        assert json.loads(seeded["plan_config"])["max_discount_pct"] == runtime_config()["plan"]["max_discount_pct"]

    def test_a_malformed_category_rule_is_dropped_not_fatal(self):
        node = MarkdownPlanGraphNode()
        forwarded = node._parent_config()["configurable"]["plan"]
        assert "default" in forwarded["margin_rules"]
