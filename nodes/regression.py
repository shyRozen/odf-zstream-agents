"""Regression node — detects new regressions by comparing to history.

Uses Sonnet to compare current failures against historical test results
and identify new regressions (tests that previously passed but now fail).
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
    JUnitResults,
    RegressionInfo,
    StageError,
)
from core.state import AnalyzeState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a regression analysis expert for ODF (OpenShift Data Foundation).

Given current test results and historical results from previous runs,
identify NEW regressions — tests that were passing in recent runs but
are now failing.

Consider:
- A test that has been flaky (intermittently failing) is less likely to be
  a true regression.
- A test that consistently passed in all recent runs and now fails is a
  strong regression signal.
- Group related regressions that likely share the same root cause.

For each regression, output a JSON object with:
- "test_id": the test identifier
- "test_name": the test name
- "current_status": the current test status
- "previous_status": the most recent previous status
- "first_failed_version": the version where this test first started failing (if known, else null)

Output ONLY a JSON array of these objects, no other text.
"""


def regression(state: AnalyzeState) -> dict:
    """Compare current failures against historical results to find regressions.

    Returns a dict with ``regressions`` populated.
    """
    junit_results: JUnitResults | None = state.get("junit_results")
    manifest: ChangeManifest | None = state.get("change_manifest")

    if not junit_results or not junit_results.test_details:
        logger.info("No test results to check for regressions")
        return {"regressions": []}

    # Get current failures
    current_failures = [
        t for t in junit_results.test_details if t.status in ("FAIL", "ERROR")
    ]
    if not current_failures:
        logger.info("No failures in current run, no regressions")
        return {"regressions": []}

    # Query historical results
    try:
        from tools.db_tools import query_historical_results
    except ImportError:
        logger.warning("db_tools not available, cannot check historical results")
        return {
            "regressions": [],
            "errors": [
                StageError(
                    stage="regression",
                    error="db_tools module not available for historical comparison",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    try:
        lookback = config.REGRESSION_LOOKBACK
        failed_test_ids = [t.test_id for t in current_failures]

        historical = query_historical_results(
            test_ids=failed_test_ids,
            lookback=lookback,
        )

        if not historical:
            logger.info("No historical data available for regression comparison")
            # Without history, treat all failures as potential regressions
            regressions = [
                RegressionInfo(
                    test_id=t.test_id,
                    test_name=t.name,
                    current_status=t.status,
                    previous_status="UNKNOWN",
                    first_failed_version=manifest.zstream_version if manifest else None,
                )
                for t in current_failures
            ]
            return {"regressions": regressions}

        # Use LLM for nuanced regression detection
        llm = get_llm("regression")
        if llm is not None:
            try:
                regressions = _detect_with_llm(
                    llm, current_failures, historical, manifest
                )
                if regressions:
                    return {"regressions": regressions}
            except Exception as e:
                logger.error("LLM regression detection failed: %s", e)

        # Deterministic fallback
        regressions = _detect_without_llm(current_failures, historical, manifest)
        logger.info("Detected %d regressions", len(regressions))
        return {"regressions": regressions}

    except Exception as e:
        logger.error("Regression detection failed: %s", e)
        return {
            "regressions": [],
            "errors": [
                StageError(
                    stage="regression",
                    error=f"Regression detection failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _detect_with_llm(
    llm,
    current_failures,
    historical: dict | list,
    manifest: ChangeManifest | None,
) -> list[RegressionInfo]:
    """Use LLM to detect regressions from historical comparison."""
    current_data = [
        {
            "test_id": t.test_id,
            "test_name": t.name,
            "status": t.status,
            "error_message": (t.error_message or "")[:500],
        }
        for t in current_failures
    ]

    version = manifest.zstream_version if manifest else "unknown"

    prompt = (
        f"Identify new regressions in ODF z-stream {version}.\n\n"
        f"Current failures ({len(current_failures)} tests):\n"
        f"{json.dumps(current_data, indent=2)}\n\n"
        f"Historical results (last {config.REGRESSION_LOOKBACK} runs):\n"
        f"{json.dumps(historical, indent=2, default=str)}"
    )

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])

    return _parse_regression_response(response.content)


def _parse_regression_response(content: str) -> list[RegressionInfo]:
    """Parse the LLM regression detection response."""
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
        logger.error("Failed to parse regression LLM response")
        return []

    results = []
    for raw in raw_results:
        try:
            results.append(
                RegressionInfo(
                    test_id=raw.get("test_id", ""),
                    test_name=raw.get("test_name", ""),
                    current_status=raw.get("current_status", "FAIL"),
                    previous_status=raw.get("previous_status", "PASS"),
                    first_failed_version=raw.get("first_failed_version"),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse regression result: %s", e)

    return results


def _detect_without_llm(
    current_failures,
    historical: dict | list,
    manifest: ChangeManifest | None,
) -> list[RegressionInfo]:
    """Deterministic regression detection based on historical data.

    Strategy: A test is a regression if it was passing in the majority
    of recent runs (>= 60% pass rate historically).
    """
    # Build historical pass/fail rates per test
    history_map: dict[str, list[str]] = {}

    if isinstance(historical, dict):
        # Dict keyed by test_id -> list of statuses
        for test_id, statuses in historical.items():
            if isinstance(statuses, list):
                history_map[test_id] = [
                    s if isinstance(s, str) else s.get("status", "UNKNOWN")
                    for s in statuses
                ]
            elif isinstance(statuses, dict):
                status_val = statuses.get("status", statuses.get("result", "UNKNOWN"))
                history_map[test_id] = [status_val]
    elif isinstance(historical, list):
        # List of records, each with test_id and status
        for record in historical:
            if isinstance(record, dict):
                test_id = record.get("test_id", record.get("id", ""))
                status = record.get("status", record.get("result", "UNKNOWN"))
                history_map.setdefault(test_id, []).append(status)

    regressions = []
    version = manifest.zstream_version if manifest else None

    for failure in current_failures:
        hist_statuses = history_map.get(failure.test_id, [])

        if not hist_statuses:
            # No history — could be a new test or missing data
            # Treat as potential regression
            regressions.append(
                RegressionInfo(
                    test_id=failure.test_id,
                    test_name=failure.name,
                    current_status=failure.status,
                    previous_status="UNKNOWN",
                    first_failed_version=version,
                )
            )
            continue

        # Count passes in history
        normalized = [_normalize_status(s) for s in hist_statuses]
        pass_count = sum(1 for s in normalized if s == "PASS")
        total = len(normalized)
        pass_rate = pass_count / total if total > 0 else 0

        if pass_rate >= 0.6:
            # Was mostly passing, now failing -> regression
            most_recent = normalized[-1] if normalized else "UNKNOWN"
            regressions.append(
                RegressionInfo(
                    test_id=failure.test_id,
                    test_name=failure.name,
                    current_status=failure.status,
                    previous_status=most_recent,
                    first_failed_version=version,
                )
            )

    return regressions


def _normalize_status(status: str) -> str:
    """Normalize status strings."""
    upper = status.upper().strip()
    if upper in ("PASSED", "PASS", "FIXED", "SUCCESS"):
        return "PASS"
    if upper in ("FAILED", "FAIL", "FAILURE", "REGRESSION"):
        return "FAIL"
    if upper in ("ERRORED", "ERROR"):
        return "ERROR"
    if upper in ("SKIPPED", "SKIP", "NOT_RUN"):
        return "SKIP"
    return upper
