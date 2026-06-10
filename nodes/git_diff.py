"""PR Analyzer node -- fetches PR diffs and uses AI to understand the changes.

1. Fetches changed files from GitHub PRs (deterministic)
2. Passes the diff + patch snippets to Claude Code to extract:
   - What was actually fixed (semantic summary)
   - Which ODF features/subsystems are affected
   - Keywords that map to test areas
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.agent_runner import run_node_json
from core.models import Change, ChangeSource, ChangeType, Severity, StageError
from core.state import InspectState

logger = logging.getLogger(__name__)


def git_diff(state: InspectState) -> dict:
    """Fetch PR diffs and analyze them with AI."""
    jira_changes = state.get("jira_changes") or []

    pr_urls = []
    for change in jira_changes:
        for url in change.linked_commits:
            if "github.com" in url and "/pull/" in url and url not in pr_urls:
                pr_urls.append(url)

    if not pr_urls:
        logger.info("No PR URLs found, skipping PR analysis")
        return {"git_changes": []}

    print(f"  [PR Analyzer] Fetching diffs for {len(pr_urls)} PRs...", flush=True)

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
    for idx, pr_url in enumerate(pr_urls, 1):
        pr_short = "/".join(pr_url.split("/")[-4:]) if "github.com" in pr_url else pr_url
        print(f"    [{idx}/{len(pr_urls)}] {pr_short}", flush=True)
        try:
            raw = github_get_pr_files(pr_url)
            data = json.loads(raw) if isinstance(raw, str) else raw

            if "error" in data:
                print(f"      ERROR: {data['error'][:80]}", flush=True)
                continue

            repo = data.get("repo", "")
            pr_title = data.get("title", "")
            pr_number = data.get("pr_number", "")
            files = data.get("files", [])
            filenames = [f["filename"] for f in files]
            component = _repo_to_component(repo)

            # AI analysis of the PR diff
            try:
                ai_analysis = _analyze_pr_with_ai(
                    pr_url=pr_url,
                    pr_title=pr_title,
                    repo=repo,
                    files=files,
                    component=component,
                )
            except Exception as ai_err:
                print(f"      AI failed: {ai_err}", flush=True)
                ai_analysis = {}

            summary = ai_analysis.get("summary", f"[{repo}] {pr_title}")
            affected_features = ai_analysis.get("affected_features", [])
            test_keywords = ai_analysis.get("test_keywords", [])

            # Enrich the summary with AI insights
            if affected_features:
                summary = f"{summary} | affects: {', '.join(affected_features[:5])}"

            # Add AI-extracted keywords to filenames for downstream scoring
            enriched_files = filenames + [f"__keyword__{kw}" for kw in test_keywords]

            changes.append(
                Change(
                    id=f"PR-{repo}-{pr_number}",
                    source=ChangeSource.GIT,
                    component=component,
                    type=ChangeType.BUGFIX,
                    severity=Severity.MAJOR,
                    summary=summary,
                    files_changed=enriched_files,
                    linked_errata=None,
                    linked_commits=[pr_url],
                )
            )

            logger.info(
                "  PR #%s (%s): %d files, AI keywords: %s",
                pr_number,
                repo,
                len(filenames),
                test_keywords[:5],
            )

        except Exception as e:
            print(f"      ERROR: {e}", flush=True)

    print(f"  [PR Analyzer] Analyzed {len(pr_urls)} PRs -> {len(changes)} changes", flush=True)
    return {"git_changes": changes}


def _analyze_pr_with_ai(
    pr_url: str,
    pr_title: str,
    repo: str,
    files: list[dict],
    component: str,
) -> dict:
    """Use Claude Code to understand what a PR actually changes.

    Returns:
        {
            "summary": "Fixed RBAC permissions for CSI nodeplugin to watch secrets",
            "affected_features": ["pv_creation", "encryption", "secret_management"],
            "test_keywords": ["rbac", "secret", "nodeplugin", "pvc", "encryption"],
            "severity_assessment": "medium"
        }
    """
    # Build a concise diff summary for the AI
    diff_summary = []
    for f in files[:15]:
        patch = f.get("patch", "")
        if patch:
            patch_lines = patch.split("\n")[:20]
            patch_preview = "\n".join(patch_lines)
        else:
            patch_preview = "(binary or too large)"
        diff_summary.append(
            f"--- {f['filename']} ({f.get('status', '?')}, "
            f"+{f.get('additions', 0)}/-{f.get('deletions', 0)})\n"
            f"{patch_preview}"
        )

    diff_text = "\n\n".join(diff_summary)

    prompt = (
        f"You are analyzing a GitHub PR for ODF (OpenShift Data Foundation) "
        f"z-stream test selection.\n\n"
        f"PR: {pr_url}\n"
        f"Title: {pr_title}\n"
        f"Repo: {repo}\n"
        f"Component: {component}\n"
        f"Files changed: {len(files)}\n\n"
        f"Diff:\n{diff_text}\n\n"
        f"Based on this PR, return a JSON object with:\n"
        f'- "summary": one-line description of what this PR actually fixes/changes\n'
        f'- "affected_features": list of ODF features affected (e.g. '
        f'"pvc_creation", "snapshot", "encryption", "bucket_replication", '
        f'"failover", "monitoring", "osd_recovery", "upgrade")\n'
        f'- "test_keywords": list of words that should match relevant test '
        f"names (e.g. if PR fixes volume attach, keywords would be "
        f'"volume", "attach", "pvc", "mount", "csi")\n'
        f'- "severity_assessment": "critical", "major", "minor" based on '
        f"the scope of the change\n\n"
        f"Return ONLY the JSON object."
    )

    try:
        result = run_node_json(
            prompt,
            "git_diff",
            timeout_seconds=60,
        )

        if result and isinstance(result, dict):
            logger.info(
                "AI analysis for PR %s: %s", pr_url.split("/")[-1], result.get("summary", "")[:80]
            )
            return result

    except Exception as e:
        logger.warning("AI analysis failed for %s: %s", pr_url, e)

    # Fallback: extract keywords from filenames and title
    return _fallback_analysis(pr_title, files, component)


def _fallback_analysis(
    pr_title: str,
    files: list[dict],
    component: str,
) -> dict:
    """Deterministic fallback when AI is unavailable."""
    keywords = set()
    for f in files:
        for segment in f["filename"].replace("/", " ").replace("-", " ").replace("_", " ").split():
            word = segment.split(".")[0].lower()
            if len(word) > 2 and word.isalpha():
                keywords.add(word)

    for word in pr_title.lower().replace("[", " ").replace("]", " ").split():
        if len(word) > 3 and word.isalpha():
            keywords.add(word)

    return {
        "summary": pr_title,
        "affected_features": [],
        "test_keywords": sorted(keywords)[:20],
        "severity_assessment": "minor",
    }


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
        "ceph-csi-operator": "ceph-csi",
    }
    for key, comp in mapping.items():
        if key in repo_lower:
            return comp
    return repo
