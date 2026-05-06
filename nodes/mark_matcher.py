"""Mark Matcher node -- per-testcase selection using the test index.

Loads the pre-built test index (532 files, 1058 tests) and scores
each test function against the z-stream changes based on:
- Component match (from directory mapping)
- Keyword overlap (bug summary vs test name/docstring/keywords)
- Tier priority (tier1 > tier2 > tier3)
- PR file path matching (if PR changed files are available)
"""

from __future__ import annotations

import logging
from pathlib import Path

from core import config
from core.models import ChangeManifest, TestSelection
from core.state import MapState

logger = logging.getLogger(__name__)


def mark_matcher(state: MapState) -> dict:
    search_areas = state.get("search_areas") or []
    manifest: ChangeManifest | None = state.get("change_manifest")
    component_mapping = state.get("component_test_mapping") or {}

    if not search_areas:
        logger.warning("No search areas provided, nothing to match")
        return {"scored_tests": []}

    # Load the pre-built test index
    try:
        from tools.ocs_ci_scanner import load_index

        index = load_index()
    except Exception as e:
        logger.error("Failed to load test index: %s", e)
        return {"scored_tests": []}

    # Build scoring context from changes
    changed_components = set()
    change_keywords: set[str] = set()
    pr_changed_files: set[str] = set()

    if manifest:
        for change in manifest.changes:
            changed_components.add(change.component.lower())
            # Keywords from summary
            for word in change.summary.lower().split():
                if len(word) > 3 and word.isalpha():
                    change_keywords.add(word)
            # Files changed in PRs
            for f in change.files_changed:
                pr_changed_files.add(f.lower())
                # Also extract filename without path
                basename = f.split("/")[-1].replace(".py", "").replace(".go", "")
                if len(basename) > 3:
                    change_keywords.add(basename.lower())

    # Reverse mapping: dir -> component
    dir_to_component: dict[str, str] = {}
    for comp, dirs in component_mapping.items():
        for d in dirs:
            dir_to_component[d.rstrip("/")] = comp

    # Normalize search areas
    search_prefixes = [sa.rstrip("/") for sa in search_areas]

    # Score every test file in the index that falls within search areas
    results: list[TestSelection] = []

    for file_info in index.get("files", []):
        file_path = file_info["file_path"]

        # Check if file is in a search area
        if not any(file_path.startswith(prefix) for prefix in search_prefixes):
            continue

        # Score each test function in the file
        file_squad = file_info.get("squad", "")
        file_marks = file_info.get("marks", [])
        file_keywords = set(file_info.get("keywords", []))
        file_tiers = file_info.get("tiers", [])
        file_desc = file_info.get("description", "")

        # Determine component from directory
        file_dir = str(Path(file_path).parent)
        comp = ""
        for dir_key, dir_comp in dir_to_component.items():
            if file_dir.startswith(dir_key.rstrip("/")):
                comp = dir_comp
                break

        for func in file_info.get("test_functions", []):
            score = _score_test_function(
                func=func,
                file_path=file_path,
                file_keywords=file_keywords,
                file_tiers=file_tiers,
                file_desc=file_desc,
                component=comp,
                changed_components=changed_components,
                change_keywords=change_keywords,
                pr_changed_files=pr_changed_files,
            )

            if score < config.MIN_RELEVANCE_SCORE:
                continue

            func_marks = func.get("marks", []) + file_marks
            squad = file_squad
            if not squad:
                for m in func_marks:
                    if "_squad" in m:
                        squad = m.split(".")[-1].split("(")[0]
                        break

            node_id = f"{file_path}::{func['node_id']}"

            results.append(
                TestSelection(
                    test_node_id=node_id,
                    file_path=file_path,
                    relevance_score=round(score, 2),
                    reason=_build_reason(func, comp, score),
                    existing_marks=[squad] if squad else [],
                )
            )

    # Sort by score descending
    results.sort(key=lambda t: -t.relevance_score)

    # Dynamic threshold: drop tests scoring < 70% of the top score
    if results:
        top_score = results[0].relevance_score
        dynamic_cutoff = max(top_score * 0.7, config.MIN_RELEVANCE_SCORE)
        results = [t for t in results if t.relevance_score >= dynamic_cutoff]

    # Cap at MAX_TESTS
    if len(results) > config.MAX_TESTS:
        results = results[: config.MAX_TESTS]

    logger.info(
        "Selected %d test cases (from %d files in search areas)",
        len(results),
        len(index.get("files", [])),
    )
    return {"scored_tests": results}


def _score_test_function(
    func: dict,
    file_path: str,
    file_keywords: set[str],
    file_tiers: list[str],
    file_desc: str,
    component: str,
    changed_components: set[str],
    change_keywords: set[str],
    pr_changed_files: set[str],
) -> float:
    """Score a single test function's relevance to the z-stream changes.

    Scoring tiers:
      0.90-1.00  PR file match — test references a file changed in the PR
      0.80-0.89  Strong keyword match — 3+ keywords from bug overlap with test
      0.70-0.79  Good keyword match — 2 keywords overlap + component match
      0.50-0.69  Component match only — right area but no specific keyword link
      0.30-0.49  Weak — in search area but no real connection
    """
    score = 0.0
    reasons = []

    func_name = func.get("name", "").lower()
    func_doc = func.get("docstring", "").lower()

    # Extract test-specific keywords from function name
    test_words = set()
    for word in func_name.replace("test_", "").split("_"):
        if len(word) > 2:
            test_words.add(word)
    test_words.update(file_keywords)

    # 1. PR file path matching — most precise signal
    if pr_changed_files:
        func_text = f"{func_name} {func_doc} {' '.join(file_keywords)}"
        for pr_file in pr_changed_files:
            pr_basename = pr_file.split("/")[-1].replace(".go", "").replace(".py", "")
            if len(pr_basename) > 3 and pr_basename in func_text:
                score = max(score, 0.95)
                reasons.append(f"PR file match: {pr_basename}")
                break

    # 2. Keyword overlap — how much the bug description matches the test
    keyword_hits = len(change_keywords & test_words)
    if keyword_hits >= 4:
        score = max(score, 0.90)
    elif keyword_hits >= 3:
        score = max(score, 0.80)
    elif keyword_hits >= 2:
        score = max(score, 0.70)
    elif keyword_hits == 1:
        score = max(score, 0.55)

    # 3. Component match — necessary but not sufficient
    if component and component.lower() in changed_components:
        score = max(score, 0.50)
    path_lower = file_path.lower()
    if any(c in path_lower for c in changed_components):
        score = max(score, 0.45)

    # 4. Tier1 boost (small — shouldn't push irrelevant tests over threshold)
    if "tier1" in file_tiers:
        score = min(score + 0.03, 1.0)

    return min(score, 1.0)


def _build_reason(func: dict, component: str, score: float) -> str:
    """Build a human-readable reason for the score."""
    parts = []
    if component:
        parts.append(component)
    doc = func.get("docstring", "")
    if doc:
        first_line = doc.split("\n")[0][:60]
        parts.append(first_line)
    else:
        parts.append(func.get("name", ""))
    return " | ".join(parts)
