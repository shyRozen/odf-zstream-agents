"""Mark Matcher node -- matches changes to pytest-marked tests.

Expands matched test directories into individual test_*.py files
using the local ocs-ci repo. Scores each file based on component
and keyword relevance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core import config
from core.models import ChangeManifest, TestSelection
from core.state import MapState
from core.test_map import load_test_areas

logger = logging.getLogger(__name__)

OCS_CI_ROOT = Path(config.OCS_CI_REPO_PATH)


def mark_matcher(state: MapState) -> dict:
    search_areas = state.get("search_areas") or []
    manifest: ChangeManifest | None = state.get("change_manifest")
    component_mapping = state.get("component_test_mapping") or {}

    if not search_areas:
        logger.warning("No search areas provided, nothing to match")
        return {"scored_tests": []}

    scored_tests = _score_test_files(manifest, component_mapping, search_areas)
    logger.info("Scored %d test files", len(scored_tests))
    return {"scored_tests": scored_tests}


def _find_test_files(directory: str) -> list[Path]:
    """Find all test_*.py files recursively under a directory."""
    full_path = OCS_CI_ROOT / directory
    if not full_path.exists():
        return []
    return sorted(full_path.rglob("test_*.py"))


def _score_test_files(
    manifest: ChangeManifest | None,
    component_mapping: dict[str, list[str]],
    search_areas: list[str],
) -> list[TestSelection]:
    changed_components = set()
    change_keywords: set[str] = set()
    if manifest:
        for change in manifest.changes:
            changed_components.add(change.component.lower())
            for word in change.summary.lower().split():
                if len(word) > 4:
                    change_keywords.add(word)

    # Load map for squad info per directory
    test_areas = load_test_areas()
    dir_to_squad: dict[str, str] = {}
    for area in test_areas:
        d = area.get("directory", "").rstrip("/")
        if d:
            dir_to_squad[d] = area.get("squad", "")

    # Reverse mapping: dir -> component
    dir_to_component: dict[str, str] = {}
    for comp, dirs in component_mapping.items():
        for d in dirs:
            dir_to_component[d.rstrip("/")] = comp

    results = []
    seen_files: set[str] = set()

    for search_area in search_areas:
        test_files = _find_test_files(search_area)
        if not test_files:
            logger.warning("No test files found in %s", search_area)
            continue

        area_key = search_area.rstrip("/")
        comp = dir_to_component.get(area_key, "")
        squad = dir_to_squad.get(area_key, "")

        # Walk up to find squad if not found at exact dir level
        if not squad:
            for map_dir, map_squad in dir_to_squad.items():
                if area_key.startswith(map_dir):
                    squad = map_squad
                    break

        for test_file in test_files:
            rel_path = str(test_file.relative_to(OCS_CI_ROOT))
            if rel_path in seen_files:
                continue
            seen_files.add(rel_path)

            score = _score_file(rel_path, test_file, comp, changed_components, change_keywords)

            results.append(
                TestSelection(
                    test_node_id=rel_path,
                    file_path=rel_path,
                    relevance_score=round(score, 2),
                    reason=f"{comp or 'matched'} ({squad})",
                    existing_marks=[squad] if squad else [],
                )
            )

    results.sort(key=lambda t: -t.relevance_score)
    return results


def _score_file(
    rel_path: str,
    file_path: Path,
    component: str,
    changed_components: set[str],
    change_keywords: set[str],
) -> float:
    """Score an individual test file's relevance."""
    score = 0.5

    # Component match — strongest signal
    if component and component.lower() in changed_components:
        score = 0.8

    # Path-based component match
    path_lower = rel_path.lower()
    if any(c in path_lower for c in changed_components):
        score = max(score, 0.75)

    # Read first 50 lines for keyword matching
    try:
        head = file_path.read_text(errors="ignore").split("\n")[:50]
        file_text = " ".join(head).lower()

        # Check for tier1 mark
        if "@tier1" in file_text or "tier1" in file_text:
            score = min(score + 0.1, 1.0)

        # Keyword match from change summaries
        hits = sum(1 for kw in change_keywords if kw in file_text)
        if hits > 0:
            score = min(score + 0.05 * min(hits, 4), 1.0)

    except Exception:
        pass

    return score
