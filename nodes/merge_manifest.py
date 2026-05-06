"""Merge Manifest node -- combines Jira, errata, and git changes.

Uses the unified agent runner to reconcile and deduplicate changes from all
three sources, cross-referencing Jira tickets with git commits and errata
advisories.  Falls back to deterministic dedup when the agent is unavailable.
"""

from __future__ import annotations

import json
import logging

from core.agent_runner import run_node_json
from core.models import (
    Change,
    ChangeManifest,
    ChangeSource,
    ChangeType,
    CoverageSummary,
    Severity,
)
from core.state import InspectState

logger = logging.getLogger(__name__)


def merge_manifest(state: InspectState) -> dict:
    """Merge and deduplicate changes from all three sources.

    Returns a dict with the unified ``change_manifest``.
    """
    jira_changes = state.get("jira_changes") or []
    errata_changes = state.get("errata_changes") or []
    git_changes = state.get("git_changes") or []

    version = state.get("zstream_version", "")
    previous = state.get("previous_version", "")

    all_changes = [*jira_changes, *errata_changes, *git_changes]

    if not all_changes:
        logger.info("No changes from any source to merge")
        return {
            "change_manifest": ChangeManifest(
                zstream_version=version,
                previous_version=previous,
                changes=[],
                coverage_summary=CoverageSummary(total_changes=0),
            )
        }

    # If there's only one source, skip dedup
    source_count = sum(1 for src in [jira_changes, errata_changes, git_changes] if src)
    if source_count <= 1:
        logger.info("Only one source has changes, skipping dedup")
        merged = all_changes
    else:
        merged = _merge_without_llm(jira_changes, errata_changes, git_changes)

    # Build coverage summary
    by_component: dict[str, int] = {}
    for change in merged:
        comp = change.component
        by_component[comp] = by_component.get(comp, 0) + 1

    manifest = ChangeManifest(
        zstream_version=version,
        previous_version=previous,
        changes=merged,
        coverage_summary=CoverageSummary(
            total_changes=len(merged),
            by_component=by_component,
        ),
    )

    logger.info(
        "Merged %d changes into manifest (%d jira, %d errata, %d git -> %d merged)",
        len(all_changes),
        len(jira_changes),
        len(errata_changes),
        len(git_changes),
        len(merged),
    )

    return {"change_manifest": manifest}


# ------------------------------------------------------------------
# Agent-powered merge
# ------------------------------------------------------------------


def _merge_with_agent(
    jira_changes: list[Change],
    errata_changes: list[Change],
    git_changes: list[Change],
    version: str,
) -> list[Change]:
    """Use the agent runner to reconcile and deduplicate changes."""
    try:

        def changes_to_dicts(changes: list[Change]) -> list[dict]:
            return [c.model_dump(mode="json") for c in changes]

        input_data = {
            "jira_changes": changes_to_dicts(jira_changes),
            "errata_changes": changes_to_dicts(errata_changes),
            "git_changes": changes_to_dicts(git_changes),
        }

        prompt = (
            f"You are a change reconciliation expert for ODF (OpenShift Data "
            f"Foundation) z-stream releases.\n\n"
            f"Merge, deduplicate, and cross-reference these changes from three "
            f"sources into a unified manifest for version {version}.\n\n"
            f"Rules:\n"
            f"- Match Jira issues to git commits using commit messages, linked "
            f"  tickets, or file paths.\n"
            f"- Match errata bugs to Jira issues using bug IDs.\n"
            f"- Merge duplicate entries, preferring Jira data for metadata and "
            f"  git data for file changes.\n"
            f"- Preserve all unique changes even if they only appear in one "
            f"  source.\n\n"
            f"Input data:\n{json.dumps(input_data, indent=2)}\n\n"
            f"For each deduplicated change, output a JSON object with:\n"
            f'- "id": primary identifier (prefer Jira key, then errata ID, '
            f"  then git ID)\n"
            f'- "source": primary source ("jira", "errata", or "git")\n'
            f'- "component": the ODF component\n'
            f'- "type": "bugfix", "security", or "enhancement"\n'
            f'- "severity": "critical", "major", "minor", or "low"\n'
            f'- "summary": concise description\n'
            f'- "files_changed": merged list of changed files from all sources\n'
            f'- "linked_errata": errata advisory ID if any\n'
            f'- "linked_commits": list of associated commit hashes\n\n'
            f"Return ONLY a JSON array of merged change objects."
        )

        raw = run_node_json(prompt, "merge_manifest")

        if raw is None:
            logger.warning("Agent returned no parseable JSON for merge")
            return []

        return _parse_raw_changes(raw)

    except Exception as e:
        logger.error("Agent merge failed: %s", e)
        return []


# ------------------------------------------------------------------
# Deterministic fallback merge
# ------------------------------------------------------------------


def _merge_without_llm(
    jira_changes: list[Change],
    errata_changes: list[Change],
    git_changes: list[Change],
) -> list[Change]:
    """Deterministic fallback merge when agent is unavailable.

    Strategy:
    - Start with Jira changes as the base.
    - For each errata change, try to match by bug ID; if matched, merge metadata.
    - For each git change, try to match by commit hash or component; if matched, add files.
    - Add any unmatched changes from errata/git as new entries.
    """
    merged: dict[str, Change] = {}

    # Start with Jira
    for change in jira_changes:
        merged[change.id] = change.model_copy()

    # Merge errata
    for errata in errata_changes:
        matched = False
        for key, existing in merged.items():
            if errata.linked_errata and existing.linked_errata == errata.linked_errata:
                matched = True
                break
            if errata.id.startswith("BZ-"):
                bug_num = errata.id.replace("BZ-", "")
                if bug_num in existing.id or bug_num in existing.summary:
                    matched = True
                    if errata.linked_errata:
                        existing.linked_errata = errata.linked_errata
                    break
        if not matched:
            merged[errata.id] = errata.model_copy()

    # Merge git
    for git_change in git_changes:
        matched = False
        for key, existing in merged.items():
            if existing.component == git_change.component:
                all_files = list(set(existing.files_changed + git_change.files_changed))
                all_commits = list(set(existing.linked_commits + git_change.linked_commits))
                existing.files_changed = all_files
                existing.linked_commits = all_commits
                matched = True
                break
        if not matched:
            merged[git_change.id] = git_change.model_copy()

    return list(merged.values())


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
                    source=_safe_enum(ChangeSource, item.get("source", "jira"), ChangeSource.JIRA),
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
            logger.warning("Failed to parse merged change: %s", e)
    return changes


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
