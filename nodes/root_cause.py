"""Root Cause node -- performs root cause analysis on failures.

Uses **Opus** (routed automatically by run_node via node_name) to analyze
each failure's error message, test source code, and related z-stream change
to determine the root cause and classify it as product_bug, test_bug, or
infra_issue.  Falls back to basic pattern-based classification.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from core.agent_runner import run_node_json
from core import config
from core.models import (
    ChangeManifest,
    FailureAnalysis,
    FailureType,
    JUnitResults,
    StageError,
)
from core.state import AnalyzeState

logger = logging.getLogger(__name__)


def root_cause(state: AnalyzeState) -> dict:
    """Analyze classified failures to determine root causes.

    Returns a dict with ``classifications`` containing updated root cause info.
    """
    classifications = state.get("classifications") or []
    junit_results: JUnitResults | None = state.get("junit_results")
    manifest: ChangeManifest | None = state.get("change_manifest")

    if not classifications:
        logger.info("No classifications to analyze for root cause")
        return {"classifications": []}

    # Filter to only failures that need root cause analysis
    needs_analysis = [
        c for c in classifications
        if c.root_cause == "Pending root cause analysis"
        or c.confidence < config.ROOT_CAUSE_CONFIDENCE_THRESHOLD
    ]

    if not needs_analysis:
        logger.info("All classifications already have root causes above threshold")
        return {"classifications": []}

    try:
        # Build context about z-stream changes
        changes_context = _build_changes_context(manifest)

        # Build test failure context
        failures_context = _build_failures_context(needs_analysis, junit_results)

        prompt = (
            f"You are a senior QE engineer performing root cause analysis on "
            f"test failures for ODF (OpenShift Data Foundation) z-stream "
            f"releases.\n\n"
            f"Perform root cause analysis on these test failures.\n\n"
            f"Z-stream changes:\n{changes_context}\n\n"
            f"Test failures requiring analysis:\n{failures_context}\n\n"
            f"Instructions:\n"
            f"1. For each failure, read the test source code at the given "
            f"   file path using the Read tool or grep to understand what "
            f"   the test does.\n"
            f"2. Analyze the error message / stack trace.\n"
            f"3. Cross-reference with the z-stream changes.\n"
            f"4. Determine:\n"
            f"   - The most likely root cause (detailed explanation)\n"
            f"   - Whether the classification is correct; if not, correct it\n"
            f"   - A confidence score (0.0-1.0)\n"
            f"   - Whether this is linked to a specific bug\n\n"
            f"For each failure, output a JSON object with:\n"
            f'- "test_id": the test identifier\n'
            f'- "test_name": the test name\n'
            f'- "failure_type": "product_bug", "test_bug", or "infra_issue"\n'
            f'- "root_cause": detailed explanation of the root cause\n'
            f'- "confidence": float 0.0-1.0\n'
            f'- "linked_bug": bug ID string or null\n'
            f'- "error_snippet": the most relevant part of the error message\n\n'
            f"Return ONLY a JSON array of these objects."
        )

        raw = run_node_json(
            prompt,
            "root_cause",  # This is in OPUS_NODES, so Opus will be used
            allowed_tools=["Read", "Bash(grep*)", "Bash(head*)"],
            cwd=config.OCS_CI_REPO_PATH,
        )

        if raw is not None:
            analyzed = _parse_rca_results(raw)
            if analyzed:
                logger.info("Root cause analysis complete for %d failures", len(analyzed))
                return {"classifications": analyzed}

        logger.warning("Agent returned no usable RCA results, using basic analysis")

    except Exception as e:
        logger.error("Root cause analysis failed: %s", e)
        return {
            "classifications": _basic_root_cause(needs_analysis),
            "errors": [
                StageError(
                    stage="root_cause",
                    error=f"Root cause analysis failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    # Fallback
    return {"classifications": _basic_root_cause(needs_analysis)}


# ------------------------------------------------------------------
# Context builders
# ------------------------------------------------------------------

def _build_changes_context(manifest: ChangeManifest | None) -> str:
    """Build a context string describing the z-stream changes."""
    if not manifest or not manifest.changes:
        return "No z-stream change information available."

    lines = [f"Version: {manifest.zstream_version}"]
    for change in manifest.changes:
        lines.append(
            f"- [{change.id}] {change.component} ({change.type.value}): "
            f"{change.summary}"
        )
        if change.files_changed:
            lines.append(f"  Files: {', '.join(change.files_changed[:5])}")
    return "\n".join(lines)


def _build_failures_context(
    failures: list[FailureAnalysis],
    junit_results: JUnitResults | None,
) -> str:
    """Build a context string describing the test failures."""
    result_lookup: dict[str, dict] = {}
    if junit_results:
        for test in junit_results.test_details:
            result_lookup[test.test_id] = {
                "name": test.name,
                "status": test.status,
                "duration_seconds": test.duration_seconds,
                "error_message": test.error_message or "",
            }

    entries = []
    for f in failures:
        entry = {
            "test_id": f.test_id,
            "test_name": f.test_name,
            "preliminary_type": f.failure_type.value,
            "confidence": f.confidence,
            "error_snippet": f.error_snippet or "",
        }

        full_result = result_lookup.get(f.test_id, {})
        if full_result.get("error_message"):
            entry["full_error"] = full_result["error_message"][:2000]
        entry["duration_seconds"] = full_result.get("duration_seconds", 0)

        entries.append(entry)

    return json.dumps(entries, indent=2)


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _parse_rca_results(raw: dict | list) -> list[FailureAnalysis]:
    """Convert agent JSON output into FailureAnalysis objects."""
    items = raw if isinstance(raw, list) else [raw]
    results = []
    for item in items:
        try:
            results.append(
                FailureAnalysis(
                    test_id=item.get("test_id", ""),
                    test_name=item.get("test_name", ""),
                    failure_type=_safe_failure_type(item.get("failure_type", "product_bug")),
                    root_cause=item.get("root_cause", "Unknown"),
                    confidence=float(item.get("confidence", 0.5)),
                    linked_bug=item.get("linked_bug"),
                    error_snippet=item.get("error_snippet"),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse RCA result: %s", e)
    return results


# ------------------------------------------------------------------
# Deterministic fallback
# ------------------------------------------------------------------

def _basic_root_cause(failures: list[FailureAnalysis]) -> list[FailureAnalysis]:
    """Provide basic root cause descriptions without agent."""
    results = []
    for f in failures:
        root_cause_msg = "Unable to perform detailed analysis (agent unavailable)."
        if f.error_snippet:
            root_cause_msg = f"Error indicates: {f.error_snippet[:200]}"

        results.append(
            FailureAnalysis(
                test_id=f.test_id,
                test_name=f.test_name,
                failure_type=f.failure_type,
                root_cause=root_cause_msg,
                confidence=max(f.confidence, 0.3),
                linked_bug=f.linked_bug,
                error_snippet=f.error_snippet,
            )
        )
    return results


def _safe_failure_type(value: str) -> FailureType:
    """Safely convert a string to FailureType."""
    try:
        return FailureType(value.lower())
    except (ValueError, AttributeError):
        return FailureType.PRODUCT_BUG
