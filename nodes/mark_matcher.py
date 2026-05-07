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

    # Load the pre-built test index (version-specific if available)
    try:
        from tools.ocs_ci_scanner import load_index

        version = state.get("version", "")
        index = load_index(version=version or None)
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
            # Keywords from summary — strip brackets, punctuation
            import re

            clean_summary = re.sub(r"[\[\](){}:,\"']", " ", change.summary.lower())
            for word in clean_summary.split():
                if len(word) > 3 and word.isalpha():
                    change_keywords.add(word)
            # Files changed in PRs + AI-extracted keywords
            for f in change.files_changed:
                if f.startswith("__keyword__"):
                    # AI-extracted keyword from PR analyzer
                    kw = f.replace("__keyword__", "").lower()
                    if len(kw) > 2:
                        change_keywords.add(kw)
                    continue
                pr_changed_files.add(f.lower())
                for segment in f.replace("/", " ").replace("-", " ").replace("_", " ").split():
                    word = segment.split(".")[0].lower()
                    if len(word) > 2 and word.isalpha():
                        change_keywords.add(word)

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

    def _test_matches_component(test_path: str, comp: str) -> bool:
        path_lower = test_path.lower()
        if comp in path_lower:
            return True
        for d in component_mapping.get(comp, []):
            if path_lower.startswith(d.rstrip("/")):
                return True
        return False

    # Dynamic threshold: drop tests scoring < 70% of the top score
    if results:
        top_score = results[0].relevance_score
        dynamic_cutoff = max(top_score * 0.7, config.MIN_RELEVANCE_SCORE)
        above_cutoff = [t for t in results if t.relevance_score >= dynamic_cutoff]
        below_cutoff = [t for t in results if t.relevance_score < dynamic_cutoff]

        # Guarantee at least one test per changed component
        covered_components = set()
        for t in above_cutoff:
            for comp in changed_components:
                if _test_matches_component(t.file_path, comp):
                    covered_components.add(comp)

        for comp in changed_components - covered_components:
            for t in below_cutoff:
                if _test_matches_component(t.file_path, comp):
                    above_cutoff.append(t)
                    covered_components.add(comp)
                    logger.info(
                        "Force-included test for uncovered component '%s': %s (%.2f)",
                        comp,
                        t.test_node_id,
                        t.relevance_score,
                    )
                    break

        results = above_cutoff

    # Guarantee at least one test per changed component before capping
    top_results = results[: config.MAX_TESTS]
    overflow = results[config.MAX_TESTS :]

    logger.info(
        "Before force-include: %d in top, %d in overflow, components: %s",
        len(top_results), len(overflow), changed_components,
    )

    covered_in_top = set()
    for t in top_results:
        for comp in changed_components:
            if _test_matches_component(t.file_path, comp):
                covered_in_top.add(comp)

    missing = changed_components - covered_in_top
    if missing:
        logger.info("Components missing from top %d: %s", config.MAX_TESTS, missing)

    for comp in missing:
        found = False
        for t in overflow:
            if _test_matches_component(t.file_path, comp):
                top_results.append(t)
                found = True
                logger.info(
                    "Force-included test for '%s': %s (%.2f)",
                    comp,
                    t.test_node_id.split("::")[-1],
                    t.relevance_score,
                )
                break
        if not found:
            logger.warning(
                "No test found for component '%s' in overflow (%d tests)",
                comp, len(overflow),
            )

    results = top_results

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

    # 3. Component match — guarantees the test is considered
    if component and component.lower() in changed_components:
        score = max(score, 0.70)
    path_lower = file_path.lower()
    if any(c in path_lower for c in changed_components):
        score = max(score, 0.70)

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
