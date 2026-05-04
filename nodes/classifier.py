"""Classifier node — categorizes test failures by type.

NO LLM — deterministic node that parses junit_results and classifies
each test by its status (PASS/FAIL/ERROR/SKIP) using simple pattern
matching on error messages.
"""

from __future__ import annotations

import logging
import re

from core.models import (
    FailureAnalysis,
    FailureType,
    JUnitResults,
    TestResult,
)
from core.state import AnalyzeState

logger = logging.getLogger(__name__)

# Patterns indicating infrastructure issues
INFRA_PATTERNS = [
    r"timeout.*connect",
    r"connection\s+refused",
    r"connection\s+reset",
    r"network\s+unreachable",
    r"dns\s+resolution\s+failed",
    r"no\s+route\s+to\s+host",
    r"ssh.*timeout",
    r"kubectl.*timed?\s*out",
    r"oc\s+.*timed?\s*out",
    r"pod.*not\s+ready",
    r"node.*not\s+ready",
    r"cluster.*unavailable",
    r"insufficient.*resources",
    r"quota.*exceeded",
    r"disk\s+pressure",
    r"memory\s+pressure",
    r"evict",
    r"oom\s*kill",
    r"image\s+pull.*error",
    r"registry.*unavailable",
    r"jenkins.*agent.*offline",
    r"workspace.*cleanup",
]

# Patterns indicating test bugs (not product bugs)
TEST_BUG_PATTERNS = [
    r"assert.*expected.*but\s+got",
    r"fixture.*not\s+found",
    r"fixture.*error",
    r"import\s+error",
    r"module\s+not\s+found",
    r"attribute\s*error.*test",
    r"type\s*error.*test",
    r"name\s*error.*test",
    r"syntax\s*error.*test",
    r"setup.*failed",
    r"teardown.*failed",
    r"conftest.*error",
    r"parametrize.*error",
    r"xfail.*strict",
    r"deprecated.*test",
    r"stale.*element",
    r"selector.*not\s+found",
]


def classifier(state: AnalyzeState) -> dict:
    """Classify each test result by its status and failure type.

    Parses junit_results to update test_details with normalized statuses,
    and produces initial FailureAnalysis entries for failed/errored tests.

    Returns a dict with updated ``junit_results`` and ``classifications``.
    """
    junit_results: JUnitResults | None = state.get("junit_results")

    if not junit_results:
        logger.warning("No junit_results to classify")
        return {"classifications": []}

    test_details = junit_results.test_details
    if not test_details:
        logger.info("No test details to classify")
        return {"classifications": []}

    classifications: list[FailureAnalysis] = []
    pass_count = 0
    fail_count = 0
    error_count = 0
    skip_count = 0

    for test in test_details:
        status = _normalize_status(test.status)
        # Update the test's status in place for downstream nodes
        test.status = status

        if status == "PASS":
            pass_count += 1
        elif status == "SKIP":
            skip_count += 1
        elif status in ("FAIL", "ERROR"):
            if status == "FAIL":
                fail_count += 1
            else:
                error_count += 1

            # Classify the failure
            failure_type = _classify_failure(test)
            confidence = _compute_confidence(test, failure_type)

            classifications.append(
                FailureAnalysis(
                    test_id=test.test_id,
                    test_name=test.name,
                    failure_type=failure_type,
                    root_cause="Pending root cause analysis",
                    confidence=confidence,
                    error_snippet=_extract_snippet(test.error_message),
                )
            )

    # Update aggregate counts
    updated_results = JUnitResults(
        total=len(test_details),
        passed=pass_count,
        failed=fail_count,
        errored=error_count,
        skipped=skip_count,
        duration_seconds=junit_results.duration_seconds,
        test_details=test_details,
    )

    logger.info(
        "Classified %d tests: %d pass, %d fail, %d error, %d skip -> %d failure analyses",
        len(test_details),
        pass_count,
        fail_count,
        error_count,
        skip_count,
        len(classifications),
    )

    return {
        "junit_results": updated_results,
        "classifications": classifications,
    }


def _normalize_status(status: str) -> str:
    """Normalize various status strings to PASS/FAIL/ERROR/SKIP."""
    upper = status.upper().strip()
    mapping = {
        "PASSED": "PASS",
        "PASS": "PASS",
        "FIXED": "PASS",
        "SUCCESS": "PASS",
        "FAILED": "FAIL",
        "FAIL": "FAIL",
        "FAILURE": "FAIL",
        "REGRESSION": "FAIL",
        "ERRORED": "ERROR",
        "ERROR": "ERROR",
        "SKIPPED": "SKIP",
        "SKIP": "SKIP",
        "NOT_RUN": "SKIP",
        "DISABLED": "SKIP",
        "XFAIL": "SKIP",
    }
    return mapping.get(upper, upper)


def _classify_failure(test: TestResult) -> FailureType:
    """Classify a test failure based on error message patterns."""
    error_msg = (test.error_message or "").lower()

    if not error_msg:
        # No error message — default to product bug
        return FailureType.PRODUCT_BUG

    # Check infrastructure patterns first (higher priority)
    for pattern in INFRA_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return FailureType.INFRA_ISSUE

    # Check test bug patterns
    for pattern in TEST_BUG_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return FailureType.TEST_BUG

    # Default to product bug
    return FailureType.PRODUCT_BUG


def _compute_confidence(test: TestResult, failure_type: FailureType) -> float:
    """Compute confidence score for the classification.

    Higher confidence when error messages clearly match patterns.
    Lower confidence when classification is ambiguous.
    """
    error_msg = (test.error_message or "").lower()

    if not error_msg:
        return 0.3  # Low confidence without error details

    if failure_type == FailureType.INFRA_ISSUE:
        # Count how many infra patterns match
        matches = sum(1 for p in INFRA_PATTERNS if re.search(p, error_msg, re.IGNORECASE))
        return min(0.5 + 0.1 * matches, 0.9)

    if failure_type == FailureType.TEST_BUG:
        matches = sum(1 for p in TEST_BUG_PATTERNS if re.search(p, error_msg, re.IGNORECASE))
        return min(0.5 + 0.1 * matches, 0.9)

    # Product bug — check if there are assertion errors (strong signal)
    if "assert" in error_msg and "fixture" not in error_msg:
        return 0.6
    return 0.4  # Uncertain — needs root cause analysis


def _extract_snippet(error_message: str | None, max_lines: int = 5) -> str | None:
    """Extract a useful snippet from the error message."""
    if not error_message:
        return None
    lines = error_message.strip().split("\n")
    if len(lines) <= max_lines:
        return error_message.strip()
    # Take first and last lines for context
    snippet_lines = lines[:3] + ["..."] + lines[-2:]
    return "\n".join(snippet_lines)
