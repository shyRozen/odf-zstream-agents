"""Top-level pipeline graph — orchestrates the full z-stream workflow.

Wires together the three sub-graphs (inspect, map_tests, analyze) with
the standalone nodes (pr_builder, jenkins, notifier) into a linear
pipeline:

    START -> inspect -> map_tests -> pr_builder -> jenkins -> analyze -> notify -> END
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from core.state import PipelineState, InspectState, MapState, AnalyzeState
from graph.inspect import build_inspect_graph
from graph.map_tests import build_map_tests_graph
from graph.analyze import build_analyze_graph
from nodes.pr_builder import pr_builder
from nodes.jenkins_agent import jenkins_agent
from nodes.notifier import notifier

# ---------------------------------------------------------------------------
# Sub-graph wrapper functions
# ---------------------------------------------------------------------------
# Each wrapper transforms PipelineState into the sub-graph's narrower state,
# invokes the compiled sub-graph, and maps the results back to PipelineState
# keys.
# ---------------------------------------------------------------------------

_inspect_graph = None
_map_tests_graph = None
_analyze_graph = None


def _get_inspect_graph():
    global _inspect_graph
    if _inspect_graph is None:
        _inspect_graph = build_inspect_graph()
    return _inspect_graph


def _get_map_tests_graph():
    global _map_tests_graph
    if _map_tests_graph is None:
        _map_tests_graph = build_map_tests_graph()
    return _map_tests_graph


def _get_analyze_graph():
    global _analyze_graph
    if _analyze_graph is None:
        _analyze_graph = build_analyze_graph()
    return _analyze_graph


def inspect_wrapper(state: PipelineState) -> dict:
    """Run the Inspect sub-graph and extract the change_manifest."""
    sub_state: InspectState = {
        "zstream_version": state.get("zstream_version", ""),
        "previous_version": state.get("previous_version", ""),
    }
    result = _get_inspect_graph().invoke(sub_state)
    output: dict = {"current_stage": "inspect"}
    if "change_manifest" in result:
        output["change_manifest"] = result["change_manifest"]
    if result.get("errors"):
        output["errors"] = result["errors"]
    return output


def map_tests_wrapper(state: PipelineState) -> dict:
    """Run the Map Tests sub-graph and extract test selections + coverage."""
    from core.test_map import get_map_summary

    sub_state: MapState = {}
    if "change_manifest" in state:
        sub_state["change_manifest"] = state["change_manifest"]
    sub_state["test_map_context"] = get_map_summary()
    sub_state["version"] = state.get("zstream_version", "")
    result = _get_map_tests_graph().invoke(sub_state)
    output: dict = {"current_stage": "map_tests"}
    if result.get("selected_tests"):
        output["selected_tests"] = result["selected_tests"]
    if "coverage_report" in result:
        output["coverage_report"] = result["coverage_report"]
    if result.get("errors"):
        output["errors"] = result["errors"]
    return output


def analyze_wrapper(state: PipelineState) -> dict:
    """Run the Analyze sub-graph and extract the analysis report."""
    sub_state: AnalyzeState = {}
    if "junit_results" in state:
        sub_state["junit_results"] = state["junit_results"]
    if "change_manifest" in state:
        sub_state["change_manifest"] = state["change_manifest"]
    result = _get_analyze_graph().invoke(sub_state)
    output: dict = {"current_stage": "analyze"}
    if "analysis_report" in result:
        output["analysis_report"] = result["analysis_report"]
    if result.get("errors"):
        output["errors"] = result["errors"]
    return output


# ---------------------------------------------------------------------------
# Thin wrappers for standalone nodes that set current_stage
# ---------------------------------------------------------------------------


def pr_builder_node(state: PipelineState) -> dict:
    result = pr_builder(state)
    result["current_stage"] = "pr_builder"
    return result


def jenkins_node(state: PipelineState) -> dict:
    result = jenkins_agent(state)
    result["current_stage"] = "jenkins"
    return result


def notify_node(state: PipelineState) -> dict:
    result = notifier(state)
    result["current_stage"] = "notify"
    return result


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------


def build_pipeline(collect_only: bool = False, stop_after_pr: bool = False):
    """Build and compile the z-stream pipeline.

    Args:
        collect_only: Stop after inspect + map_tests (stages 1-2).
        stop_after_pr: Stop after PR is created (stages 1-3).

    Returns the compiled LangGraph runnable.
    """
    graph = StateGraph(PipelineState)

    graph.add_node("inspect", inspect_wrapper)
    graph.add_node("map_tests", map_tests_wrapper)

    graph.add_edge(START, "inspect")
    graph.add_edge("inspect", "map_tests")

    if collect_only:
        graph.add_edge("map_tests", END)
    elif stop_after_pr:
        graph.add_node("pr_builder", pr_builder_node)
        graph.add_edge("map_tests", "pr_builder")
        graph.add_edge("pr_builder", END)
    else:
        graph.add_node("analyze", analyze_wrapper)
        graph.add_node("pr_builder", pr_builder_node)
        graph.add_node("jenkins", jenkins_node)
        graph.add_node("notify", notify_node)

        graph.add_edge("map_tests", "pr_builder")
        graph.add_edge("pr_builder", "jenkins")
        graph.add_edge("jenkins", "analyze")
        graph.add_edge("analyze", "notify")
        graph.add_edge("notify", END)

    return graph.compile()
