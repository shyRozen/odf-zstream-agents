"""Jira Inspector node — queries Jira for z-stream changes.

Uses Sonnet to extract structured Change objects from Jira search results.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a Jira issue analyst for ODF (OpenShift Data Foundation) z-stream releases.

Given a list of Jira issues, extract structured change information.

For each issue, output a JSON object with these fields:
- id: the Jira issue key (e.g., "ODF-1234")
- component: the ODF component affected (e.g., "ocs-operator", "rook-ceph", "noobaa", "ceph-csi", "odf-console")
- type: one of "bugfix", "security", "enhancement"
- severity: one of "critical", "major", "minor", "low"
- summary: a concise description of the change
- files_changed: list of files mentioned in the issue (empty list if none)
- linked_errata: errata advisory ID if mentioned (null otherwise)
- linked_commits: list of commit hashes if mentioned (empty list if none)

Output ONLY a JSON array of these objects, no other text.
"""


def jira_inspector(state: InspectState) -> dict:
    """Query Jira for bug fixes targeted at the z-stream version.

    Returns a dict with ``jira_changes`` populated.
    """
    version = state.get("zstream_version", "")
    if not version:
        logger.warning("No zstream_version provided, skipping Jira inspection")
        return {"jira_changes": []}

    # Fetch Jira issues for this z-stream version
    try:
        from tools.jira_tools import jira_search, jira_get_issue
    except ImportError:
        logger.warning("jira_tools not available, skipping Jira inspection")
        return {
            "jira_changes": [],
            "errors": [
                StageError(
                    stage="jira_inspector",
                    error="jira_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    try:
        # Search for issues fixed in this z-stream version
        jql = (
            f'project = ODF AND fixVersion = "{version}" '
            f"AND status in (Closed, Resolved, Verified) "
            f"ORDER BY priority DESC"
        )
        raw_results = jira_search(version)

        if isinstance(raw_results, str):
            try:
                search_results = json.loads(raw_results)
            except json.JSONDecodeError:
                search_results = []
        else:
            search_results = raw_results

        if isinstance(search_results, dict):
            search_results = search_results.get("issues", [])

        if not search_results:
            logger.info("No Jira issues found for version %s", version)
            return {"jira_changes": []}

        # Enrich each issue with full details
        enriched_issues = []
        for issue in search_results:
            if isinstance(issue, str):
                issue = {"key": issue}
            issue_key = issue.get("key", "")
            try:
                full_issue = jira_get_issue(issue_key)
                enriched_issues.append(full_issue)
            except Exception as e:
                logger.warning("Failed to fetch details for %s: %s", issue_key, e)
                enriched_issues.append(issue)

        # Use LLM to extract structured changes
        llm = get_llm("jira_inspector")
        if llm is None:
            logger.warning("No LLM available for jira_inspector, using raw parsing")
            return {"jira_changes": _parse_issues_without_llm(enriched_issues)}

        issues_text = json.dumps(enriched_issues, indent=2, default=str)
        prompt = (
            f"Extract change information from these Jira issues for "
            f"z-stream version {version}:\n\n{issues_text}"
        )

        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        # Parse the LLM response
        changes = _parse_llm_response(response.content)
        logger.info("Extracted %d changes from Jira", len(changes))
        return {"jira_changes": changes}

    except Exception as e:
        logger.error("Jira inspection failed: %s", e)
        return {
            "jira_changes": [],
            "errors": [
                StageError(
                    stage="jira_inspector",
                    error=f"Jira inspection failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _parse_llm_response(content: str) -> list[Change]:
    """Parse the LLM JSON response into Change objects."""
    # Strip markdown code fences if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        raw_changes = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response as JSON: %s", text[:200])
        return []

    changes = []
    for raw in raw_changes:
        try:
            change = Change(
                id=raw.get("id", "UNKNOWN"),
                source=ChangeSource.JIRA,
                component=raw.get("component", "unknown"),
                type=_safe_enum(ChangeType, raw.get("type", "bugfix"), ChangeType.BUGFIX),
                severity=_safe_enum(Severity, raw.get("severity", "minor"), Severity.MINOR),
                summary=raw.get("summary", ""),
                files_changed=raw.get("files_changed", []),
                linked_errata=raw.get("linked_errata"),
                linked_commits=raw.get("linked_commits", []),
            )
            changes.append(change)
        except Exception as e:
            logger.warning("Failed to parse change from LLM output: %s", e)

    return changes


def _parse_issues_without_llm(issues: list[dict]) -> list[Change]:
    """Fallback parser when no LLM is available."""
    changes = []
    for issue in issues:
        fields = issue.get("fields", issue)
        priority_name = ""
        priority = fields.get("priority")
        if isinstance(priority, dict):
            priority_name = priority.get("name", "").lower()
        elif isinstance(priority, str):
            priority_name = priority.lower()

        severity = Severity.MINOR
        if "critical" in priority_name or "blocker" in priority_name:
            severity = Severity.CRITICAL
        elif "major" in priority_name:
            severity = Severity.MAJOR
        elif "low" in priority_name or "trivial" in priority_name:
            severity = Severity.LOW

        issue_type = fields.get("issuetype", {})
        type_name = ""
        if isinstance(issue_type, dict):
            type_name = issue_type.get("name", "").lower()
        elif isinstance(issue_type, str):
            type_name = issue_type.lower()

        change_type = ChangeType.BUGFIX
        if "security" in type_name:
            change_type = ChangeType.SECURITY
        elif "enhancement" in type_name or "feature" in type_name:
            change_type = ChangeType.ENHANCEMENT

        components = fields.get("components", [])
        component = "unknown"
        if components:
            if isinstance(components[0], dict):
                component = components[0].get("name", "unknown")
            elif isinstance(components[0], str):
                component = components[0]

        summary = fields.get("summary", "")
        if not summary and isinstance(issue, dict):
            summary = issue.get("summary", "")

        key = issue.get("key", fields.get("key", "UNKNOWN"))

        changes.append(
            Change(
                id=key,
                source=ChangeSource.JIRA,
                component=component,
                type=change_type,
                severity=severity,
                summary=summary,
                files_changed=[],
                linked_errata=None,
                linked_commits=[],
            )
        )

    return changes


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
