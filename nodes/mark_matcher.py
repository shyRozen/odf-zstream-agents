"""Mark Matcher node -- matches changes to pytest-marked tests.

Uses **Opus** (routed automatically by run_node via node_name) to score
each test's relevance to the z-stream changes by reading test code,
pytest marks, and change descriptions.  Falls back to heuristic scoring
based on component/keyword overlap.
"""

from __future__ import annotations

import json
import logging

from core.agent_runner import run_node_json
from core import config
from core.models import ChangeManifest, TestSelection
from core.state import MapState

logger = logging.getLogger(__name__)

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

    # Build change summary for the prompt
    change_summary = _build_change_summary(manifest)

    # Build the full prompt with all context
    search_areas_json = json.dumps(search_areas, indent=2)
    manifest_json = json.dumps(
        [c.model_dump(mode="json") for c in manifest.changes] if manifest else [],
        indent=2,
    )
    mapping_json = json.dumps(component_mapping, indent=2)

    try:
        prompt = (
            f"You are a senior QE engineer for ODF (OpenShift Data Foundation).\n\n"
            f"Read tests in these directories, score their relevance to these "
            f"z-stream changes (0.0-1.0).\n\n"
            f"Search areas (test directories to explore):\n{search_areas_json}\n\n"
            f"Component-to-test mapping:\n{mapping_json}\n\n"
            f"Z-stream changes:\n{change_summary}\n\n"
            f"Full change manifest:\n{manifest_json}\n\n"
            f"Instructions:\n"
            f"1. Use find/grep to discover test files in the search areas.\n"
            f"2. Read test code to understand what each test validates.\n"
            f"3. Check existing pytest marks (e.g. @pytest.mark.tier1, "
            f"   @pytest.mark.brown_squad).\n"
            f"4. Score each test's relevance from 0.0 to 1.0:\n"
            f"   - 1.0: directly validates the exact fix/change\n"
            f"   - 0.8-0.9: covers the same component and feature area\n"
            f"   - 0.6-0.7: covers related functionality\n"
            f"   - 0.3-0.5: tangentially related\n"
            f"   - 0.0-0.2: unrelated\n\n"
            f"For each test, output a JSON object with:\n"
            f'- "test_node_id": the full pytest node ID '
            f"  (file::class::test or file::test)\n"
            f'- "file_path": the test file path\n'
            f'- "relevance_score": float 0.0-1.0\n'
            f'- "reason": brief explanation of the score\n'
            f'- "existing_marks": list of existing pytest marks\n\n'
            f"Return ONLY a JSON array of these objects."
        )

        raw = run_node_json(
            prompt,
            "mark_matcher",  # This is in OPUS_NODES, so Opus will be used
            allowed_tools=[
                "Read",
                "Bash(find*)",
                "Bash(grep*)",
                "Bash(cat*)",
                "Bash(head*)",
            ],
            cwd=config.OCS_CI_REPO_PATH,
        )

        if raw is not None:
            scored_tests = _parse_scored_results(raw)
            if scored_tests:
                scored_tests.sort(key=lambda t: t.relevance_score, reverse=True)
                logger.info(
                    "Scored %d tests, top score: %.2f",
                    len(scored_tests),
                    scored_tests[0].relevance_score,
                )
                return {"scored_tests": scored_tests}

        logger.warning("Agent returned no usable scored tests, using heuristic")

    except Exception as e:
        logger.error("Agent mark matching failed: %s, using heuristic", e)

    # Fallback: heuristic scoring
    return {"scored_tests": _score_heuristic(manifest, component_mapping, search_areas)}


# ------------------------------------------------------------------
# Prompt helpers
# ------------------------------------------------------------------


def _build_change_summary(manifest: ChangeManifest | None) -> str:
    """Build a concise summary of changes for the scoring prompt."""
    if not manifest or not manifest.changes:
        return "No change details available."

    lines = [f"Z-stream version: {manifest.zstream_version}"]
    for change in manifest.changes:
        files_str = ", ".join(change.files_changed[:5]) if change.files_changed else "n/a"
        lines.append(
            f"- [{change.id}] {change.component} ({change.type.value}/"
            f"{change.severity.value}): {change.summary} | files: {files_str}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------


def _parse_scored_results(raw: dict | list) -> list[TestSelection]:
    """Convert agent JSON output into TestSelection objects."""
    items = raw if isinstance(raw, list) else [raw]
    results = []
    for item in items:
        try:
            results.append(
                TestSelection(
                    test_node_id=item.get("test_node_id", ""),
                    file_path=item.get("file_path", ""),
                    relevance_score=float(item.get("relevance_score", 0.0)),
                    reason=item.get("reason", ""),
                    existing_marks=item.get("existing_marks", []),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse scored test: %s", e)
    return results


# ------------------------------------------------------------------
# Heuristic fallback
# ------------------------------------------------------------------


def _score_heuristic(
    manifest: ChangeManifest | None,
    component_mapping: dict[str, list[str]],
    search_areas: list[str],
) -> list[TestSelection]:
    """Heuristic fallback scoring when agent is unavailable.

    Scores based on:
    - Whether the test directory matches a changed component (0.7 base)
    - Whether keywords from change summaries appear in the directory path
    """
    changed_components = set()
    change_keywords: set[str] = set()
    if manifest:
        for change in manifest.changes:
            changed_components.add(change.component)
            words = change.summary.lower().split()
            change_keywords.update(w for w in words if len(w) > 4)

    # Build reverse mapping: dir -> component
    dir_to_component: dict[str, str] = {}
    for comp, dirs in component_mapping.items():
        for d in dirs:
            dir_to_component[d] = comp

    results = []
    for area in search_areas:
        score = 0.3  # base

        # Check component match
        comp = dir_to_component.get(area, "")
        if comp and comp in changed_components:
            score = 0.7

        # Keyword match boost
        area_lower = area.lower()
        keyword_hits = sum(1 for kw in change_keywords if kw in area_lower)
        if keyword_hits > 0:
            score = min(score + 0.05 * keyword_hits, 1.0)

        results.append(
            TestSelection(
                test_node_id=area,
                file_path=area,
                relevance_score=round(score, 2),
                reason="Heuristic scoring (agent unavailable)",
                existing_marks=[],
            )
        )

    results.sort(key=lambda t: t.relevance_score, reverse=True)
    return results
