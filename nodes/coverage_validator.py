"""Coverage Validator node -- checks test selection against change coverage.

Uses the unified agent runner to verify that selected tests adequately cover
all z-stream changes.  Falls back to deterministic component-keyword matching.
"""

from __future__ import annotations

import json
import logging

from core.agent_runner import run_node_json
from core import config
from core.models import (
    ChangeManifest,
    CoverageReport,
    GapDetail,
    TestSelection,
)
from core.state import MapState

logger = logging.getLogger(__name__)


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

    # Deterministic coverage check
    gap_details = _validate_without_llm(manifest, selected)

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


# ------------------------------------------------------------------
# Coverage validation
# ------------------------------------------------------------------


def _validate_coverage(
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Validate coverage and return gap details, using agent then fallback."""
    try:
        return _validate_with_agent(manifest, selected)
    except Exception as e:
        logger.error("Agent coverage validation failed: %s, using fallback", e)

    return _validate_without_llm(manifest, selected)


def _validate_with_agent(
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Use the agent runner to check coverage gaps."""
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

    threshold = config.MIN_RELEVANCE_SCORE

    prompt = (
        f"You are a test coverage analyst for ODF (OpenShift Data Foundation) "
        f"z-stream releases.\n\n"
        f"Analyze coverage for ODF z-stream {manifest.zstream_version}.\n\n"
        f"Filter tests by score >= {threshold}, check each change has "
        f"coverage, and identify gaps.\n\n"
        f"Changes requiring coverage:\n{json.dumps(changes_data, indent=2)}\n\n"
        f"Selected tests ({len(selected)} total):\n"
        f"{json.dumps(tests_data, indent=2)}\n\n"
        f"For each coverage gap, provide:\n"
        f'- "change_id": the ID of the uncovered change\n'
        f'- "component": the component of the uncovered change\n'
        f'- "reason": why there is a gap\n\n'
        f"Return a JSON object with:\n"
        f'- "covered_change_ids": list of change IDs that have adequate '
        f"  test coverage\n"
        f'- "gaps": list of gap objects as described above\n\n'
        f"Return ONLY the JSON object."
    )

    raw = run_node_json(prompt, "coverage_validator")

    if raw is None or not isinstance(raw, dict):
        logger.warning("Agent returned no parseable coverage result")
        return _validate_without_llm(manifest, selected)

    gaps_raw = raw.get("gaps", [])
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


# ------------------------------------------------------------------
# Deterministic fallback
# ------------------------------------------------------------------


def _validate_without_llm(
    manifest: ChangeManifest,
    selected: list[TestSelection],
) -> list[GapDetail]:
    """Deterministic fallback: check each change has at least one test
    whose file path or reason mentions the same component."""
    # Build set of covered components from test file paths and reasons
    covered_components: set[str] = set()
    for test in selected:
        text = f"{test.file_path} {test.reason} {test.test_node_id}".lower()
        for comp_keyword in [
            "ocs",
            "odf",
            "ceph",
            "rook",
            "noobaa",
            "mcg",
            "rgw",
            "pv",
            "csi",
            "ui",
            "console",
            "deploy",
            "upgrade",
            "disaster",
            "disaster-recovery",
            "dr",
            "monitor",
            "nfs",
            "lvmo",
            "lvm",
            "z_cluster",
            "pod_and_daemons",
            "object",
            "bucket",
            "operator",
            "odf-cli",
            "cli",
            "ceph-monitoring",
        ]:
            if comp_keyword in text:
                covered_components.add(comp_keyword)

    # Map components to their keywords for matching
    component_keywords = {
        "ocs-operator": ["ocs", "operator", "deploy", "z_cluster"],
        "odf-operator": ["odf", "operator", "deploy"],
        "rook": ["rook", "ceph", "z_cluster", "pod_and_daemons", "pv"],
        "rook-ceph": ["rook", "ceph", "z_cluster", "pod_and_daemons", "pv"],
        "mcg": ["noobaa", "mcg", "rgw", "object", "bucket"],
        "noobaa": ["noobaa", "mcg", "rgw", "object", "bucket"],
        "ceph-csi": ["csi", "pv", "storageclass"],
        "odf-console": ["ui", "console"],
        "disaster-recovery": ["disaster", "disaster-recovery", "dr", "rdr", "mdr"],
        "monitoring": ["monitor", "monitoring", "prometheus", "ceph-monitoring"],
        "ceph-monitoring": ["monitor", "monitoring", "prometheus", "ceph-monitoring"],
        "odf-cli": ["odf-cli", "cli", "odf_cli"],
        "nfs": ["nfs"],
        "lvmo": ["lvmo", "lvm"],
        "must-gather": ["must", "gather"],
        "deployment": ["deploy", "upgrade", "install"],
    }

    gaps = []
    for change in manifest.changes:
        keywords = component_keywords.get(change.component, [change.component.lower()])
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
