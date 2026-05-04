"""Mark Matcher node — matches changes to pytest-marked tests.

Uses **Opus** to score each test's relevance to the z-stream changes
by reading test code, pytest marks, and change descriptions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from core.models import ChangeManifest, StageError, TestSelection
from core.state import MapState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior QE engineer for ODF (OpenShift Data Foundation).

You are evaluating whether specific test cases are relevant to z-stream changes.

For each test, you will be given:
- The test file path and test function name
- The test's existing pytest marks (e.g., @pytest.mark.tier1, @pytest.mark.brown_squad)
- A summary of the test's purpose (from its code/docstring)
- The z-stream changes that need test coverage

Score each test's relevance from 0.0 to 1.0:
- 1.0: Test directly validates the exact fix/change
- 0.8-0.9: Test covers the same component and feature area
- 0.6-0.7: Test covers related functionality that might be affected
- 0.3-0.5: Test is tangentially related
- 0.0-0.2: Test is unrelated

For each test, output a JSON object with:
- "test_node_id": the full pytest node ID (file::class::test or file::test)
- "file_path": the test file path
- "relevance_score": float 0.0-1.0
- "reason": brief explanation of why this score
- "existing_marks": list of existing pytest marks

Output ONLY a JSON array of these objects, no other text.
"""

# Process tests in batches to stay within token limits
BATCH_SIZE = 20


def mark_matcher(state: MapState) -> dict:
    """Score and select tests based on component mappings and pytest marks.

    Returns a dict with ``scored_tests``.
    """
    search_areas = state.get("search_areas") or []
    manifest: ChangeManifest | None = state.get("change_manifest")
    component_mapping = state.get("component_test_mapping") or {}

    if not search_areas:
        logger.warning("No search areas provided, nothing to match")
        return {"scored_tests": []}

    # Import test discovery tools
    try:
        from tools.ocs_ci_tools import list_tests, read_test_marks
    except ImportError:
        logger.warning("ocs_ci_tools not available, cannot discover tests")
        return {
            "scored_tests": [],
            "errors": [
                StageError(
                    stage="mark_matcher",
                    error="ocs_ci_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    # Discover tests in all search areas
    all_tests: list[dict] = []
    for area in search_areas:
        try:
            tests = list_tests(area)
            if tests:
                for test in tests:
                    if isinstance(test, str):
                        all_tests.append({"test_node_id": test, "file_path": test.split("::")[0]})
                    elif isinstance(test, dict):
                        all_tests.append(test)
        except Exception as e:
            logger.warning("Failed to list tests in %s: %s", area, e)

    if not all_tests:
        logger.info("No tests found in search areas")
        return {"scored_tests": []}

    # Read marks for each test
    for test_info in all_tests:
        file_path = test_info.get("file_path", "")
        if file_path:
            try:
                marks = read_test_marks(file_path)
                test_info["marks"] = marks if isinstance(marks, list) else []
            except Exception as e:
                logger.warning("Failed to read marks for %s: %s", file_path, e)
                test_info["marks"] = []

    # Use Opus LLM to score tests
    llm = get_llm("mark_matcher")  # Configured to use Opus
    if llm is None:
        logger.warning("No LLM available for mark_matcher, using heuristic scoring")
        return {"scored_tests": _score_heuristic(all_tests, manifest, component_mapping)}

    # Build change summary for the prompt
    change_summary = _build_change_summary(manifest)

    # Process in batches
    scored_tests: list[TestSelection] = []
    for i in range(0, len(all_tests), BATCH_SIZE):
        batch = all_tests[i : i + BATCH_SIZE]
        try:
            batch_results = _score_batch(llm, batch, change_summary)
            scored_tests.extend(batch_results)
        except Exception as e:
            logger.error("Failed to score batch %d: %s", i // BATCH_SIZE, e)
            # Fallback: give middle scores to this batch
            for test_info in batch:
                scored_tests.append(
                    TestSelection(
                        test_node_id=test_info.get("test_node_id", ""),
                        file_path=test_info.get("file_path", ""),
                        relevance_score=0.5,
                        reason="Scoring failed, assigned default score",
                        existing_marks=test_info.get("marks", []),
                    )
                )

    # Sort by relevance (highest first)
    scored_tests.sort(key=lambda t: t.relevance_score, reverse=True)

    logger.info("Scored %d tests, top score: %.2f", len(scored_tests),
                scored_tests[0].relevance_score if scored_tests else 0.0)

    return {"scored_tests": scored_tests}


def _build_change_summary(manifest: ChangeManifest | None) -> str:
    """Build a concise summary of changes for the scoring prompt."""
    if not manifest or not manifest.changes:
        return "No change details available."

    lines = [f"Z-stream version: {manifest.zstream_version}"]
    for change in manifest.changes:
        files_str = ", ".join(change.files_changed[:5]) if change.files_changed else "n/a"
        lines.append(
            f"- [{change.id}] {change.component} ({change.type.value}/{change.severity.value}): "
            f"{change.summary} | files: {files_str}"
        )
    return "\n".join(lines)


def _score_batch(llm, tests: list[dict], change_summary: str) -> list[TestSelection]:
    """Score a batch of tests using the LLM."""
    tests_data = []
    for t in tests:
        tests_data.append({
            "test_node_id": t.get("test_node_id", ""),
            "file_path": t.get("file_path", ""),
            "marks": t.get("marks", []),
            "docstring": t.get("docstring", ""),
        })

    prompt = (
        f"Score these {len(tests)} tests for relevance to the z-stream changes.\n\n"
        f"Z-stream changes:\n{change_summary}\n\n"
        f"Tests to evaluate:\n{json.dumps(tests_data, indent=2)}"
    )

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])

    return _parse_scored_response(response.content)


def _parse_scored_response(content: str) -> list[TestSelection]:
    """Parse the LLM scoring response into TestSelection objects."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        raw_tests = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse mark_matcher LLM response as JSON")
        return []

    results = []
    for raw in raw_tests:
        try:
            results.append(
                TestSelection(
                    test_node_id=raw.get("test_node_id", ""),
                    file_path=raw.get("file_path", ""),
                    relevance_score=float(raw.get("relevance_score", 0.0)),
                    reason=raw.get("reason", ""),
                    existing_marks=raw.get("existing_marks", []),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse scored test: %s", e)

    return results


def _score_heuristic(
    tests: list[dict],
    manifest: ChangeManifest | None,
    component_mapping: dict[str, list[str]],
) -> list[TestSelection]:
    """Heuristic fallback scoring when LLM is unavailable.

    Scores based on:
    - Whether the test directory matches a changed component (0.7 base)
    - Whether the test has tier1/tier2 marks (boost)
    - Whether the test file name contains keywords from change summaries
    """
    changed_components = set()
    change_keywords: set[str] = set()
    if manifest:
        for change in manifest.changes:
            changed_components.add(change.component)
            # Extract keywords from summary
            words = change.summary.lower().split()
            change_keywords.update(w for w in words if len(w) > 4)

    # Build reverse mapping: dir -> component
    dir_to_component: dict[str, str] = {}
    for comp, dirs in component_mapping.items():
        for d in dirs:
            dir_to_component[d] = comp

    results = []
    for test_info in tests:
        test_id = test_info.get("test_node_id", "")
        file_path = test_info.get("file_path", "")
        marks = test_info.get("marks", [])
        score = 0.3  # base

        # Check component match
        for dir_path, comp in dir_to_component.items():
            if file_path.startswith(dir_path) and comp in changed_components:
                score = 0.7
                break

        # Mark-based boost
        mark_names = [m if isinstance(m, str) else m.get("name", "") for m in marks]
        if "tier1" in mark_names:
            score = min(score + 0.1, 1.0)
        if "tier2" in mark_names:
            score = min(score + 0.05, 1.0)

        # Keyword match boost
        file_lower = file_path.lower()
        keyword_hits = sum(1 for kw in change_keywords if kw in file_lower)
        if keyword_hits > 0:
            score = min(score + 0.05 * keyword_hits, 1.0)

        results.append(
            TestSelection(
                test_node_id=test_id,
                file_path=file_path,
                relevance_score=round(score, 2),
                reason="Heuristic scoring (LLM unavailable)",
                existing_marks=mark_names,
            )
        )

    results.sort(key=lambda t: t.relevance_score, reverse=True)
    return results
