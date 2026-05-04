"""PR Builder node — creates a GitHub PR with the selected tests.

Uses Sonnet to generate a descriptive PR body. Calls GitHub tools to
create a branch, add pytest marks, and open the PR.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from core.models import ChangeManifest, StageError, TestSelection
from core.state import PipelineState

logger = logging.getLogger(__name__)

PR_DESCRIPTION_PROMPT = """\
You are writing a GitHub PR description for a z-stream test enablement PR.

Given the z-stream version, change manifest, and list of selected tests,
generate a clear PR description in Markdown format.

Include:
1. A summary of what this PR does (enables tests for z-stream validation)
2. The z-stream version being tested
3. A table or list of the changes being validated
4. The number of tests being enabled
5. Coverage statistics

Output ONLY the Markdown PR body, no code fences.
"""


def pr_builder(state: PipelineState) -> dict:
    """Create a GitHub PR that enables the selected tests for the z-stream run.

    Returns a dict with ``pr_url`` and ``pr_number``.
    """
    selected_tests: list[TestSelection] = state.get("selected_tests") or []
    manifest: ChangeManifest | None = state.get("change_manifest")
    version = state.get("zstream_version", "unknown")

    if not selected_tests:
        logger.warning("No tests selected, skipping PR creation")
        return {"pr_url": "", "pr_number": 0}

    try:
        from tools.github_tools import (
            github_create_branch,
            github_add_mark_to_test,
            github_create_pr,
        )
    except ImportError:
        logger.warning("github_tools not available, skipping PR creation")
        return {
            "pr_url": "",
            "pr_number": 0,
            "errors": [
                StageError(
                    stage="pr_builder",
                    error="github_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    mark_name = f"zstream_{version.replace('.', '_').replace('-', '_')}"
    branch_name = f"zstream/{version}/test-enablement"

    try:
        # Step 1: Create a new branch
        logger.info("Creating branch: %s", branch_name)
        try:
            github_create_branch(branch_name)
        except Exception as e:
            logger.warning("Branch creation failed (may already exist): %s", e)

        # Step 2: Add pytest marks to each selected test
        marked_count = 0
        mark_errors = []
        for test in selected_tests:
            try:
                github_add_mark_to_test(
                    branch=branch_name,
                    file_path=test.file_path,
                    test_node_id=test.test_node_id,
                    mark_name=mark_name,
                )
                marked_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to add mark to %s: %s", test.test_node_id, e
                )
                mark_errors.append(f"{test.test_node_id}: {e}")

        if marked_count == 0:
            logger.error("Failed to mark any tests, aborting PR creation")
            return {
                "pr_url": "",
                "pr_number": 0,
                "errors": [
                    StageError(
                        stage="pr_builder",
                        error=f"Failed to mark any tests. Errors: {mark_errors[:5]}",
                        timestamp=datetime.utcnow().isoformat(),
                        recoverable=True,
                    )
                ],
            }

        # Step 3: Generate PR description
        pr_body = _generate_pr_description(manifest, selected_tests, version, marked_count)

        # Step 4: Create the PR
        pr_title = f"[z-stream {version}] Enable {marked_count} tests for validation"
        logger.info("Creating PR: %s", pr_title)

        pr_result = github_create_pr(
            branch=branch_name,
            title=pr_title,
            body=pr_body,
        )

        pr_url = ""
        pr_number = 0
        if isinstance(pr_result, dict):
            pr_url = pr_result.get("url", pr_result.get("html_url", ""))
            pr_number = pr_result.get("number", 0)
        elif isinstance(pr_result, str):
            pr_url = pr_result
        elif isinstance(pr_result, int):
            pr_number = pr_result

        logger.info("PR created: %s (#%d)", pr_url, pr_number)

        result = {"pr_url": pr_url, "pr_number": pr_number}
        if mark_errors:
            result["errors"] = [
                StageError(
                    stage="pr_builder",
                    error=f"{len(mark_errors)} tests failed to mark: {mark_errors[:3]}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ]
        return result

    except Exception as e:
        logger.error("PR creation failed: %s", e)
        return {
            "pr_url": "",
            "pr_number": 0,
            "errors": [
                StageError(
                    stage="pr_builder",
                    error=f"PR creation failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _generate_pr_description(
    manifest: ChangeManifest | None,
    tests: list[TestSelection],
    version: str,
    marked_count: int,
) -> str:
    """Generate a PR description using LLM or fallback template."""
    llm = get_llm("pr_builder")
    if llm is not None:
        try:
            return _generate_with_llm(llm, manifest, tests, version, marked_count)
        except Exception as e:
            logger.warning("LLM PR description failed: %s, using template", e)

    return _generate_template(manifest, tests, version, marked_count)


def _generate_with_llm(
    llm,
    manifest: ChangeManifest | None,
    tests: list[TestSelection],
    version: str,
    marked_count: int,
) -> str:
    """Generate PR description using LLM."""
    changes_summary = "No change manifest available."
    if manifest and manifest.changes:
        changes_data = [
            {"id": c.id, "component": c.component, "type": c.type.value, "summary": c.summary}
            for c in manifest.changes
        ]
        changes_summary = json.dumps(changes_data, indent=2)

    test_summary = [
        {"test": t.test_node_id, "score": t.relevance_score, "reason": t.reason}
        for t in tests[:20]  # Limit for prompt size
    ]

    prompt = (
        f"Generate a PR description for z-stream {version}.\n\n"
        f"Changes:\n{changes_summary}\n\n"
        f"Tests marked: {marked_count}\n"
        f"Top tests:\n{json.dumps(test_summary, indent=2)}"
    )

    response = llm.invoke([
        SystemMessage(content=PR_DESCRIPTION_PROMPT),
        HumanMessage(content=prompt),
    ])

    return response.content.strip()


def _generate_template(
    manifest: ChangeManifest | None,
    tests: list[TestSelection],
    version: str,
    marked_count: int,
) -> str:
    """Fallback template-based PR description."""
    lines = [
        f"## Z-Stream Test Enablement: {version}",
        "",
        f"This PR enables **{marked_count}** tests for z-stream version "
        f"**{version}** validation.",
        "",
    ]

    if manifest and manifest.changes:
        lines.append("### Changes Being Validated")
        lines.append("")
        lines.append("| ID | Component | Type | Severity | Summary |")
        lines.append("|---|---|---|---|---|")
        for c in manifest.changes:
            lines.append(
                f"| {c.id} | {c.component} | {c.type.value} | "
                f"{c.severity.value} | {c.summary} |"
            )
        lines.append("")

    lines.append("### Test Selection Summary")
    lines.append("")
    lines.append(f"- **Total tests selected:** {len(tests)}")
    lines.append(f"- **Tests marked:** {marked_count}")
    if tests:
        avg_score = sum(t.relevance_score for t in tests) / len(tests)
        lines.append(f"- **Average relevance score:** {avg_score:.2f}")
    lines.append("")

    # Show top 10 tests
    lines.append("### Top Tests by Relevance")
    lines.append("")
    for t in tests[:10]:
        lines.append(f"- `{t.test_node_id}` (score: {t.relevance_score:.2f}) — {t.reason}")
    if len(tests) > 10:
        lines.append(f"- ... and {len(tests) - 10} more")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by ODF Z-Stream Pipeline*")

    return "\n".join(lines)
