"""Red Hat Errata advisory tools for ODF z-stream change ingestion."""

from __future__ import annotations

import json
import re

import httpx
from langchain_core.tools import tool


# Red Hat public errata API base URL
ERRATA_API_BASE = "https://errata.devel.redhat.com/api/v1"
# Public advisory listing (no auth required for released advisories)
PUBLIC_ERRATA_URL = "https://access.redhat.com/errata"


def errata_fetch(version: str) -> str:
    """Fetch errata advisories related to an ODF z-stream version.

    Queries the Red Hat public errata API for advisories matching the given
    ODF version. Returns advisory metadata including synopsis, type, severity,
    and associated CVEs and bugs.

    If the errata API is unreachable, returns a structured placeholder that
    downstream agents can use to continue processing.

    Args:
        version: The ODF version string (e.g. "4.16.1").

    Returns:
        JSON string with a list of advisories, or a structured fallback.
    """
    # Construct search terms for ODF/OCS errata
    product_name = "Red Hat OpenShift Data Foundation"
    search_version = version

    # Try the public errata API first
    try:
        url = f"{ERRATA_API_BASE}/erratum"
        params = {
            "filter[product]": product_name,
            "filter[release]": search_version,
            "per_page": 50,
        }

        with httpx.Client(timeout=30, verify=True) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        advisories = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            advisories.append({
                "id": item.get("id", ""),
                "advisory_name": attrs.get("advisory_name", ""),
                "synopsis": attrs.get("synopsis", ""),
                "type": attrs.get("errata_type", ""),
                "severity": attrs.get("severity", ""),
                "status": attrs.get("status", ""),
                "release_date": attrs.get("actual_ship_date", ""),
                "cves": attrs.get("cves", []),
                "bugs": attrs.get("bugs", []),
            })

        return json.dumps({
            "version": version,
            "source": "errata_api",
            "total": len(advisories),
            "advisories": advisories,
        }, indent=2)

    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
        # Return a structured fallback so downstream agents can still proceed
        return json.dumps({
            "version": version,
            "source": "fallback",
            "total": 0,
            "advisories": [],
            "note": f"Errata API unavailable ({type(exc).__name__}: {str(exc)[:200]}). "
                    "Advisories could not be fetched. Proceed with Jira and git sources.",
        }, indent=2)
    except Exception as exc:
        return json.dumps({
            "version": version,
            "source": "error",
            "total": 0,
            "advisories": [],
            "error": f"Unexpected error fetching errata: {str(exc)}",
        }, indent=2)


def errata_parse(advisory_content: str) -> str:
    """Parse raw errata advisory content to extract CVEs, bugfixes, and components.

    Takes the text or JSON content of an errata advisory and extracts structured
    information including CVE identifiers, Bugzilla IDs, affected components,
    and the advisory type (security, bugfix, enhancement).

    Args:
        advisory_content: The raw advisory text or JSON string to parse.

    Returns:
        JSON string with extracted CVEs, bugs, components, and classification.
    """
    try:
        # Try parsing as JSON first
        try:
            data = json.loads(advisory_content)
        except (json.JSONDecodeError, TypeError):
            data = None

        cves: list[str] = []
        bugs: list[str] = []
        components: list[str] = []
        advisory_type = "unknown"
        severity = "unknown"
        synopsis = ""

        if isinstance(data, dict):
            # Structured advisory data
            cves = data.get("cves", [])
            bugs = [str(b) for b in data.get("bugs", [])]
            components = data.get("components", [])
            advisory_type = data.get("type", data.get("errata_type", "unknown"))
            severity = data.get("severity", "unknown")
            synopsis = data.get("synopsis", "")

            # Also scan synopsis for component hints
            content_to_scan = synopsis + " " + data.get("description", "")
        else:
            content_to_scan = advisory_content

        # Extract CVEs from text using regex
        cve_pattern = re.compile(r"CVE-\d{4}-\d{4,}")
        found_cves = cve_pattern.findall(content_to_scan)
        cves = list(set(cves + found_cves))

        # Extract Bugzilla IDs
        bz_pattern = re.compile(r"(?:BZ#?|bugzilla[:/]?\s*)(\d{6,})", re.IGNORECASE)
        found_bugs = bz_pattern.findall(content_to_scan)
        bugs = list(set(bugs + found_bugs))

        # Detect advisory type from content
        content_lower = content_to_scan.lower()
        if any(kw in content_lower for kw in ["cve-", "security", "vulnerability"]):
            advisory_type = "security"
        elif any(kw in content_lower for kw in ["bug fix", "bugfix", "bug-fix"]):
            advisory_type = "bugfix"
        elif any(kw in content_lower for kw in ["enhancement", "feature"]):
            advisory_type = "enhancement"

        # Detect severity
        severity_keywords = {
            "critical": ["critical"],
            "important": ["important"],
            "moderate": ["moderate"],
            "low": ["low"],
        }
        for sev, keywords in severity_keywords.items():
            if any(kw in content_lower for kw in keywords):
                severity = sev
                break

        # Extract ODF component names from content
        odf_components = [
            "ceph-csi", "mcg", "noobaa", "rgw", "rook",
            "ocs-operator", "odf-operator", "odf-console",
            "monitoring", "encryption", "lvmo", "lvm",
            "disaster-recovery", "nfs", "ui",
        ]
        for comp in odf_components:
            if comp in content_lower:
                components.append(comp)
        components = list(set(components))

        return json.dumps({
            "advisory_type": advisory_type,
            "severity": severity,
            "synopsis": synopsis,
            "cves": cves,
            "bugs": bugs,
            "components": components,
            "cve_count": len(cves),
            "bug_count": len(bugs),
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to parse advisory content: {str(exc)}"})


# Tool-wrapped versions for LangGraph ReAct agents
errata_fetch_tool = tool(errata_fetch)
errata_parse_tool = tool(errata_parse)
