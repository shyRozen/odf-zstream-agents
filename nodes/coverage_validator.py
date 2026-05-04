"""Coverage Validator node — checks test selection against change coverage.

Uses Sonnet to verify that selected tests adequately cover all z-stream
changes. Identifies coverage gaps and returns a filtered test list.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core import config
from core.llm import get_llm
from core.models import (
    ChangeManifest,
    CoverageReport,
    GapDetail,
    StageError,
    TestSelection,
)
from core.state import MapState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a test coverage analyst for ODF (OpenShift Data Foundation) z-stream releases.

Given a list of z-stream changes and scored test selections, determine:

1. Which changes are adequately covered by the selected tests.
2. Which changes have coverage gaps (no relevant test or only low-relevance tests).

For each gap, provide:
- change_id: the ID of the uncovered change
- component: the component of the uncovered change
- reason: why there's a gap (e.g., "no tests found for this component",
  "only low-relevance tests available")

Output a JSON object with:
- "covered_change_ids": list of change IDs that have adequate test coverage
- "gaps": list of {"change_id": str, "component": str, "reason": str}

Output ONLY the JSON object, no other text.
"""


def coverage_validator(state: MapState) -> dict:
    """Validate that selected tests provide sufficient change coverage.

    Returns a dict with ``selected_tests``, ``coverage_report``,
    and incremented ``attempt_count``.
    """
    attempt = (state.get("attempt_count") or 0) + 1
    scored_tests = state.get("scored_tests") or []
    manifest: ChangeManifest | None = state.get("change_manifest")

    min_score = config.MIN_RELEVANCE_SCORE
    max_tests = config.MAX_TESTS

    # Filter tests by minimum relevance score
    selected = [t for t in scored_tests if t.relevance_score >= min_score]

    # Cap at max tests (take highest-scored)
    selected.sort(key=lambda t: t.relevance_score, reverse=True)
    if len(selected) > max_tests:
        logger.info("Capping test selection from %d to %d", len(selected), max_tests)
        selected = selected[:max_tests]

    if not manifest or not manifest.changes:
        logger.info("No changes to validate coverage against")
        report = CoverageReport(
            total_changes=0,
            covered=0,
            gaps=0,
            coverage_ratio=1.0,
            gap_details=[],
        )
        return {
            "selected_tests": selected,
            "coverage_report": report,
            "attempt_count": attempt,
        }

    # Determine coverage using LLM or fallback
    gap_details = _validate_coverage(manifest, selected)

    total = len(manifest.changes)
    gaps = len(gap_details)
    covered = total - gaps
    ratio = covered / total if total > 0 else 1.0

    report = CoverageReport(
        total_changes=total,
        covered=covered,
        gaps=gaps,
        coverage_ratio=round(ratio, 3),
        gap_details=gap_details,
    )

    logger.info(
        "Coverage validation (attempt %d): %d/%d changes covered (%.1f%%), %d gaps",
        attempt,
        covered,
        total,
        ratio * 100,
        gaps,
    )

    return {
        "selected_tests": selected,
        "coverage_report": report,
        "attempt_count": attempt,
    }


def _validate_coverage(
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Validate coverage and return gap details."""
    llm = get_llm("coverage_validator")
    if llm is not None:
        try:
            return _validate_with_llm(llm, manifest, selected)
        except Exception as e:
            logger.error("LLM coverage validation failed: %s, using fallback", e)

    return _validate_without_llm(manifest, selected)


def _validate_with_llm(
    llm,
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Use LLM to check coverage gaps."""
    changes_data = [
        {
            "id": c.id,
            "component": c.component,
            "type": c.type.value,
            "severity": c.severity.value,
            "summary": c.summary,
        }
        for c in manifest.changes
    ]

    tests_data = [
        {
            "test_node_id": t.test_node_id,
            "file_path": t.file_path,
            "relevance_score": t.relevance_score,
            "reason": t.reason,
        }
        for t in selected
    ]

    prompt = (
        f"Analyze coverage for ODF z-stream {manifest.zstream_version}.\n\n"
        f"Changes requiring coverage:\n{json.dumps(changes_data, indent=2)}\n\n"
        f"Selected tests ({len(selected)} total):\n{json.dumps(tests_data, indent=2)}"
    )

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])

    return _parse_coverage_response(response.content, manifest)


def _parse_coverage_response(content: str, manifest: ChangeManifest) -> list[GapDetail]:
    """Parse the LLM coverage validation response."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse coverage LLM response")
        return _validate_without_llm(manifest, [])

    gaps_raw = result.get("gaps", [])
    gaps = []
    for g in gaps_raw:
        try:
            gaps.append(
                GapDetail(
                    change_id=g.get("change_id", "UNKNOWN"),
                    component=g.get("component", "unknown"),
                    reason=g.get("reason", ""),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse gap detail: %s", e)

    return gaps


def _validate_without_llm(
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Deterministic fallback: check each change has at least one test
    whose file path or reason mentions the same component."""
    # Build set of covered components from test file paths and reasons
    covered_components: set[str] = set()
    for test in selected:
        path_lower = test.file_path.lower()
        reason_lower = test.reason.lower()
        # Extract component hints from file paths
        for comp_keyword in [
            "ocs", "odf", "ceph", "rook", "noobaa", "mcg",
            "rgw", "pv", "csi", "ui", "console", "deploy", "upgrade",
        ]:
            if comp_keyword in path_lower or comp_keyword in reason_lower:
                covered_components.add(comp_keyword)

    # Map components to their keywords for matching
    component_keywords = {
        "ocs-operator": ["ocs", "operator"],
        "odf-operator": ["odf", "operator"],
        "rook-ceph": ["rook", "ceph"],
        "noobaa": ["noobaa", "mcg", "rgw", "object", "bucket"],
        "ceph-csi": ["csi", "pv", "storageclass"],
        "odf-console": ["ui", "console"],
        "deployment": ["deploy", "upgrade", "install"],
    }

    gaps = []
    for change in manifest.changes:
        keywords = component_keywords.get(
            change.component, [change.component.lower()]
        )
        has_coverage = any(kw in covered_components for kw in keywords)
        if not has_coverage:
            gaps.append(
                GapDetail(
                    change_id=change.id,
                    component=change.component,
                    reason=f"No tests found matching component '{change.component}'",
                )
            )

    return gaps
