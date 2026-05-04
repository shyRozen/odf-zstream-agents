"""Analyze Manager sub-graph — DAG for failure analysis.

Classifier runs first, then root_cause and regression run in parallel,
and finally report_generator produces the consolidated analysis report.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from core.state import AnalyzeState
from nodes.classifier import classifier
from nodes.root_cause import root_cause
from nodes.regression import regression
from nodes.report_generator import report_generator


def build_analyze_graph() -> StateGraph:
    """Build and compile the Analyze Manager sub-graph.

    Topology::

        START → classifier ──┬── root_cause ──────┐
                              └── regression ──────┘
                                                   └── report_generator → END
    """
    graph = StateGraph(AnalyzeState)

    # Add nodes
    graph.add_node("classifier", classifier)
    graph.add_node("root_cause", root_cause)
    graph.add_node("regression", regression)
    graph.add_node("report_generator", report_generator)

    # Sequential entry
    graph.add_edge(START, "classifier")

    # Fan-out from classifier
    graph.add_edge("classifier", "root_cause")
    graph.add_edge("classifier", "regression")

    # Fan-in to report generator
    graph.add_edge("root_cause", "report_generator")
    graph.add_edge("regression", "report_generator")

    # Terminal
    graph.add_edge("report_generator", END)

    return graph.compile()
