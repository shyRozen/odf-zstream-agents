"""Git Diff node — extracts file-level changes between version tags.

NO LLM — deterministic node that calls git tools directly and maps
file paths to ODF components.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from core import config
from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)

# Map file path patterns to ODF component names
COMPONENT_PATH_MAP = {
    r"ocs_ci/ocs/ocp\.py": "ocp",
    r"ocs_ci/ocs/ocs\.py": "ocs-operator",
    r"ocs_ci/ocs/resources/storage_cluster\.py": "ocs-operator",
    r"ocs_ci/ocs/resources/ocs\.py": "ocs-operator",
    r"ocs_ci/ocs/resources/odf\.py": "odf-operator",
    r"ocs_ci/ocs/rook\.py": "rook-ceph",
    r"ocs_ci/ocs/resources/ceph\.py": "rook-ceph",
    r"ocs_ci/ocs/bucket_utils\.py": "noobaa",
    r"ocs_ci/ocs/resources/mcg\.py": "noobaa",
    r"ocs_ci/ocs/resources/noobaa\.py": "noobaa",
    r"ocs_ci/ocs/ui/": "odf-console",
    r"tests/functional/ui/": "odf-console",
    r"tests/.*noobaa": "noobaa",
    r"tests/.*mcg": "noobaa",
    r"tests/.*rgw": "noobaa",
    r"tests/.*ceph": "rook-ceph",
    r"tests/.*rook": "rook-ceph",
    r"tests/.*pv": "ceph-csi",
    r"tests/.*csi": "ceph-csi",
    r"ocs_ci/ocs/resources/pv": "ceph-csi",
    r"ocs_ci/deployment/": "deployment",
    r"ocs_ci/utility/": "utility",
}


def git_diff(state: InspectState) -> dict:
    """Run git diff between previous and current version tags.

    Returns a dict with ``git_changes`` populated.
    """
    version = state.get("zstream_version", "")
    previous = state.get("previous_version", "")

    if not version or not previous:
        logger.warning(
            "Missing version info (current=%s, previous=%s), skipping git diff",
            version,
            previous,
        )
        return {"git_changes": []}

    try:
        from tools.git_tools import git_diff_files, git_log_between
    except ImportError:
        logger.warning("git_tools not available, skipping git diff")
        return {
            "git_changes": [],
            "errors": [
                StageError(
                    stage="git_diff",
                    error="git_tools module not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    try:
        # Get file-level diff between versions
        diff_result = git_diff_files(config.OCS_CI_REPO_PATH, previous, version)
        diff_files = (
            json.loads(diff_result).get("files", [])
            if isinstance(diff_result, str)
            else diff_result
        )
        # Get commit log between versions
        log_result = git_log_between(config.OCS_CI_REPO_PATH, previous, version)
        commit_log = (
            json.loads(log_result).get("commits", []) if isinstance(log_result, str) else log_result
        )

        if not diff_files and not commit_log:
            logger.info("No git changes between %s and %s", previous, version)
            return {"git_changes": []}

        # Group changed files by component
        component_files: dict[str, list[str]] = {}
        for file_path in diff_files:
            component = _classify_file(file_path)
            component_files.setdefault(component, []).append(file_path)

        # Parse commit log to extract commit metadata
        commits = _parse_commit_log(commit_log)

        # Build Change objects — one per component group
        changes = []
        for component, files in component_files.items():
            # Find commits that touch this component's files
            related_commits = []
            related_jira_keys = set()
            for commit in commits:
                commit_files = commit.get("files", [])
                if any(f in files for f in commit_files):
                    related_commits.append(commit.get("hash", ""))
                # Also check if the commit message mentions these files
                msg = commit.get("message", "")
                if any(f.split("/")[-1] in msg for f in files):
                    related_commits.append(commit.get("hash", ""))
                # Extract Jira keys from commit messages
                jira_matches = re.findall(r"[A-Z]+-\d+", msg)
                related_jira_keys.update(jira_matches)

            related_commits = list(set(related_commits))

            # Determine change type from commit messages
            change_type = ChangeType.BUGFIX
            for commit in commits:
                msg = commit.get("message", "").lower()
                if "security" in msg or "cve" in msg:
                    change_type = ChangeType.SECURITY
                    break
                elif "feature" in msg or "enhancement" in msg:
                    change_type = ChangeType.ENHANCEMENT

            file_summary = ", ".join(files[:5])
            if len(files) > 5:
                file_summary += f" (and {len(files) - 5} more)"

            changes.append(
                Change(
                    id=f"git-{component}-{version}",
                    source=ChangeSource.GIT,
                    component=component,
                    type=change_type,
                    severity=_estimate_severity(files),
                    summary=f"Changes in {component}: {file_summary}",
                    files_changed=files,
                    linked_errata=None,
                    linked_commits=related_commits,
                )
            )

        logger.info(
            "Extracted %d component-level changes from %d changed files",
            len(changes),
            len(diff_files),
        )
        return {"git_changes": changes}

    except Exception as e:
        logger.error("Git diff failed: %s", e)
        return {
            "git_changes": [],
            "errors": [
                StageError(
                    stage="git_diff",
                    error=f"Git diff failed: {e}",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }


def _classify_file(file_path: str) -> str:
    """Classify a file path to an ODF component."""
    for pattern, component in COMPONENT_PATH_MAP.items():
        if re.search(pattern, file_path):
            return component
    # Fallback: use the top-level directory
    parts = file_path.split("/")
    if len(parts) >= 2:
        return parts[1] if parts[0] in ("ocs_ci", "tests") else parts[0]
    return "unknown"


def _estimate_severity(files: list[str]) -> Severity:
    """Estimate severity based on changed files."""
    critical_patterns = [
        r"deployment",
        r"upgrade",
        r"security",
        r"auth",
        r"crypt",
    ]
    major_patterns = [
        r"operator",
        r"controller",
        r"manager",
    ]

    for f in files:
        for pat in critical_patterns:
            if re.search(pat, f, re.IGNORECASE):
                return Severity.CRITICAL
    for f in files:
        for pat in major_patterns:
            if re.search(pat, f, re.IGNORECASE):
                return Severity.MAJOR
    return Severity.MINOR


def _parse_commit_log(commit_log: str | list | dict) -> list[dict]:
    """Parse git log output into structured commit data.

    Handles multiple formats: raw string log, list of dicts, or dict.
    """
    if isinstance(commit_log, list):
        return commit_log
    if isinstance(commit_log, dict):
        return [commit_log]
    if not isinstance(commit_log, str) or not commit_log.strip():
        return []

    commits = []
    # Parse standard git log --oneline or similar
    for line in commit_log.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Try to match "hash message" format
        parts = line.split(None, 1)
        if len(parts) >= 2:
            commits.append(
                {
                    "hash": parts[0],
                    "message": parts[1],
                    "files": [],
                }
            )
        elif len(parts) == 1:
            commits.append(
                {
                    "hash": parts[0],
                    "message": "",
                    "files": [],
                }
            )

    return commits
