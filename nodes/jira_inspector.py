"""Jira Inspector node -- queries Jira for z-stream changes.

Uses the unified agent runner to extract structured Change objects from Jira
search results.  Falls back to deterministic parsing when the agent is
unavailable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from core.agent_runner import run_node_json
from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)


def jira_inspector(state: InspectState) -> dict:
    """Query Jira for bug fixes targeted at the z-stream version.

    Returns a dict with ``jira_changes`` populated.
    """
    version = state.get("zstream_version", "")
    if not version:
        logger.warning("No zstream_version provided, skipping Jira inspection")
        return {"jira_changes": []}

    try:
        prompt = (
            f"You are a Jira issue analyst for ODF (OpenShift Data Foundation) "
            f"z-stream releases.\n\n"
            f"Query Jira for ODF bugs fixed in version {version}.\n\n"
            f"Steps:\n"
            f"1. Use curl to query the Jira REST API for issues with "
            f"   fixVersion=\"{version}\" in the ODF project that are Closed, "
            f"   Resolved, or Verified.  Sort by priority DESC.\n"
            f"2. For each issue found, fetch its full details.\n"
            f"3. Extract structured change information.\n\n"
            f"For each issue, produce a JSON object with these fields:\n"
            f'- "id": the Jira issue key (e.g. "ODF-1234")\n'
            f'- "component": the ODF component affected (e.g. "ocs-operator", '
            f'  "rook-ceph", "noobaa", "ceph-csi", "odf-console")\n'
            f'- "type": one of "bugfix", "security", "enhancement"\n'
            f'- "severity": one of "critical", "major", "minor", "low"\n'
            f'- "summary": a concise description of the change\n'
            f'- "files_changed": list of files mentioned in the issue '
            f"  (empty list if none)\n"
            f'- "linked_errata": errata advisory ID if mentioned (null otherwise)\n'
            f'- "linked_commits": list of commit hashes if mentioned '
            f"  (empty list if none)\n\n"
            f"Return ONLY a JSON array of these objects."
        )

        raw = run_node_json(
            prompt,
            "jira_inspector",
            allowed_tools=["Bash(curl*)", "WebFetch"],
        )

        if raw is None:
            logger.warning("Agent returned no parseable JSON, using empty list")
            return {"jira_changes": []}

        changes = _parse_raw_changes(raw)
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


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _parse_raw_changes(raw: dict | list) -> list[Change]:
    """Convert agent JSON output into a list of Change objects."""
    items = raw if isinstance(raw, list) else [raw]
    changes = []
    for item in items:
        try:
            changes.append(
                Change(
                    id=item.get("id", "UNKNOWN"),
                    source=ChangeSource.JIRA,
                    component=item.get("component", "unknown"),
                    type=_safe_enum(ChangeType, item.get("type", "bugfix"), ChangeType.BUGFIX),
                    severity=_safe_enum(Severity, item.get("severity", "minor"), Severity.MINOR),
                    summary=item.get("summary", ""),
                    files_changed=item.get("files_changed", []),
                    linked_errata=item.get("linked_errata"),
                    linked_commits=item.get("linked_commits", []),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse change from agent output: %s", e)
    return changes


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
