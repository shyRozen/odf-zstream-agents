"""Report Generator node -- produces the final analysis report.

Uses the unified agent runner to generate a comprehensive markdown report
and a concise Slack summary from all analysis data.  Falls back to a
template-based report.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from core.agent_runner import run_node, run_node_json
from core.models import (
    AnalysisReport,
    ChangeManifest,
    FailureAnalysis,
    JUnitResults,
    RegressionInfo,
    StageError,
)
from core.state import AnalyzeState

logger = logging.getLogger(__name__)


def report_generator(state: AnalyzeState) -> dict:
    """Generate markdown and Slack summary from analysis results.

    Returns a dict with ``analysis_report``.
    """
    junit_results: JUnitResults | None = state.get("junit_results")
    classifications = state.get("classifications") or []
    regressions = state.get("regressions") or []
    manifest: ChangeManifest | None = state.get("change_manifest")

    # Calculate pass rate
    pass_rate = 0.0
    if junit_results and junit_results.total > 0:
        pass_rate = junit_results.passed / junit_results.total

    # Try agent-generated report
    try:
        report = _generate_with_agent(
            junit_results, classifications, regressions, manifest, pass_rate
        )
        if report:
            return {"analysis_report": report}
    except Exception as e:
        logger.error("Agent report generation failed: %s", e)

    # Fallback to template-based report
    report = _generate_template(
        junit_results, classifications, regressions, manifest, pass_rate
    )

    logger.info("Report generated: pass_rate=%.1f%%", pass_rate * 100)
    return {"analysis_report": report}


# ------------------------------------------------------------------
# Agent-powered report generation
# ------------------------------------------------------------------

def _generate_with_agent(
    junit_results: JUnitResults | None,
    classifications: list[FailureAnalysis],
    regressions: list[RegressionInfo],
    manifest: ChangeManifest | None,
    pass_rate: float,
) -> AnalysisReport | None:
    """Generate report using the agent runner."""
    context = {
        "pass_rate": round(pass_rate, 3),
        "test_results": {
            "total": junit_results.total if junit_results else 0,
            "passed": junit_results.passed if junit_results else 0,
            "failed": junit_results.failed if junit_results else 0,
            "errored": junit_results.errored if junit_results else 0,
            "skipped": junit_results.skipped if junit_results else 0,
            "duration_seconds": junit_results.duration_seconds if junit_results else 0,
        },
        "classifications": [
            {
                "test_id": c.test_id,
                "test_name": c.test_name,
                "failure_type": c.failure_type.value,
                "root_cause": c.root_cause,
                "confidence": c.confidence,
                "linked_bug": c.linked_bug,
            }
            for c in classifications
        ],
        "regressions": [
            {
                "test_id": r.test_id,
                "test_name": r.test_name,
                "current_status": r.current_status,
                "previous_status": r.previous_status,
                "first_failed_version": r.first_failed_version,
            }
            for r in regressions
        ],
        "zstream_version": manifest.zstream_version if manifest else "unknown",
        "changes_count": len(manifest.changes) if manifest else 0,
    }

    prompt = (
        f"You are a QE report writer for ODF (OpenShift Data Foundation) "
        f"z-stream releases.\n\n"
        f"Generate two outputs from this analysis data:\n\n"
        f"1. A **Markdown report** (detailed, suitable for GitHub PR comment "
        f"   or wiki) containing:\n"
        f"   - Executive summary with pass rate and key findings\n"
        f"   - Test results overview (total, pass, fail, error, skip)\n"
        f"   - Failure analysis table with root causes and classifications\n"
        f"   - Regression analysis section\n"
        f"   - Recommendations for the team\n\n"
        f"2. A **Slack summary** (concise, 3-5 lines max) with:\n"
        f"   - Pass rate emoji indicator (green/yellow/red circle)\n"
        f"   - Key numbers (total, passed, failed)\n"
        f"   - Count of regressions if any\n"
        f"   - One-line recommendation\n\n"
        f"Analysis data:\n{json.dumps(context, indent=2)}\n\n"
        f"Return a JSON object with:\n"
        f'- "markdown_report": the full markdown report string\n'
        f'- "slack_summary": the concise Slack summary string\n'
        f'- "recommendations": list of actionable recommendation strings\n\n'
        f"Return ONLY the JSON object."
    )

    raw = run_node_json(prompt, "report_generator")

    if raw is None or not isinstance(raw, dict):
        logger.warning("Agent returned no parseable report result")
        return None

    return AnalysisReport(
        pass_rate=pass_rate,
        classifications=classifications,
        regressions=regressions,
        recommendations=raw.get("recommendations", []),
        markdown_report=raw.get("markdown_report", ""),
        slack_summary=raw.get("slack_summary", ""),
    )


# ------------------------------------------------------------------
# Template fallback
# ------------------------------------------------------------------

def _generate_template(
    junit_results: JUnitResults | None,
    classifications: list[FailureAnalysis],
    regressions: list[RegressionInfo],
    manifest: ChangeManifest | None,
    pass_rate: float,
) -> AnalysisReport:
    """Generate template-based report without agent."""
    version = manifest.zstream_version if manifest else "unknown"

    # Build markdown report
    md_lines = [
        f"# Z-Stream Analysis Report: {version}",
        "",
        "## Executive Summary",
        "",
    ]

    if pass_rate >= 0.95:
        md_lines.append(f"Overall pass rate: **{pass_rate:.1%}** - Excellent")
    elif pass_rate >= 0.80:
        md_lines.append(f"Overall pass rate: **{pass_rate:.1%}** - Acceptable with issues")
    else:
        md_lines.append(f"Overall pass rate: **{pass_rate:.1%}** - Needs attention")

    md_lines.append("")

    # Test results overview
    md_lines.extend([
        "## Test Results",
        "",
        "| Metric | Count |",
        "|--------|-------|",
    ])
    if junit_results:
        md_lines.append(f"| Total | {junit_results.total} |")
        md_lines.append(f"| Passed | {junit_results.passed} |")
        md_lines.append(f"| Failed | {junit_results.failed} |")
        md_lines.append(f"| Errors | {junit_results.errored} |")
        md_lines.append(f"| Skipped | {junit_results.skipped} |")
        duration_min = junit_results.duration_seconds / 60
        md_lines.append(f"| Duration | {duration_min:.1f} min |")
    else:
        md_lines.append("| Total | 0 |")
    md_lines.append("")

    # Failure classifications
    if classifications:
        md_lines.extend([
            "## Failure Analysis",
            "",
            "| Test | Type | Root Cause | Confidence | Bug |",
            "|------|------|------------|------------|-----|",
        ])
        for c in classifications:
            bug = c.linked_bug or "-"
            root_short = c.root_cause[:80] + "..." if len(c.root_cause) > 80 else c.root_cause
            md_lines.append(
                f"| {c.test_name} | {c.failure_type.value} | "
                f"{root_short} | {c.confidence:.0%} | {bug} |"
            )
        md_lines.append("")

        # Breakdown by type
        product_bugs = sum(1 for c in classifications if c.failure_type.value == "product_bug")
        test_bugs = sum(1 for c in classifications if c.failure_type.value == "test_bug")
        infra_issues = sum(1 for c in classifications if c.failure_type.value == "infra_issue")
        md_lines.extend([
            "### Failure Breakdown",
            "",
            f"- Product bugs: {product_bugs}",
            f"- Test bugs: {test_bugs}",
            f"- Infrastructure issues: {infra_issues}",
            "",
        ])

    # Regressions
    if regressions:
        md_lines.extend([
            "## Regressions",
            "",
            f"**{len(regressions)} new regression(s) detected:**",
            "",
            "| Test | Current | Previous | First Failed |",
            "|------|---------|----------|--------------|",
        ])
        for r in regressions:
            first_failed = r.first_failed_version or "-"
            md_lines.append(
                f"| {r.test_name} | {r.current_status} | "
                f"{r.previous_status} | {first_failed} |"
            )
        md_lines.append("")
    else:
        md_lines.extend([
            "## Regressions",
            "",
            "No new regressions detected.",
            "",
        ])

    # Recommendations
    recommendations = _generate_recommendations(
        classifications, regressions, pass_rate
    )

    if recommendations:
        md_lines.extend([
            "## Recommendations",
            "",
        ])
        for rec in recommendations:
            md_lines.append(f"- {rec}")
        md_lines.append("")

    md_lines.extend([
        "---",
        f"*Report generated at {datetime.utcnow().isoformat()}Z*",
    ])

    markdown_report = "\n".join(md_lines)

    # Build Slack summary
    if pass_rate >= 0.95:
        emoji = ":large_green_circle:"
    elif pass_rate >= 0.80:
        emoji = ":large_yellow_circle:"
    else:
        emoji = ":red_circle:"

    total = junit_results.total if junit_results else 0
    passed = junit_results.passed if junit_results else 0
    failed = (junit_results.failed + junit_results.errored) if junit_results else 0

    slack_lines = [
        f"{emoji} *Z-Stream {version} Test Results*: {pass_rate:.0%} pass rate",
        f"Total: {total} | Passed: {passed} | Failed: {failed}",
    ]
    if regressions:
        slack_lines.append(f":warning: {len(regressions)} new regression(s) detected")
    if recommendations:
        slack_lines.append(f"Top action: {recommendations[0]}")

    slack_summary = "\n".join(slack_lines)

    return AnalysisReport(
        pass_rate=pass_rate,
        classifications=classifications,
        regressions=regressions,
        recommendations=recommendations,
        markdown_report=markdown_report,
        slack_summary=slack_summary,
    )


def _generate_recommendations(
    classifications: list[FailureAnalysis],
    regressions: list[RegressionInfo],
    pass_rate: float,
) -> list[str]:
    """Generate actionable recommendations."""
    recommendations = []

    product_bugs = [c for c in classifications if c.failure_type.value == "product_bug"]
    test_bugs = [c for c in classifications if c.failure_type.value == "test_bug"]
    infra_issues = [c for c in classifications if c.failure_type.value == "infra_issue"]

    if product_bugs:
        high_conf = [b for b in product_bugs if b.confidence >= 0.7]
        if high_conf:
            recommendations.append(
                f"Investigate {len(high_conf)} high-confidence product bug(s) before release."
            )
        else:
            recommendations.append(
                f"Review {len(product_bugs)} potential product bug(s) -- confidence is low, "
                f"manual verification recommended."
            )

    if regressions:
        recommendations.append(
            f"Prioritize {len(regressions)} regression(s) -- these tests were previously passing."
        )

    if infra_issues:
        recommendations.append(
            f"Address {len(infra_issues)} infrastructure issue(s) and consider retrying affected tests."
        )

    if test_bugs:
        recommendations.append(
            f"Fix {len(test_bugs)} test bug(s) to improve test reliability."
        )

    if pass_rate < 0.80:
        recommendations.append(
            "Pass rate is below 80% -- consider blocking the z-stream release until resolved."
        )
    elif pass_rate < 0.95:
        recommendations.append(
            "Pass rate is below 95% -- review failures before approving the z-stream release."
        )

    if not recommendations:
        recommendations.append("All tests passing -- z-stream release is ready for approval.")

    return recommendations
