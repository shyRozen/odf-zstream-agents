"""Merge Manifest node — combines Jira, errata, and git changes.

Uses Sonnet to reconcile and deduplicate changes from all three sources,
cross-referencing Jira tickets with git commits and errata advisories.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from core.models import (
    Change,
    ChangeManifest,
    CoverageSummary,
    StageError,
)
from core.state import InspectState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a change reconciliation expert for ODF (OpenShift Data Foundation) z-stream releases.

You are given changes from three sources:
1. Jira issues (bug fixes, security fixes, enhancements)
2. Errata advisories (official Red Hat advisories)
3. Git diffs (file-level code changes)

Your job is to reconcile and deduplicate these changes:
- Match Jira issues to git commits using commit messages, linked tickets, or file paths.
- Match errata bugs to Jira issues using bug IDs.
- Merge duplicate entries, preferring Jira data for metadata and git data for file changes.
- Preserve all unique changes even if they only appear in one source.

For each deduplicated change, output a JSON object with:
- id: the primary identifier (prefer Jira key, then errata ID, then git ID)
- source: the primary source ("jira", "errata", or "git")
- component: the ODF component
- type: "bugfix", "security", or "enhancement"
- severity: "critical", "major", "minor", or "low"
- summary: concise description
- files_changed: merged list of changed files from all sources
- linked_errata: errata advisory ID if any
- linked_commits: list of associated commit hashes

Output ONLY a JSON array of the merged change objects, no other text.
"""


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
        merged = _merge_with_llm(jira_changes, errata_changes, git_changes, version)
        if not merged:
            # Fallback: simple dedup if LLM fails
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


def _merge_with_llm(
    jira_changes: list[Change],
    errata_changes: list[Change],
    git_changes: list[Change],
    version: str,
) -> list[Change]:
    """Use LLM to reconcile and deduplicate changes."""
    llm = get_llm("merge_manifest")
    if llm is None:
        logger.warning("No LLM available for merge_manifest")
        return []

    try:
        def changes_to_dicts(changes: list[Change]) -> list[dict]:
            return [c.model_dump(mode="json") for c in changes]

        input_data = {
            "jira_changes": changes_to_dicts(jira_changes),
            "errata_changes": changes_to_dicts(errata_changes),
            "git_changes": changes_to_dicts(git_changes),
        }

        prompt = (
            f"Reconcile and deduplicate these changes for ODF z-stream "
            f"version {version}:\n\n{json.dumps(input_data, indent=2)}"
        )

        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        return _parse_llm_response(response.content)

    except Exception as e:
        logger.error("LLM merge failed: %s", e)
        return []


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
        logger.error("Failed to parse merge LLM response as JSON")
        return []

    from core.models import ChangeSource, ChangeType, Severity

    changes = []
    for raw in raw_changes:
        try:
            changes.append(
                Change(
                    id=raw.get("id", "UNKNOWN"),
                    source=_safe_enum(ChangeSource, raw.get("source", "jira"), ChangeSource.JIRA),
                    component=raw.get("component", "unknown"),
                    type=_safe_enum(ChangeType, raw.get("type", "bugfix"), ChangeType.BUGFIX),
                    severity=_safe_enum(Severity, raw.get("severity", "minor"), Severity.MINOR),
                    summary=raw.get("summary", ""),
                    files_changed=raw.get("files_changed", []),
                    linked_errata=raw.get("linked_errata"),
                    linked_commits=raw.get("linked_commits", []),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse merged change: %s", e)

    return changes


def _merge_without_llm(
    jira_changes: list[Change],
    errata_changes: list[Change],
    git_changes: list[Change],
) -> list[Change]:
    """Deterministic fallback merge when LLM is unavailable.

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
            # Match by linked errata or ID overlap
            if errata.linked_errata and existing.linked_errata == errata.linked_errata:
                matched = True
                break
            # Check if errata ID contains a bug number that matches
            if errata.id.startswith("BZ-"):
                bug_num = errata.id.replace("BZ-", "")
                if bug_num in existing.id or bug_num in existing.summary:
                    matched = True
                    # Merge errata link
                    if errata.linked_errata:
                        existing.linked_errata = errata.linked_errata
                    break
        if not matched:
            merged[errata.id] = errata.model_copy()

    # Merge git
    for git_change in git_changes:
        matched = False
        for key, existing in merged.items():
            # Match by component
            if existing.component == git_change.component:
                # Merge file changes and commits
                all_files = list(set(existing.files_changed + git_change.files_changed))
                all_commits = list(set(existing.linked_commits + git_change.linked_commits))
                existing.files_changed = all_files
                existing.linked_commits = all_commits
                matched = True
                break
        if not matched:
            merged[git_change.id] = git_change.model_copy()

    return list(merged.values())


def _safe_enum(enum_cls, value: str, default):
    """Safely convert a string to an enum value."""
    try:
        return enum_cls(value.lower())
    except (ValueError, AttributeError):
        return default
