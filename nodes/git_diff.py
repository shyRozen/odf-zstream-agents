"""PR Analyzer node -- fetches changed files from GitHub PRs.

Reads PR URLs from jira_changes (stored in linked_commits field),
calls GitHub API to get the diff, and produces git_changes with
actual file paths from the PRs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)


def git_diff(state: InspectState) -> dict:
    """Fetch changed files from all PRs linked in jira_changes."""
    jira_changes = state.get("jira_changes") or []

    # Collect all unique PR URLs from jira changes
    pr_urls = []
    for change in jira_changes:
        for url in change.linked_commits:
            if "github.com" in url and "/pull/" in url and url not in pr_urls:
                pr_urls.append(url)

    if not pr_urls:
        logger.info("No PR URLs found in jira changes, skipping PR analysis")
        return {"git_changes": []}

    logger.info("Fetching diffs for %d PRs", len(pr_urls))

    try:
        from tools.github_tools import github_get_pr_files
    except ImportError:
        return {
            "git_changes": [],
            "errors": [
                StageError(
                    stage="git_diff",
                    error="github_tools not available",
                    timestamp=datetime.utcnow().isoformat(),
                    recoverable=True,
                )
            ],
        }

    changes = []
    for pr_url in pr_urls:
        try:
            raw = github_get_pr_files(pr_url)
            data = json.loads(raw) if isinstance(raw, str) else raw

            if "error" in data:
                logger.warning("Failed to fetch PR %s: %s", pr_url, data["error"])
                continue

            repo = data.get("repo", "")
            pr_title = data.get("title", "")
            files = data.get("files", [])
            filenames = [f["filename"] for f in files]

            component = _repo_to_component(repo)

            changes.append(
                Change(
                    id=f"PR-{repo}-{data.get('pr_number', '')}",
                    source=ChangeSource.GIT,
                    component=component,
                    type=ChangeType.BUGFIX,
                    severity=Severity.MAJOR,
                    summary=f"[{repo}] {pr_title}",
                    files_changed=filenames,
                    linked_errata=None,
                    linked_commits=[pr_url],
                )
            )

            logger.info(
                "  PR %s: %d files changed in %s",
                pr_url.split("/")[-1],
                len(filenames),
                repo,
            )

        except Exception as e:
            logger.warning("Error fetching PR %s: %s", pr_url, e)

    logger.info("Analyzed %d PRs → %d changes", len(pr_urls), len(changes))
    return {"git_changes": changes}


def _repo_to_component(repo: str) -> str:
    """Map GitHub repo to ODF component name."""
    repo_lower = repo.lower()
    mapping = {
        "odf-console": "odf-console",
        "rook": "rook",
        "ceph-csi": "ceph-csi",
        "noobaa-core": "mcg",
        "noobaa-operator": "mcg",
        "ramen": "disaster-recovery",
        "odf-multicluster-orchestrator": "disaster-recovery",
        "ocs-operator": "ocs-operator",
        "ocs-client-operator": "ocs-operator",
        "odf-operator": "odf-operator",
        "odf-must-gather": "must-gather",
        "kubernetes-csi-addons": "ceph-csi",
    }
    for key, component in mapping.items():
        if key in repo_lower:
            return component
    return repo
