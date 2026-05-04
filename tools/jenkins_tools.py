"""Jenkins CI tools for triggering builds and collecting results."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from core import config


def _get_jenkins_server():
    """Create a Jenkins server connection, or return an error string."""
    if not config.JENKINS_URL:
        return None, json.dumps({"error": "JENKINS_URL not configured"})
    if not config.JENKINS_USER or not config.JENKINS_API_TOKEN:
        return None, json.dumps({"error": "JENKINS_USER or JENKINS_API_TOKEN not configured"})

    try:
        import jenkins

        server = jenkins.Jenkins(
            config.JENKINS_URL,
            username=config.JENKINS_USER,
            password=config.JENKINS_API_TOKEN,
        )
        # Verify connection
        server.get_whoami()
        return server, None

    except Exception as exc:
        return None, json.dumps({"error": f"Failed to connect to Jenkins: {str(exc)}"})


def jenkins_trigger_build(job_name: str, parameters: str) -> str:
    """Trigger a parameterized Jenkins build.

    Queues a build for the specified job with the given parameters.
    Default parameters from config are merged with the provided ones.

    Args:
        job_name: The Jenkins job name (e.g. "qe-deploy-ocs-cluster-prod").
        parameters: JSON string of build parameters. These are merged with
                    the default parameters from config.yaml. Example:
                    '{"OCS_VERSION": "4.16.1", "TEST_SUITE": "tier1"}'

    Returns:
        JSON string with the queue item number, or an error message.
    """
    server, error = _get_jenkins_server()
    if error:
        return error

    try:
        # Parse provided parameters
        try:
            params = json.loads(parameters) if parameters else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid JSON parameters: {str(exc)}"})

        # Merge with default parameters (provided params take precedence)
        merged_params = dict(config.JENKINS_DEFAULT_PARAMS)
        merged_params.update(params)

        queue_number = server.build_job(job_name, parameters=merged_params)

        return json.dumps({
            "job_name": job_name,
            "queue_number": queue_number,
            "parameters": merged_params,
            "message": f"Build queued for '{job_name}' (queue #{queue_number})",
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to trigger build: {str(exc)}"})


def jenkins_get_build_status(job_name: str, build_number: int) -> str:
    """Get the status of a Jenkins build.

    Returns build information including status (SUCCESS, FAILURE, BUILDING, etc.),
    duration, timestamp, and build parameters.

    Args:
        job_name: The Jenkins job name.
        build_number: The build number to check.

    Returns:
        JSON string with build status details, or an error message.
    """
    server, error = _get_jenkins_server()
    if error:
        return error

    try:
        build_info = server.get_build_info(job_name, build_number)

        result = {
            "job_name": job_name,
            "build_number": build_number,
            "result": build_info.get("result"),  # None if still building
            "building": build_info.get("building", False),
            "duration_ms": build_info.get("duration", 0),
            "estimated_duration_ms": build_info.get("estimatedDuration", 0),
            "timestamp": build_info.get("timestamp", 0),
            "url": build_info.get("url", ""),
        }

        # Determine human-readable status
        if build_info.get("building"):
            result["status"] = "BUILDING"
        elif build_info.get("result"):
            result["status"] = build_info["result"]
        else:
            result["status"] = "UNKNOWN"

        # Extract build parameters
        actions = build_info.get("actions", [])
        for action in actions:
            if action.get("_class", "").endswith("ParametersAction"):
                params = {}
                for param in action.get("parameters", []):
                    params[param.get("name", "")] = param.get("value", "")
                result["parameters"] = params
                break

        return json.dumps(result, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to get build status: {str(exc)}"})


def jenkins_get_test_report(job_name: str, build_number: int) -> str:
    """Get the JUnit test report for a Jenkins build.

    Retrieves the test results summary including pass/fail/skip counts
    and details of failed tests.

    Args:
        job_name: The Jenkins job name.
        build_number: The build number to get results for.

    Returns:
        JSON string with test report summary and failed test details.
    """
    server, error = _get_jenkins_server()
    if error:
        return error

    try:
        try:
            report = server.get_build_test_report(job_name, build_number)
        except Exception as exc:
            error_msg = str(exc)
            if "404" in error_msg or "Not Found" in error_msg:
                return json.dumps({
                    "error": "No test report available for this build",
                    "job_name": job_name,
                    "build_number": build_number,
                    "note": "The build may still be running or didn't produce JUnit results",
                })
            raise

        # Extract summary
        total = report.get("totalCount", 0)
        failed = report.get("failCount", 0)
        skipped = report.get("skipCount", 0)
        passed = total - failed - skipped

        # Extract failed test details
        failed_tests = []
        for suite in report.get("suites", []):
            for case in suite.get("cases", []):
                if case.get("status") in ("FAILED", "REGRESSION", "ERROR"):
                    failed_tests.append({
                        "name": case.get("name", ""),
                        "class_name": case.get("className", ""),
                        "status": case.get("status", ""),
                        "duration": case.get("duration", 0),
                        "error_message": (case.get("errorDetails", "") or "")[:500],
                        "error_stacktrace": (case.get("errorStackTrace", "") or "")[:1000],
                    })

        result = {
            "job_name": job_name,
            "build_number": build_number,
            "summary": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "pass_rate": round(passed / total * 100, 1) if total > 0 else 0.0,
            },
            "failed_tests": failed_tests[:50],  # Cap at 50 to avoid overflow
            "failed_test_count": len(failed_tests),
        }

        return json.dumps(result, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to get test report: {str(exc)}"})


def jenkins_get_console_log(job_name: str, build_number: int) -> str:
    """Get the console output of a Jenkins build (last 5000 characters).

    Retrieves the build console log, truncated to the last 5000 characters
    to avoid token overflow when passed to an LLM agent.

    Args:
        job_name: The Jenkins job name.
        build_number: The build number.

    Returns:
        JSON string with the console log text (truncated), or an error message.
    """
    server, error = _get_jenkins_server()
    if error:
        return error

    try:
        output = server.get_build_console_output(job_name, build_number)

        max_chars = 5000
        truncated = len(output) > max_chars
        if truncated:
            output = output[-max_chars:]

        return json.dumps({
            "job_name": job_name,
            "build_number": build_number,
            "truncated": truncated,
            "total_length": len(output) if not truncated else f">{max_chars}",
            "console_output": output,
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to get console log: {str(exc)}"})


# Tool-wrapped versions for LangGraph ReAct agents
jenkins_trigger_build_tool = tool(jenkins_trigger_build)
jenkins_get_build_status_tool = tool(jenkins_get_build_status)
jenkins_get_test_report_tool = tool(jenkins_get_test_report)
jenkins_get_console_log_tool = tool(jenkins_get_console_log)
