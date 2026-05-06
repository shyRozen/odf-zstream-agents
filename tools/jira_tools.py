"""Jira Cloud REST API tools for ODF z-stream issue tracking."""

from __future__ import annotations

import json

import httpx
from langchain_core.tools import tool

from core import config


def _jira_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _jira_auth() -> tuple[str, str] | None:
    if config.JIRA_EMAIL and config.JIRA_API_TOKEN:
        return (config.JIRA_EMAIL, config.JIRA_API_TOKEN)
    return None


def _fix_version_name(version: str) -> str:
    """Build fixVersion string. If version doesn't start with 'odf-', prefix it."""
    if version.startswith("odf-"):
        return version
    return f"odf-{version}"


def _fetch_remote_links(issue_key: str) -> list[str]:
    """Fetch GitHub PR URLs from an issue's remote links."""
    auth = _jira_auth()
    if not auth or not config.JIRA_URL:
        return []
    url = f"{config.JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}/remotelink"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=_jira_headers(), auth=auth)
            resp.raise_for_status()
            links = resp.json()
        return [
            link["object"]["url"]
            for link in links
            if "github.com" in link.get("object", {}).get("url", "")
            and "/pull/" in link.get("object", {}).get("url", "")
        ]
    except Exception:
        return []


def jira_search(version: str, project: str = "DFBUGS") -> str:
    """Search Jira for issues with a given fixVersion in a project.

    Queries the Jira Cloud REST API v3 (POST /search/jql) for all issues
    matching the specified fix version and project. The version is
    automatically prefixed with ``odf-`` if needed (e.g. "4.17.2" becomes
    "odf-4.17.2").

    Args:
        version: The ODF version (e.g. "4.17.2" or "odf-4.17.2").
        project: The Jira project key. Defaults to "DFBUGS".

    Returns:
        JSON string with a list of matching issues, or an error message.
    """
    if not config.JIRA_URL:
        return json.dumps({"error": "JIRA_URL not configured"})

    auth = _jira_auth()
    if auth is None:
        return json.dumps({"error": "JIRA_EMAIL or JIRA_API_TOKEN not configured"})

    fix_ver = _fix_version_name(version)
    jql = f'project = "{project}" AND fixVersion = "{fix_ver}" ' f"ORDER BY priority DESC"
    url = f"{config.JIRA_URL.rstrip('/')}/rest/api/3/search/jql"
    payload = {
        "jql": jql,
        "maxResults": 200,
        "fields": [
            "summary",
            "status",
            "priority",
            "components",
            "labels",
            "fixVersions",
            "issuetype",
            "assignee",
        ],
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=_jira_headers(), auth=auth)
            resp.raise_for_status()
            data = resp.json()

        issues = []
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            key = issue["key"]
            pr_urls = _fetch_remote_links(key)
            issues.append(
                {
                    "key": key,
                    "summary": fields.get("summary", ""),
                    "status": fields.get("status", {}).get("name", ""),
                    "priority": fields.get("priority", {}).get("name", ""),
                    "issuetype": fields.get("issuetype", {}).get("name", ""),
                    "components": [c.get("name", "") for c in fields.get("components", [])],
                    "labels": fields.get("labels", []),
                    "fixVersions": [v.get("name", "") for v in fields.get("fixVersions", [])],
                    "pr_urls": pr_urls,
                    "assignee": (
                        fields.get("assignee", {}).get("displayName", "Unassigned")
                        if fields.get("assignee")
                        else "Unassigned"
                    ),
                }
            )

        return json.dumps(
            {
                "total": data.get("total", 0) or len(issues),
                "issues": issues,
            },
            indent=2,
        )

    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {"error": f"Jira API error {exc.response.status_code}: {exc.response.text[:500]}"}
        )
    except Exception as exc:
        return json.dumps({"error": f"Jira request failed: {str(exc)}"})


def jira_get_issue(issue_key: str) -> str:
    """Get detailed information for a single Jira issue.

    Fetches the full issue details including summary, description, status,
    priority, components, labels, fix versions, comments, and linked issues.

    Args:
        issue_key: The Jira issue key (e.g. "ODF-1234").

    Returns:
        JSON string with the issue details, or an error message.
    """
    if not config.JIRA_URL:
        return json.dumps({"error": "JIRA_URL not configured"})

    auth = _jira_auth()
    if auth is None:
        return json.dumps({"error": "JIRA_EMAIL or JIRA_API_TOKEN not configured"})

    url = f"{config.JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    params = {
        "fields": "summary,description,status,priority,components,labels,fixVersions,"
        "issuetype,assignee,comment,issuelinks,created,updated,resolution",
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, params=params, headers=_jira_headers(), auth=auth)
            resp.raise_for_status()
            data = resp.json()

        fields = data.get("fields", {})

        # Extract description text from Atlassian Document Format
        description_adf = fields.get("description")
        description_text = ""
        if description_adf and isinstance(description_adf, dict):
            for block in description_adf.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        description_text += inline.get("text", "")
                description_text += "\n"
        elif isinstance(description_adf, str):
            description_text = description_adf

        # Extract comments
        comments = []
        for c in fields.get("comment", {}).get("comments", []):
            body = c.get("body", "")
            if isinstance(body, dict):
                text_parts = []
                for block in body.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            text_parts.append(inline.get("text", ""))
                body = " ".join(text_parts)
            comments.append(
                {
                    "author": c.get("author", {}).get("displayName", ""),
                    "created": c.get("created", ""),
                    "body": body[:500],
                }
            )

        # Extract linked issues
        links = []
        for link in fields.get("issuelinks", []):
            link_info = {}
            if "outwardIssue" in link:
                link_info = {
                    "type": link.get("type", {}).get("outward", ""),
                    "key": link["outwardIssue"].get("key", ""),
                    "summary": link["outwardIssue"].get("fields", {}).get("summary", ""),
                }
            elif "inwardIssue" in link:
                link_info = {
                    "type": link.get("type", {}).get("inward", ""),
                    "key": link["inwardIssue"].get("key", ""),
                    "summary": link["inwardIssue"].get("fields", {}).get("summary", ""),
                }
            if link_info:
                links.append(link_info)

        result = {
            "key": data.get("key", ""),
            "summary": fields.get("summary", ""),
            "description": description_text.strip(),
            "status": fields.get("status", {}).get("name", ""),
            "priority": fields.get("priority", {}).get("name", ""),
            "issuetype": fields.get("issuetype", {}).get("name", ""),
            "resolution": (
                fields.get("resolution", {}).get("name", "") if fields.get("resolution") else None
            ),
            "components": [c.get("name", "") for c in fields.get("components", [])],
            "labels": fields.get("labels", []),
            "fixVersions": [v.get("name", "") for v in fields.get("fixVersions", [])],
            "assignee": (
                fields.get("assignee", {}).get("displayName", "Unassigned")
                if fields.get("assignee")
                else "Unassigned"
            ),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "comments": comments[-5:],  # Last 5 comments
            "linked_issues": links,
        }

        return json.dumps(result, indent=2)

    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {"error": f"Jira API error {exc.response.status_code}: {exc.response.text[:500]}"}
        )
    except Exception as exc:
        return json.dumps({"error": f"Jira request failed: {str(exc)}"})


def jira_create_bug(summary: str, description: str, component: str, labels: str = "") -> str:
    """Create a new Bug issue in Jira for the ODF project.

    Files a bug with the given summary, description, and component.
    Optionally attaches labels (comma-separated).

    Args:
        summary: One-line summary of the bug.
        description: Detailed bug description.
        component: The ODF component name (e.g. "ceph-csi", "mcg").
        labels: Comma-separated labels to attach (e.g. "zstream,regression").

    Returns:
        JSON string with the created issue key and URL, or an error message.
    """
    if not config.JIRA_URL:
        return json.dumps({"error": "JIRA_URL not configured"})

    auth = _jira_auth()
    if auth is None:
        return json.dumps({"error": "JIRA_EMAIL or JIRA_API_TOKEN not configured"})

    url = f"{config.JIRA_URL.rstrip('/')}/rest/api/3/issue"

    label_list = [item.strip() for item in labels.split(",") if item.strip()] if labels else []

    payload = {
        "fields": {
            "project": {"key": config.JIRA_PROJECT_KEY},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": description,
                            }
                        ],
                    }
                ],
            },
            "issuetype": {"name": "Bug"},
            "components": [{"name": component}],
            "labels": label_list,
        }
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=_jira_headers(), auth=auth)
            resp.raise_for_status()
            data = resp.json()

        issue_key = data.get("key", "")
        issue_url = f"{config.JIRA_URL.rstrip('/')}/browse/{issue_key}"

        return json.dumps(
            {
                "key": issue_key,
                "url": issue_url,
                "message": f"Bug {issue_key} created successfully",
            },
            indent=2,
        )

    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {"error": f"Jira API error {exc.response.status_code}: {exc.response.text[:500]}"}
        )
    except Exception as exc:
        return json.dumps({"error": f"Failed to create bug: {str(exc)}"})


# Tool-wrapped versions for LangGraph ReAct agents
jira_search_tool = tool(jira_search)
jira_get_issue_tool = tool(jira_get_issue)
jira_create_bug_tool = tool(jira_create_bug)
