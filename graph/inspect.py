"""Inspect Manager sub-graph — fan-out/fan-in to gather change data.

Three parallel data-gathering nodes (Jira, Errata, Git) feed into a
single merge node that produces the unified ChangeManifest.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from core.state import InspectState
from nodes.jira_inspector import jira_inspector
from nodes.errata_parser import errata_parser
from nodes.git_diff import git_diff
from nodes.merge_manifest import merge_manifest


def build_inspect_graph() -> StateGraph:
    """Build and compile the Inspect Manager sub-graph.

    Topology::

        START ──┬── jira_inspector ──┐
                ├── errata_parser ──┤
                └── git_diff ───────┘
                                    └── merge_manifest ── END
    """
    graph = StateGraph(InspectState)

    # Add nodes
    graph.add_node("jira_inspector", jira_inspector)
    graph.add_node("errata_parser", errata_parser)
    graph.add_node("git_diff", git_diff)
    graph.add_node("merge_manifest", merge_manifest)

    # Fan-out: START → three parallel nodes
    graph.add_edge(START, "jira_inspector")
    graph.add_edge(START, "errata_parser")
    graph.add_edge(START, "git_diff")

    # Fan-in: all three → merge_manifest
    graph.add_edge("jira_inspector", "merge_manifest")
    graph.add_edge("errata_parser", "merge_manifest")
    graph.add_edge("git_diff", "merge_manifest")

    # Terminal
    graph.add_edge("merge_manifest", END)

    return graph.compile()
