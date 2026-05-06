"""Inspect Manager sub-graph — Jira first, then PR analysis in parallel with errata.

Jira must run first because the PR Analyzer needs the PR URLs from
Jira's remote links. Errata runs in parallel with PR analysis since
it's independent.

Topology::

    START ── jira_inspector ──┬── git_diff (PR Analyzer) ──┐
                              └── errata_parser ───────────┘
                                                           └── merge_manifest ── END
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from core.state import InspectState
from nodes.jira_inspector import jira_inspector
from nodes.errata_parser import errata_parser
from nodes.git_diff import git_diff
from nodes.merge_manifest import merge_manifest


def build_inspect_graph() -> StateGraph:
    """Build and compile the Inspect Manager sub-graph."""
    graph = StateGraph(InspectState)

    graph.add_node("jira_inspector", jira_inspector)
    graph.add_node("errata_parser", errata_parser)
    graph.add_node("git_diff", git_diff)
    graph.add_node("merge_manifest", merge_manifest)

    # Jira runs first (needs to fetch PR URLs from remote links)
    graph.add_edge(START, "jira_inspector")

    # After Jira: PR Analyzer and Errata run in parallel
    graph.add_edge("jira_inspector", "git_diff")
    graph.add_edge("jira_inspector", "errata_parser")

    # Both fan into merge
    graph.add_edge("git_diff", "merge_manifest")
    graph.add_edge("errata_parser", "merge_manifest")

    graph.add_edge("merge_manifest", END)

    return graph.compile()
