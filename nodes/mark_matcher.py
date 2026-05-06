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
from core.test_map import load_test_areas

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

    # Use codebase map + heuristic scoring (fast, deterministic)
    scored_tests = _score_heuristic(manifest, component_mapping, search_areas)
    logger.info("Scored %d test areas using codebase map", len(scored_tests))
    return {"scored_tests": scored_tests}


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
    """Heuristic scoring using the pre-built codebase map.

    Scores based on:
    - Whether the test area matches a changed component (0.8 base)
    - Tier1 tests get a boost
    - Keyword overlap with change summaries
    """
    changed_components = set()
    change_keywords: set[str] = set()
    if manifest:
        for change in manifest.changes:
            changed_components.add(change.component.lower())
            words = change.summary.lower().split()
            change_keywords.update(w for w in words if len(w) > 4)

    # Load test areas from the codebase map
    test_areas = load_test_areas()

    # Build reverse mapping: dir -> component
    dir_to_component: dict[str, str] = {}
    for comp, dirs in component_mapping.items():
        for d in dirs:
            dir_to_component[d] = comp

    results = []
    for area in test_areas:
        area_dir = area.get("directory", "")

        # Only consider areas that match our search areas
        if not any(area_dir.startswith(sa.rstrip("/")) for sa in search_areas):
            continue

        test_count = area.get("test_functions", 0)
        if test_count == 0:
            continue

        squad = area.get("squad", "")
        tiers = area.get("tiers", {})
        score = 0.5

        # Component match
        comp = dir_to_component.get(area_dir, "")
        if comp and comp.lower() in changed_components:
            score = 0.8
        elif any(c in area_dir.lower() for c in changed_components):
            score = 0.7

        # Tier1 boost
        if tiers.get("tier1", 0) > 0:
            score = min(score + 0.1, 1.0)

        # Keyword match
        area_text = (area_dir + " " + area.get("body", "")).lower()
        hits = sum(1 for kw in change_keywords if kw in area_text)
        if hits > 0:
            score = min(score + 0.05 * min(hits, 4), 1.0)

        results.append(
            TestSelection(
                test_node_id=area.get("name", area_dir),
                file_path=area_dir,
                relevance_score=round(score, 2),
                reason=f"{test_count} tests ({squad}, tier1={tiers.get('tier1', 0)})",
                existing_marks=[squad] if squad else [],
            )
        )

    results.sort(key=lambda t: t.relevance_score, reverse=True)
    return results
