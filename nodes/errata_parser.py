"""Errata Parser node — extracts change info from errata advisories.

Uses Sonnet to parse advisory content into structured Change objects.
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
You are an errata advisory analyst for ODF (OpenShift Data Foundation) z-stream releases.

Given errata advisory data, extract structured change information for each bug/CVE fixed.

For each change in the advisory, output a JSON object with these fields:
- id: the bug ID or CVE ID (e.g., "BZ-2001234" or "CVE-2024-1234")
- component: the ODF component affected (e.g., "ocs-operator", "rook-ceph", "noobaa", "ceph-csi", "odf-console")
- type: one of "bugfix", "security", "enhancement"
- severity: one of "critical", "major", "minor", "low"
- summary: a concise description of the change
- linked_errata: the errata advisory ID (e.g., "RHSA-2024:1234")
- linked_commits: list of commit hashes if mentioned (empty list if none)

Output ONLY a JSON array of these objects, no other text.
"""


def errata_parser(state: InspectState) -> dict:
    """Parse errata advisories for the z-stream version.

    Returns a dict with ``errata_changes`` populated.
    """
    version = state.get("zstream_version", "")
    if not version:
        logger.warning("No zstream_version provided, skipping errata parsing")
        return {"errata_changes": []}

    try:
        from tools.errata_tools import errata_fetch
    except ImportError:
        logger.warning("errata_tools not available, skipping errata parsing")
        return {
            "errata_changes": [],
            "errors": [
                StageError(
                    stage="errata_parser",
                    error="errata_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    try:
        # Fetch errata advisory for this version
        advisory_data = errata_fetch(version)

        if not advisory_data:
            logger.info("No errata advisory found for version %s", version)
            return {"errata_changes": []}

        # Use LLM to extract structured changes from advisory
        llm = get_llm("errata_parser")
        if llm is None:
            logger.warning("No LLM available for errata_parser, using raw parsing")
            return {"errata_changes": _parse_advisory_without_llm(advisory_data)}

        advisory_text = json.dumps(advisory_data, indent=2, default=str)
        prompt = (
            f"Extract change information from this errata advisory for "
            f"ODF z-stream version {version}:\n\n{advisory_text}"
        )

        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        changes = _parse_llm_response(response.content)
        logger.info("Extracted %d changes from errata", len(changes))
        return {"errata_changes": changes}

    except Exception as e:
        logger.error("Errata parsing failed: %s", e)
        return {
            "errata_changes": [],
            "errors": [
                StageError(
                    stage="errata_parser",
                    error=f"Errata parsing failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _parse_llm_response(content: str) -> list[Change]:
    """Parse the LLM JSON response into Change objects."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
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
                source=ChangeSource.ERRATA,
                component=raw.get("component", "unknown"),
                type=_safe_enum(ChangeType, raw.get("type", "bugfix"), ChangeType.BUGFIX),
                severity=_safe_enum(Severity, raw.get("severity", "minor"), Severity.MINOR),
                summary=raw.get("summary", ""),
                files_changed=[],
                linked_errata=raw.get("linked_errata"),
                linked_commits=raw.get("linked_commits", []),
            )
            changes.append(change)
        except Exception as e:
            logger.warning("Failed to parse errata change from LLM output: %s", e)

    return changes


def _parse_advisory_without_llm(advisory_data: dict | list) -> list[Change]:
    """Fallback parser when no LLM is available."""
    changes = []

    # Handle advisory as a dict with bugs/cves lists
    if isinstance(advisory_data, dict):
        advisory_id = advisory_data.get("advisory_id", advisory_data.get("id", ""))
        bugs = advisory_data.get("bugs", [])
        cves = advisory_data.get("cves", [])

        for bug in bugs:
            bug_id = bug.get("id", bug.get("bug_id", "")) if isinstance(bug, dict) else str(bug)
            summary = bug.get("summary", "") if isinstance(bug, dict) else ""
            component = bug.get("component", "unknown") if isinstance(bug, dict) else "unknown"
            changes.append(
                Change(
                    id=f"BZ-{bug_id}" if not str(bug_id).startswith("BZ-") else str(bug_id),
                    source=ChangeSource.ERRATA,
                    component=component,
                    type=ChangeType.BUGFIX,
                    severity=Severity.MINOR,
                    summary=summary,
                    linked_errata=str(advisory_id) if advisory_id else None,
                )
            )

        for cve in cves:
            cve_id = cve.get("id", cve.get("cve_id", "")) if isinstance(cve, dict) else str(cve)
            summary = cve.get("summary", "") if isinstance(cve, dict) else ""
            severity_str = cve.get("severity", "minor") if isinstance(cve, dict) else "minor"
            changes.append(
                Change(
                    id=str(cve_id),
                    source=ChangeSource.ERRATA,
                    component="unknown",
                    type=ChangeType.SECURITY,
                    severity=_safe_enum(Severity, severity_str, Severity.MAJOR),
                    summary=summary,
                    linked_errata=str(advisory_id) if advisory_id else None,
                )
            )
    elif isinstance(advisory_data, list):
        for item in advisory_data:
            if isinstance(item, dict):
                changes.extend(_parse_advisory_without_llm(item))

    return changes


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
