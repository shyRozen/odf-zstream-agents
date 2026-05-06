"""Coverage Validator node -- checks test selection against change coverage.

Uses the component→directory mapping from config to verify that every
changed component has at least one selected test in its mapped directory.
"""

from __future__ import annotations

import logging

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
    attempt = (state.get("attempt_count") or 0) + 1
    scored_tests = state.get("scored_tests") or []
    manifest: ChangeManifest | None = state.get("change_manifest")
    component_mapping = state.get("component_test_mapping") or {}

    selected = scored_tests

    if not manifest or not manifest.changes:
        return {
            "selected_tests": selected,
            "coverage_report": CoverageReport(
                total_changes=0, covered=0, gaps=0, coverage_ratio=1.0
            ),
            "attempt_count": attempt,
        }

    # Check coverage using the component→directory mapping
    # A component is covered if ANY selected test's file_path starts with
    # one of the component's mapped directories
    gaps = []
    for change in manifest.changes:
        comp = change.component
        dirs = component_mapping.get(comp, [])

        if not dirs:
            # Try fallback: check if any selected test path contains the component name
            comp_variants = [
                comp.lower(),
                comp.lower().replace("-", "_"),
                comp.lower().replace("_", "-"),
            ]
            has_test = any(
                any(v in t.file_path.lower() or v in t.test_node_id.lower() for v in comp_variants)
                for t in selected
            )
            if not has_test:
                gaps.append(
                    GapDetail(
                        change_id=change.id,
                        component=comp,
                        reason=f"No test directory mapped for component '{comp}'",
                    )
                )
            continue

        # Check if any selected test is in one of the component's directories
        has_test = any(any(t.file_path.startswith(d.rstrip("/")) for d in dirs) for t in selected)
        if not has_test:
            gaps.append(
                GapDetail(
                    change_id=change.id,
                    component=comp,
                    reason=f"No selected tests in {dirs}",
                )
            )

    total = len(manifest.changes)
    gap_count = len(gaps)
    covered = total - gap_count
    ratio = covered / total if total > 0 else 1.0

    report = CoverageReport(
        total_changes=total,
        covered=covered,
        gaps=gap_count,
        coverage_ratio=round(ratio, 3),
        gap_details=gaps,
    )

    logger.info(
        "Coverage (attempt %d): %d/%d covered (%.0f%%), %d gaps",
        attempt,
        covered,
        total,
        ratio * 100,
        gap_count,
    )

    return {
        "selected_tests": selected,
        "coverage_report": report,
        "attempt_count": attempt,
    }
