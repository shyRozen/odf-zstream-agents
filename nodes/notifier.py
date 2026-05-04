"""Notifier node — sends pipeline results to Slack and GitHub.

NO LLM — deterministic node that posts the analysis report via
Slack webhook and as a GitHub PR comment.
"""
from __future__ import annotations

import logging
from datetime import datetime

from core.models import AnalysisReport, StageError
from core.state import PipelineState

logger = logging.getLogger(__name__)


def notifier(state: PipelineState) -> dict:
    """Send the analysis report via Slack webhook and GitHub PR comment.

    Returns an empty dict (terminal node).
    """
    report: AnalysisReport | None = state.get("analysis_report")
    pr_number = state.get("pr_number", 0)
    pr_url = state.get("pr_url", "")

    if not report:
        logger.warning("No analysis report to send")
        return {}

    errors = []

    # Post Slack notification
    if report.slack_summary:
        _post_slack(report.slack_summary, errors)

    # Post GitHub PR comment
    if report.markdown_report and pr_number:
        _comment_pr(report.markdown_report, pr_number, errors)

    if errors:
        return {
            "errors": [
                StageError(
                    stage="notifier",
                    error="; ".join(errors),
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ]
        }

    return {}


def _post_slack(summary: str, errors: list[str]) -> None:
    """Post summary to Slack via webhook."""
    try:
        from tools.slack_tools import slack_post_message
    except ImportError:
        logger.warning("slack_tools not available, skipping Slack notification")
        errors.append("slack_tools module not available")
        return

    try:
        slack_post_message(summary)
        logger.info("Slack notification sent successfully")
    except Exception as e:
        logger.error("Failed to send Slack notification: %s", e)
        errors.append(f"Slack notification failed: {e}")


def _comment_pr(markdown_report: str, pr_number: int, errors: list[str]) -> None:
    """Post the full report as a GitHub PR comment."""
    try:
        from tools.github_tools import github_comment_pr
    except ImportError:
        logger.warning("github_tools not available, skipping PR comment")
        errors.append("github_tools module not available")
        return

    try:
        github_comment_pr(pr_number=pr_number, body=markdown_report)
        logger.info("GitHub PR comment posted on PR #%d", pr_number)
    except Exception as e:
        logger.error("Failed to post PR comment: %s", e)
        errors.append(f"PR comment failed: {e}")
