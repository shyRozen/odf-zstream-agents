"""Jira Inspector node -- queries Jira for z-stream bugs and their PRs.

Fetches bugs from DFBUGS project, extracts GitHub PR URLs from
remote links, and builds Change objects with PR references.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)

PRIORITY_TO_SEVERITY = {
    "blocker": Severity.CRITICAL,
    "critical": Severity.CRITICAL,
    "major": Severity.MAJOR,
    "normal": Severity.MINOR,
    "minor": Severity.MINOR,
    "trivial": Severity.LOW,
}


def jira_inspector(state: InspectState) -> dict:
    version = state.get("zstream_version", "")
    if not version:
        return {"jira_changes": []}

    try:
        from tools.jira_tools import jira_search
    except ImportError:
        return {
            "jira_changes": [],
            "errors": [
                StageError(
                    stage="jira_inspector",
                    error="jira_tools not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    try:
        raw_result = jira_search(version)
        data = json.loads(raw_result) if isinstance(raw_result, str) else raw_result

        if "error" in data:
            logger.error("Jira search error: %s", data["error"])
            return {
                "jira_changes": [],
                "errors": [
                    StageError(
                        stage="jira_inspector",
                        error=f"Jira API: {data['error']}",
                        timestamp=datetime.utcnow().isoformat(),
                        recoverable=True,
                    )
                ],
            }

        issues = data.get("issues", [])
        if not issues:
            logger.info("No Jira issues found for version %s", version)
            return {"jira_changes": []}

        changes = []
        total_prs = 0
        for issue in issues:
            summary = issue.get("summary", "")
            priority = issue.get("priority", "normal").lower()
            components = issue.get("components", [])
            raw_comp = components[0] if components else ""
            component = _normalize_component(raw_comp) if raw_comp else _guess_component(summary)
            pr_urls = issue.get("pr_urls", [])
            total_prs += len(pr_urls)

            change_type = ChangeType.BUGFIX
            if "cve" in summary.lower():
                change_type = ChangeType.SECURITY
            elif "enhancement" in issue.get("issuetype", "").lower():
                change_type = ChangeType.ENHANCEMENT

            changes.append(
                Change(
                    id=issue.get("key", "UNKNOWN"),
                    source=ChangeSource.JIRA,
                    component=component,
                    type=change_type,
                    severity=PRIORITY_TO_SEVERITY.get(priority, Severity.MINOR),
                    summary=summary,
                    files_changed=[],
                    linked_errata=None,
                    linked_commits=pr_urls,
                )
            )

        logger.info(
            "Found %d bugs, %d PRs for version %s",
            len(changes),
            total_prs,
            version,
        )
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


JIRA_COMPONENT_MAP = {
    "management-console": "odf-console",
    "multi-cloud object gateway": "mcg",
    "noobaa-nc": "mcg",
    "rook": "rook",
    "csi-driver": "ceph-csi",
    "csi-addons": "ceph-csi",
    "ocs-operator": "ocs-operator",
    "ocs-client-operator": "ocs-operator",
    "odf-operator": "odf-operator",
    "odf-dr/ramen": "disaster-recovery",
    "multicluster-orchestrator": "disaster-recovery",
    "build": "rook",
    "ceph/rados/x86": "rook",
    "ceph/rados/z": "rook",
    "ceph-monitoring": "monitoring",
    "odf-cli": "odf-cli",
    "documentation": "unknown",
    "must-gather": "must-gather",
}


def _normalize_component(raw: str) -> str:
    """Map DFBUGS component names to our standard component names."""
    return JIRA_COMPONENT_MAP.get(raw.lower(), raw)


def _guess_component(summary: str) -> str:
    s = summary.lower()
    for keyword, comp in [
        ("console", "odf-console"),
        ("noobaa", "mcg"),
        ("mcg", "mcg"),
        ("rgw", "mcg"),
        ("csi", "ceph-csi"),
        ("rook", "rook"),
        ("ceph", "rook"),
        ("ocs-operator", "ocs-operator"),
        ("odf-operator", "odf-operator"),
        ("ramen", "disaster-recovery"),
        ("rdr", "disaster-recovery"),
        ("mdr", "disaster-recovery"),
        ("monitor", "monitoring"),
        ("nfs", "nfs"),
        ("lvmo", "lvmo"),
        ("lvm", "lvmo"),
    ]:
        if keyword in s:
            return comp
    return "unknown"
