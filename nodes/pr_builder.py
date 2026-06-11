"""PR Builder node -- creates a GitHub PR with the selected tests.

Uses the unified agent runner (run_node, not JSON) to generate a descriptive
PR body.  Keeps direct calls to github_tools functions for branch/mark/PR
creation.  Falls back to a template-based PR description.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.agent_runner import run_node
from core.models import ChangeManifest, StageError, TestSelection
from core.state import PipelineState

logger = logging.getLogger(__name__)


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
            github_add_marks_to_test,
            github_create_pr,
            github_register_marker,
            github_register_mark_in_marks_py,
        )
        from core.models import component_marker_name
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
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    branch_name = f"zstream/{version}/test-enablement-{timestamp}"
    parts = version.split(".")
    base_branch = f"release-{parts[0]}.{parts[1]}" if len(parts) >= 2 else "master"

    try:
        # Step 1: Create a new branch from the release branch
        logger.info("Creating branch: %s (from %s)", branch_name, base_branch)
        try:
            github_create_branch(branch_name, base_branch=base_branch)
        except Exception as e:
            logger.warning("Branch creation failed (may already exist): %s", e)

        # Step 2: Build per-component marker names
        comp_markers: dict[str, str] = {}
        for test in selected_tests:
            if test.component and test.component not in comp_markers:
                comp_markers[test.component] = component_marker_name(mark_name, test.component)

        # Step 3: Register all markers (global + per-component) in pytest.ini and marks.py
        all_markers = {mark_name: f"z-stream {version} test enablement"}
        for comp, comp_mark in comp_markers.items():
            all_markers[comp_mark] = f"z-stream {version} {comp} component tests"

        for m_name, m_desc in all_markers.items():
            logger.info("Registering marker: %s", m_name)
            try:
                github_register_marker(
                    branch=branch_name,
                    mark_name=m_name,
                    description=m_desc,
                )
            except Exception as e:
                logger.warning("Failed to register marker %s in pytest.ini: %s", m_name, e)
            try:
                github_register_mark_in_marks_py(
                    branch=branch_name,
                    mark_name=m_name,
                )
            except Exception as e:
                logger.warning("Failed to register marker %s in marks.py: %s", m_name, e)

        # Step 4: Add marks to each selected test (grouped by file)
        from collections import defaultdict

        file_marks: dict[str, set[str]] = defaultdict(set)
        for test in selected_tests:
            file_marks[test.file_path].add(mark_name)
            if test.component and test.component in comp_markers:
                file_marks[test.file_path].add(comp_markers[test.component])

        marked_count = 0
        mark_errors = []
        for file_path, marks in file_marks.items():
            try:
                github_add_marks_to_test(
                    branch=branch_name,
                    file_path=file_path,
                    mark_names=sorted(marks),
                )
                marked_count += 1
            except Exception as e:
                logger.warning("Failed to add marks to %s: %s", file_path, e)
                mark_errors.append(f"{file_path}: {e}")

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
            base_branch=base_branch,
        )

        pr_url = ""
        pr_number = 0
        if isinstance(pr_result, str):
            try:
                pr_result = json.loads(pr_result)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(pr_result, dict):
            pr_url = pr_result.get("url", pr_result.get("html_url", ""))
            pr_number = pr_result.get("pr_number", pr_result.get("number", 0))

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


# ------------------------------------------------------------------
# PR description generation
# ------------------------------------------------------------------


def _generate_pr_description(
    manifest: ChangeManifest | None,
    tests: list[TestSelection],
    version: str,
    marked_count: int,
) -> str:
    """Generate a PR description using agent or fallback template."""
    try:
        return _generate_with_agent(manifest, tests, version, marked_count)
    except Exception as e:
        logger.warning("Agent PR description failed: %s, using template", e)

    return _generate_template(manifest, tests, version, marked_count)


def _generate_with_agent(
    manifest: ChangeManifest | None,
    tests: list[TestSelection],
    version: str,
    marked_count: int,
) -> str:
    """Generate PR description using the agent runner."""
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
        f"You are writing a GitHub PR description for a z-stream test "
        f"enablement PR.\n\n"
        f"Generate a clear PR description in Markdown format for z-stream "
        f"{version}.\n\n"
        f"Include:\n"
        f"1. A summary of what this PR does (enables tests for z-stream "
        f"   validation)\n"
        f"2. The z-stream version being tested\n"
        f"3. A table or list of the changes being validated\n"
        f"4. The number of tests being enabled\n"
        f"5. Coverage statistics\n\n"
        f"Changes:\n{changes_summary}\n\n"
        f"Tests marked: {marked_count}\n"
        f"Top tests:\n{json.dumps(test_summary, indent=2)}\n\n"
        f"Output ONLY the Markdown PR body, no code fences."
    )

    result = run_node(prompt, "pr_builder")

    # Validate we got something reasonable
    if result and len(result) > 50 and not result.startswith("Agent"):
        return result.strip()

    raise ValueError(f"Agent returned unusable PR description: {result[:100]}")


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
        lines.append(f"- `{t.test_node_id}` (score: {t.relevance_score:.2f}) -- {t.reason}")
    if len(tests) > 10:
        lines.append(f"- ... and {len(tests) - 10} more")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by ODF Z-Stream Pipeline*")

    return "\n".join(lines)
