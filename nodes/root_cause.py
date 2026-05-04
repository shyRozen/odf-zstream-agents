"""Root Cause node — performs root cause analysis on failures.

Uses **Opus** to analyze each failure's error message, test source code,
and related z-stream change to determine the root cause and classify it
as product_bug, test_bug, or infra_issue.
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
    FailureAnalysis,
    FailureType,
    JUnitResults,
    StageError,
)
from core.state import AnalyzeState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior QE engineer performing root cause analysis on test failures
for ODF (OpenShift Data Foundation) z-stream releases.

For each failed test, you are given:
- The test name and ID
- The error message / stack trace
- The preliminary classification (product_bug, test_bug, or infra_issue)
- The z-stream changes that might be related
- The initial confidence score

Analyze each failure and determine:
1. The most likely root cause (detailed explanation)
2. Whether the preliminary classification is correct — if not, provide the corrected type
3. A confidence score (0.0-1.0) for your analysis
4. Whether this is likely linked to a specific bug (provide bug ID if known)

For each failure, output a JSON object with:
- "test_id": the test identifier
- "test_name": the test name
- "failure_type": "product_bug", "test_bug", or "infra_issue"
- "root_cause": detailed explanation of the root cause
- "confidence": float 0.0-1.0
- "linked_bug": bug ID string or null
- "error_snippet": the most relevant part of the error message

Output ONLY a JSON array of these objects, no other text.
"""


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

    llm = get_llm("root_cause")  # Configured to use Opus
    if llm is None:
        logger.warning("No LLM available for root_cause analysis")
        # Return classifications with basic root cause from error messages
        updated = _basic_root_cause(needs_analysis)
        return {"classifications": updated}

    try:
        # Build context about z-stream changes
        changes_context = _build_changes_context(manifest)

        # Build test failure context
        failures_context = _build_failures_context(needs_analysis, junit_results)

        prompt = (
            f"Perform root cause analysis on these test failures.\n\n"
            f"Z-stream changes:\n{changes_context}\n\n"
            f"Test failures requiring analysis:\n{failures_context}"
        )

        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        analyzed = _parse_rca_response(response.content)
        if analyzed:
            logger.info("Root cause analysis complete for %d failures", len(analyzed))
            return {"classifications": analyzed}

        # Fallback if parsing fails
        logger.warning("Failed to parse RCA response, using basic analysis")
        return {"classifications": _basic_root_cause(needs_analysis)}

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


def _build_changes_context(manifest: ChangeManifest | None) -> str:
    """Build a context string describing the z-stream changes."""
    if not manifest or not manifest.changes:
        return "No z-stream change information available."

    lines = [f"Version: {manifest.zstream_version}"]
    for change in manifest.changes:
        lines.append(
            f"- [{change.id}] {change.component} ({change.type.value}): {change.summary}"
        )
        if change.files_changed:
            lines.append(f"  Files: {', '.join(change.files_changed[:5])}")
    return "\n".join(lines)


def _build_failures_context(
    failures: list[FailureAnalysis],
    junit_results: JUnitResults | None,
) -> str:
    """Build a context string describing the test failures."""
    # Build a lookup from test_id to full test result
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

        # Add full error message from junit results if available
        full_result = result_lookup.get(f.test_id, {})
        if full_result.get("error_message"):
            entry["full_error"] = full_result["error_message"][:2000]
        entry["duration_seconds"] = full_result.get("duration_seconds", 0)

        entries.append(entry)

    return json.dumps(entries, indent=2)


def _parse_rca_response(content: str) -> list[FailureAnalysis]:
    """Parse the LLM root cause analysis response."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        raw_results = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse RCA response as JSON")
        return []

    results = []
    for raw in raw_results:
        try:
            results.append(
                FailureAnalysis(
                    test_id=raw.get("test_id", ""),
                    test_name=raw.get("test_name", ""),
                    failure_type=_safe_failure_type(raw.get("failure_type", "product_bug")),
                    root_cause=raw.get("root_cause", "Unknown"),
                    confidence=float(raw.get("confidence", 0.5)),
                    linked_bug=raw.get("linked_bug"),
                    error_snippet=raw.get("error_snippet"),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse RCA result: %s", e)

    return results


def _basic_root_cause(failures: list[FailureAnalysis]) -> list[FailureAnalysis]:
    """Provide basic root cause descriptions without LLM."""
    results = []
    for f in failures:
        root_cause_msg = "Unable to perform detailed analysis (LLM unavailable)."
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
