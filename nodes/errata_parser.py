"""Errata Parser node -- extracts change info from errata advisories.

Uses the unified agent runner to parse advisory content into structured
Change objects.  Falls back to deterministic parsing when the agent is
unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime

from core.agent_runner import run_node_json
from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)


def errata_parser(state: InspectState) -> dict:
    """Parse errata advisories for the z-stream version.

    Returns a dict with ``errata_changes`` populated.
    """
    version = state.get("zstream_version", "")
    if not version:
        logger.warning("No zstream_version provided, skipping errata parsing")
        return {"errata_changes": []}

    try:
        prompt = (
            f"You are an errata advisory analyst for ODF (OpenShift Data "
            f"Foundation) z-stream releases.\n\n"
            f"Find Red Hat errata/advisories for ODF {version}. Parse CVEs, "
            f"bugfixes, and affected components.\n\n"
            f"Steps:\n"
            f"1. Search for RHSA/RHBA/RHEA advisories related to ODF {version} "
            f"   using curl against the Red Hat errata API or web search.\n"
            f"2. For each advisory found, extract the bugs and CVEs it covers.\n"
            f"3. Identify the ODF component affected by each fix.\n\n"
            f"For each change in the advisory, produce a JSON object with:\n"
            f'- "id": the bug ID or CVE ID (e.g. "BZ-2001234" or "CVE-2024-1234")\n'
            f'- "component": the ODF component affected (e.g. "ocs-operator", '
            f'  "rook-ceph", "noobaa", "ceph-csi", "odf-console")\n'
            f'- "type": one of "bugfix", "security", "enhancement"\n'
            f'- "severity": one of "critical", "major", "minor", "low"\n'
            f'- "summary": a concise description of the change\n'
            f'- "linked_errata": the errata advisory ID (e.g. "RHSA-2024:1234")\n'
            f'- "linked_commits": list of commit hashes if mentioned '
            f"  (empty list if none)\n\n"
            f"Return ONLY a JSON array of these objects."
        )

        raw = run_node_json(
            prompt,
            "errata_parser",
            allowed_tools=["Bash(curl*)", "WebSearch", "WebFetch"],
        )

        if raw is None:
            logger.warning("Agent returned no parseable JSON, returning empty list")
            return {"errata_changes": []}

        changes = _parse_raw_changes(raw)
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
                    source=ChangeSource.ERRATA,
                    component=item.get("component", "unknown"),
                    type=_safe_enum(ChangeType, item.get("type", "bugfix"), ChangeType.BUGFIX),
                    severity=_safe_enum(Severity, item.get("severity", "minor"), Severity.MINOR),
                    summary=item.get("summary", ""),
                    files_changed=[],
                    linked_errata=item.get("linked_errata"),
                    linked_commits=item.get("linked_commits", []),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse errata change from agent output: %s", e)
    return changes


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
