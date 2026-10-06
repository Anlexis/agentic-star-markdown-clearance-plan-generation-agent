# The wiring itself: the backbone slots, the nested boundary, and the trust
# level every node declares.

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.function_node import FunctionNode
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import Graph, MarkdownPlanGraphNode, RetC2027Agent
from src.schemas.state import State


class TestOuterGraph:
    def test_the_agent_inherits_the_framework_base_directly(self):
        assert issubclass(RetC2027Agent, AgentBaseGraph)

    def test_the_backbone_slots_are_all_filled(self):
        agent = RetC2027Agent()
        agent.register_nodes()
        assert set(agent._nodes) == {"initialize", "pre_process", "main", "post_process", "finalize"}
        assert isinstance(agent._nodes["main"], MarkdownPlanGraphNode)

    def test_the_manifest_alias_resolves_to_the_agent(self):
        assert Graph is RetC2027Agent

    def test_the_state_schema_is_the_shared_one(self):
        assert RetC2027Agent().state_schema is State


class TestNestedBoundary:
    def test_the_main_slot_is_a_subgraph_node(self):
        assert issubclass(MarkdownPlanGraphNode, GraphNode)

    def test_the_inner_graph_registers_the_five_domain_nodes(self):
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert list(inner._nodes) == [
            "input_parse",
            "template_select",
            "data_enrich",
            "document_generate",
            "quality_check",
        ]

    def test_merge_output_returns_only_changed_keys(self):
        delta = MarkdownPlanGraphNode().merge_output({}, {"markdown_plan": "x", "status": "success"})
        # The boundary now also carries the declined-completion marker; on a run
        # that produced a plan neither side set one, so it crosses empty.
        assert set(delta) == {"markdown_plan", "result", "clearance_metadata", "status", "error_code"}
        assert delta["error_code"] == ""
        assert delta["result"] == "x"

    def test_the_inner_graph_withholds_a_plan_that_did_not_pass(self):
        output = DomainWorkflowGraph().get_output({"markdown_plan": "ungated text", "status": AgentStatus.ERROR.value})
        assert output["markdown_plan"] is None

    def test_the_route_callable_is_annotated_with_this_graphs_state(self):
        # The runtime reads the annotation as the callable's input schema and
        # projects away every field it does not carry, so an annotation naming
        # the framework base would hand the callable a state with the domain
        # fields missing.
        assert DomainWorkflowGraph.route.__annotations__["state"] is State


class TestTrustLevels:
    def test_every_node_declares_a_trust_level_reachable_from_the_public_entry(self):
        import importlib
        import inspect
        import pkgutil

        import src.nodes as nodes_pkg

        declared = {}
        for _, name, _ in pkgutil.walk_packages(nodes_pkg.__path__, prefix="src.nodes."):
            module = importlib.import_module(name)
            for attribute in vars(module).values():
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, FunctionNode)
                    and attribute is not FunctionNode
                    and not inspect.isabstract(attribute)
                ):
                    declared[attribute.__name__] = attribute.required_trust_level

        assert declared, "no domain nodes were discovered"
        # The manifest advertises VERIFIED_EXTERNAL. A node demanding more than
        # the advertised level is unreachable through the public entry point:
        # every real invocation would stop at its trust gate.
        assert all(level is TrustLevel.VERIFIED_EXTERNAL for level in declared.values()), declared
