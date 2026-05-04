"""Map Tests Manager sub-graph — sequential pipeline with conditional retry.

Runs code_analyzer -> mark_matcher -> coverage_validator, then retries from
the top if coverage gaps remain and we haven't exceeded the attempt limit.
"""
from __future__ import annotations

from typing import Literal

from langgraph.graph import StateGraph, START, END

from core.state import MapState
from nodes.code_analyzer import code_analyzer
from nodes.mark_matcher import mark_matcher
from nodes.coverage_validator import coverage_validator

MAX_RETRIES = 2


def _should_retry(state: MapState) -> Literal["retry", "done"]:
    """Decide whether to retry the mapping loop.

    Retries when:
    - The coverage report has gaps (gaps > 0), AND
    - We haven't exceeded the maximum attempt count.
    """
    report = state.get("coverage_report")
    attempt = state.get("attempt_count", 0)

    if report is not None and report.gaps > 0 and attempt < MAX_RETRIES:
        return "retry"
    return "done"


def build_map_tests_graph() -> StateGraph:
    """Build and compile the Map Tests Manager sub-graph.

    Topology::

        START → code_analyzer → mark_matcher → coverage_validator
                    ↑                                  │
                    └──── retry (if gaps & attempts<2) ┘
                                                       │
                                                  done → END
    """
    graph = StateGraph(MapState)

    # Add nodes
    graph.add_node("code_analyzer", code_analyzer)
    graph.add_node("mark_matcher", mark_matcher)
    graph.add_node("coverage_validator", coverage_validator)

    # Linear chain
    graph.add_edge(START, "code_analyzer")
    graph.add_edge("code_analyzer", "mark_matcher")
    graph.add_edge("mark_matcher", "coverage_validator")

    # Conditional retry edge
    graph.add_conditional_edges(
        "coverage_validator",
        _should_retry,
        {
            "retry": "code_analyzer",
            "done": END,
        },
    )

    return graph.compile()
