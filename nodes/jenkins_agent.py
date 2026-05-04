"""Jenkins Agent node — triggers and monitors a Jenkins CI build.

NO LLM — deterministic node that triggers a build, polls with
exponential backoff, and fetches JUnit results.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from core import config
from core.models import JUnitResults, StageError, TestResult
from core.state import PipelineState

logger = logging.getLogger(__name__)


def jenkins_agent(state: PipelineState) -> dict:
    """Trigger a Jenkins build, poll for completion, and fetch JUnit results.

    Returns a dict with ``jenkins_build_id``, ``jenkins_build_url``,
    and ``junit_results``.
    """
    version = state.get("zstream_version", "unknown")
    pr_url = state.get("pr_url", "")

    try:
        from tools.jenkins_tools import (
            jenkins_trigger_build,
            jenkins_get_build_status,
            jenkins_get_test_report,
        )
    except ImportError:
        logger.warning("jenkins_tools not available, skipping Jenkins build")
        return {
            "jenkins_build_id": 0,
            "jenkins_build_url": "",
            "junit_results": JUnitResults(),
            "errors": [
                StageError(
                    stage="jenkins_agent",
                    error="jenkins_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    mark_expression = f"zstream_{version.replace('.', '_').replace('-', '_')}"

    # Build parameters
    params = dict(config.JENKINS_DEFAULT_PARAMS)
    params["TEST_MARK_EXPRESSION"] = mark_expression
    if pr_url:
        params["PR_URL"] = pr_url

    try:
        # Step 1: Trigger the build
        logger.info("Triggering Jenkins build with mark expression: %s", mark_expression)
        trigger_result = jenkins_trigger_build(
            job_name=config.JENKINS_JOB_NAME,
            parameters=json.dumps(params),
        )

        build_id = 0
        build_url = ""
        if isinstance(trigger_result, str):
            try:
                trigger_result = json.loads(trigger_result)
            except json.JSONDecodeError:
                pass
        if isinstance(trigger_result, dict):
            build_id = trigger_result.get("build_id", trigger_result.get("queue_id", 0))
            build_url = trigger_result.get("url", trigger_result.get("build_url", ""))
        elif isinstance(trigger_result, int):
            build_id = trigger_result

        if not build_id and not build_url:
            logger.error("Jenkins trigger returned no build identifier")
            return {
                "jenkins_build_id": 0,
                "jenkins_build_url": "",
                "junit_results": JUnitResults(),
                "errors": [
                    StageError(
                        stage="jenkins_agent",
                        error="Jenkins trigger returned no build identifier",
                        timestamp=datetime.utcnow().isoformat(),
                        recoverable=True,
                    )
                ],
            }

        logger.info("Jenkins build triggered: id=%s url=%s", build_id, build_url)

        # Step 2: Poll for completion with exponential backoff
        poll_interval = config.JENKINS_POLL_INITIAL
        max_interval = config.JENKINS_POLL_MAX
        multiplier = config.JENKINS_POLL_MULTIPLIER
        max_wait_seconds = config.JENKINS_MAX_WAIT_HOURS * 3600

        elapsed = 0
        build_status = None

        while elapsed < max_wait_seconds:
            logger.info(
                "Polling Jenkins build status (elapsed: %ds, next poll in %ds)...",
                elapsed,
                poll_interval,
            )
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status_result = jenkins_get_build_status(
                    job_name=config.JENKINS_JOB_NAME,
                    build_number=build_id,
                )

                if isinstance(status_result, dict):
                    build_status = status_result.get("status", status_result.get("result", ""))
                elif isinstance(status_result, str):
                    build_status = status_result

                # Check if the build is complete
                if build_status and build_status.upper() in (
                    "SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT",
                ):
                    logger.info("Jenkins build completed with status: %s", build_status)
                    # Update build_url if returned by status check
                    if isinstance(status_result, dict):
                        build_url = status_result.get("url", build_url)
                        build_id = status_result.get("build_id", build_id)
                    break

            except Exception as e:
                logger.warning("Error polling Jenkins: %s", e)

            # Exponential backoff
            poll_interval = min(int(poll_interval * multiplier), max_interval)
        else:
            logger.error("Jenkins build timed out after %d seconds", max_wait_seconds)
            return {
                "jenkins_build_id": build_id,
                "jenkins_build_url": build_url,
                "junit_results": JUnitResults(),
                "errors": [
                    StageError(
                        stage="jenkins_agent",
                        error=f"Jenkins build timed out after {config.JENKINS_MAX_WAIT_HOURS}h",
                        timestamp=datetime.utcnow().isoformat(),
                        recoverable=False,
                    )
                ],
            }

        # Step 3: Fetch test report
        logger.info("Fetching JUnit test report...")
        try:
            report_data = jenkins_get_test_report(
                job_name=config.JENKINS_JOB_NAME,
                build_number=build_id,
            )
            junit_results = _parse_test_report(report_data)
        except Exception as e:
            logger.error("Failed to fetch test report: %s", e)
            junit_results = JUnitResults()

        logger.info(
            "Jenkins results: %d total, %d passed, %d failed, %d errors, %d skipped",
            junit_results.total,
            junit_results.passed,
            junit_results.failed,
            junit_results.errored,
            junit_results.skipped,
        )

        return {
            "jenkins_build_id": build_id,
            "jenkins_build_url": build_url,
            "junit_results": junit_results,
        }

    except Exception as e:
        logger.error("Jenkins agent failed: %s", e)
        return {
            "jenkins_build_id": 0,
            "jenkins_build_url": "",
            "junit_results": JUnitResults(),
            "errors": [
                StageError(
                    stage="jenkins_agent",
                    error=f"Jenkins agent failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _parse_test_report(report_data: dict | list | str) -> JUnitResults:
    """Parse Jenkins test report data into JUnitResults."""
    if isinstance(report_data, str):
        # If it's a raw string, return empty results
        logger.warning("Test report is a raw string, cannot parse")
        return JUnitResults()

    if isinstance(report_data, list):
        # List of test results
        test_details = []
        passed = failed = errored = skipped = 0
        total_duration = 0.0

        for item in report_data:
            if not isinstance(item, dict):
                continue
            result = _parse_single_test(item)
            test_details.append(result)
            total_duration += result.duration_seconds
            if result.status == "PASS":
                passed += 1
            elif result.status == "FAIL":
                failed += 1
            elif result.status == "ERROR":
                errored += 1
            elif result.status == "SKIP":
                skipped += 1

        return JUnitResults(
            total=len(test_details),
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            duration_seconds=total_duration,
            test_details=test_details,
        )

    if isinstance(report_data, dict):
        # Jenkins-style report object
        test_details = []
        suites = report_data.get("suites", [])
        for suite in suites:
            cases = suite.get("cases", []) if isinstance(suite, dict) else []
            for case in cases:
                if isinstance(case, dict):
                    test_details.append(_parse_single_test(case))

        # Also check for top-level "cases" or "testCases"
        if not test_details:
            for key in ("cases", "testCases", "tests"):
                cases = report_data.get(key, [])
                if cases:
                    for case in cases:
                        if isinstance(case, dict):
                            test_details.append(_parse_single_test(case))
                    break

        passed = sum(1 for t in test_details if t.status == "PASS")
        failed = sum(1 for t in test_details if t.status == "FAIL")
        errored = sum(1 for t in test_details if t.status == "ERROR")
        skipped = sum(1 for t in test_details if t.status == "SKIP")
        total_duration = sum(t.duration_seconds for t in test_details)

        return JUnitResults(
            total=report_data.get("totalCount", len(test_details)),
            passed=report_data.get("passCount", passed),
            failed=report_data.get("failCount", failed),
            errored=errored,
            skipped=report_data.get("skipCount", skipped),
            duration_seconds=report_data.get("duration", total_duration),
            test_details=test_details,
        )

    return JUnitResults()


def _parse_single_test(case: dict) -> TestResult:
    """Parse a single test case from Jenkins report."""
    name = case.get("name", case.get("testName", "unknown"))
    class_name = case.get("className", case.get("classname", ""))
    test_id = f"{class_name}::{name}" if class_name else name

    status_raw = case.get("status", case.get("result", "")).upper()
    status_map = {
        "PASSED": "PASS",
        "PASS": "PASS",
        "FIXED": "PASS",
        "FAILED": "FAIL",
        "FAIL": "FAIL",
        "REGRESSION": "FAIL",
        "ERROR": "ERROR",
        "ERRORED": "ERROR",
        "SKIPPED": "SKIP",
        "SKIP": "SKIP",
        "NOT_RUN": "SKIP",
    }
    status = status_map.get(status_raw, status_raw or "UNKNOWN")

    duration = case.get("duration", case.get("time", 0.0))
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    error_msg = case.get("errorDetails", case.get("errorMessage", case.get("message")))
    if not error_msg:
        stderr = case.get("stderr", case.get("errorStackTrace", ""))
        if stderr:
            # Take first few lines of stack trace
            error_msg = "\n".join(str(stderr).split("\n")[:10])

    return TestResult(
        test_id=test_id,
        name=name,
        status=status,
        duration_seconds=duration,
        error_message=error_msg,
    )
