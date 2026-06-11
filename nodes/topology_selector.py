"""Topology Selector node -- maps fixes to Jenkins deployment configs.

Parses Jira bug descriptions for platform and deployment type, then
uses AI to select the best Jenkins deployment configuration from the
full 152-config catalog extracted from ocs4-jenkins.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.agent_runner import run_node
from core.models import ChangeManifest, component_marker_name
from core.state import PipelineState

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parent.parent / "jenkins_deployment_catalog.json"

PLATFORM_PRIORITY = ["vsphere", "ibmcloud", "aws", "baremetal", "gcp", "azure", "rhv"]

DEPLOYMENT_TYPE_MAP = {
    "internal": "standard",
    "internal-attached": "lso",
    "lso": "lso",
    "external": "external",
    "external mode": "external",
    "multicluster": "standard",
    "provider": "provider_client",
    "provider-client": "provider_client",
    "consumer": "provider_client",
    "dr": "regional_dr",
    "regional dr": "regional_dr",
    "metro dr": "metro_dr",
    "stretch": "metro_dr",
    "arbiter": "metro_dr",
}


def topology_selector(state: PipelineState) -> dict:
    """Map fixes to Jenkins deployment configs using Jira data + AI."""
    manifest: ChangeManifest | None = state.get("change_manifest")
    version = state.get("zstream_version", "unknown")
    selected_tests = state.get("selected_tests") or []

    if not manifest or not manifest.changes:
        logger.warning("No changes to classify")
        return {"deployment_specs": []}

    parts = version.split(".")
    ocs_version = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version
    mark_name = f"zstream_{version.replace('.', '_').replace('-', '_')}"
    pr_number = state.get("pr_number", 0)

    catalog = _load_catalog()
    fix_details = _fetch_jira_details(manifest)
    _print_jira_analysis(fix_details)

    # AI picks the best deployment config for each fix
    selections = _select_deployments_with_ai(
        manifest, version, fix_details, catalog
    )

    if not selections:
        selections = _select_deployments_heuristic(
            manifest, fix_details, catalog
        )

    # Group fixes by selected deployment job
    job_groups: dict[str, list[str]] = {}
    for fix_id, job_name in selections.items():
        job_groups.setdefault(job_name, []).append(fix_id)

    # Map fix_id -> component for per-deployment marker composition
    fix_to_component: dict[str, str] = {}
    for change in manifest.changes:
        fix_to_component[change.id] = change.component

    # Build deployment specs
    catalog_by_job = {e["job"]: e for e in catalog}
    specs = []
    for job_name, fix_ids in job_groups.items():
        entry = catalog_by_job.get(job_name, {})

        # Compose per-deployment TEST_MARK_EXPRESSION from component markers
        deployment_components = set()
        for fix_id in fix_ids:
            comp = fix_to_component.get(fix_id, "")
            if comp:
                deployment_components.add(comp)

        if deployment_components:
            comp_markers = sorted(
                component_marker_name(mark_name, c) for c in deployment_components
            )
            test_mark_expr = " or ".join(comp_markers)
        else:
            test_mark_expr = mark_name

        # Count tests matching this deployment's components
        test_count = sum(
            1 for t in selected_tests
            if t.component and t.component in deployment_components
        )

        specs.append({
            "job_name": job_name,
            "platform": entry.get("platform", "unknown"),
            "install": entry.get("install", "unknown"),
            "features": entry.get("features", []),
            "cluster_conf": entry.get("cluster_conf", ""),
            "fix_ids": fix_ids,
            "fix_count": len(fix_ids),
            "test_count": test_count,
            "jenkins_params": {
                "OCS_VERSION": ocs_version,
                "OCP_VERSION": ocs_version,
                "CLUSTER_CONF": entry.get("cluster_conf", ""),
                "TEST_MARK_EXPRESSION": test_mark_expr,
                "TEST_PATH": "tests/",
                "OCS_CI_REPOSITORY_BRANCH": (
                    f"pr/{pr_number}|release-{ocs_version}"
                    if pr_number
                    else ""
                ),
                "RUN_INSTALL_OCP": True,
                "RUN_INSTALL_OCS": True,
                "RUN_TEST": True,
                "RUN_TEARDOWN": False,
                "PRODUCTION_RUN": True,
                "REPORT_PORTAL": True,
                "DISPLAY_NAME": f"z-stream-{version}-{job_name}",
            },
        })

    _print_deployment_plan(specs, version)
    return {"deployment_specs": specs}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _load_catalog() -> list[dict]:
    if CATALOG_PATH.exists():
        with open(CATALOG_PATH) as f:
            return json.load(f)
    logger.warning("Jenkins deployment catalog not found at %s", CATALOG_PATH)
    return []


# ---------------------------------------------------------------------------
# Jira parsing
# ---------------------------------------------------------------------------


def _fetch_jira_details(manifest: ChangeManifest) -> dict[str, dict]:
    try:
        from tools.jira_tools import jira_get_issue
    except ImportError:
        return {}

    # Only fetch bugs that might have platform info (skip CVEs and PRs)
    bugs_to_fetch = []
    for change in manifest.changes:
        if not change.id.startswith("DFBUGS"):
            continue
        # CVE bugs rarely have platform info
        if "CVE-" in change.summary:
            continue
        bugs_to_fetch.append(change)

    if not bugs_to_fetch:
        return {}

    print(
        f"  [Topology] Fetching Jira details for "
        f"{len(bugs_to_fetch)} bugs (skipping "
        f"{sum(1 for c in manifest.changes if 'CVE-' in c.summary)} CVEs)...",
        flush=True,
    )

    details = {}
    for change in bugs_to_fetch:
        try:
            raw = json.loads(jira_get_issue(change.id))
            desc = raw.get("description", "")
            platform_info = _parse_platform(desc)
            deploy_info = _parse_deployment_type(desc)
            details[change.id] = {
                "summary": change.summary,
                "component": change.component,
                "platform_raw": platform_info["raw"],
                "platform": platform_info["parsed"],
                "install_type": platform_info["install_type"],
                "deployment_raw": deploy_info["raw"],
                "deployment_type": deploy_info["parsed"],
                "special_requirements": _parse_special_requirements(desc),
            }
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", change.id, e)

    # Add CVE bugs with defaults (standard IPI, no platform info)
    for change in manifest.changes:
        if change.id.startswith("DFBUGS") and change.id not in details:
            details[change.id] = {
                "summary": change.summary,
                "component": change.component,
                "platform_raw": "",
                "platform": "unknown",
                "install_type": "unknown",
                "deployment_raw": "",
                "deployment_type": "unknown",
                "special_requirements": [],
            }

    return details


def _parse_platform(description: str) -> dict:
    pattern = (
        r"(?:The OCP platform infrastructure and deployment type|"
        r"platform.*infrastructure.*deployment)"
        r"[^:]*:\s*\n(.*?)(?:\n\s*\n|\nThe ODF|\nThe version|\nDoes this)"
    )
    match = re.search(pattern, description, re.IGNORECASE | re.DOTALL)
    raw = match.group(1).strip() if match else ""

    platform_map = {
        "aws": "aws", "azure": "azure", "gcp": "gcp",
        "vsphere": "vsphere", "vmware": "vsphere",
        "bare metal": "baremetal", "baremetal": "baremetal",
        "ibm": "ibmcloud", "rosa": "rosa",
        "all platforms": "all", "platform agnostic": "all",
    }
    parsed = "unknown"
    if raw:
        for kw, val in platform_map.items():
            if kw in raw.lower():
                parsed = val
                break

    install_type = "unknown"
    if raw:
        rl = raw.lower()
        if "ipi" in rl:
            install_type = "ipi"
        elif "upi" in rl:
            install_type = "upi"
        elif "all" in rl or "agnostic" in rl:
            install_type = "any"

    return {"raw": raw, "parsed": parsed, "install_type": install_type}


def _parse_deployment_type(description: str) -> dict:
    pattern = (
        r"(?:The ODF deployment type|ODF.*deployment.*type)"
        r"[^:]*:\s*\n(.*?)(?:\n\s*\n|\nThe version|\nDoes this)"
    )
    match = re.search(pattern, description, re.IGNORECASE | re.DOTALL)
    raw = match.group(1).strip() if match else ""

    parsed = "unknown"
    if raw:
        for kw in DEPLOYMENT_TYPE_MAP:
            if kw in raw.lower():
                parsed = kw
                break
    return {"raw": raw, "parsed": parsed}


def _parse_special_requirements(description: str) -> list[str]:
    reqs = []
    dl = description.lower()
    for kw in [
        "fips", "encryption", "kms", "vault", "thales",
        "proxy", "disconnected", "ipv6", "multus", "lso",
        "compact", "external", "arbiter", "stretch",
    ]:
        if kw in dl:
            reqs.append(kw)
    return reqs


# ---------------------------------------------------------------------------
# AI deployment selection
# ---------------------------------------------------------------------------


def _select_deployments_with_ai(
    manifest, version, fix_details, catalog,
) -> dict[str, str]:
    if not fix_details or not catalog:
        return {}

    # Build a condensed catalog for the AI prompt
    catalog_summary = []
    for e in catalog:
        features_str = ", ".join(e["features"]) if e["features"] else "standard"
        catalog_summary.append(
            f"  {e['job']}: {e['platform']} {e['install']} [{features_str}]"
        )

    fixes_text = []
    for change in manifest.changes:
        detail = fix_details.get(change.id, {})
        if not detail:
            fixes_text.append(
                f"- {change.id} [{change.component}]: {change.summary}"
            )
            continue
        fixes_text.append(
            f"- {change.id} [{change.component}]: {change.summary}\n"
            f"  Platform: {detail.get('platform_raw', '?')}\n"
            f"  Install: {detail.get('install_type', '?')}\n"
            f"  Deploy type: {detail.get('deployment_raw', '?')}\n"
            f"  Special: {detail.get('special_requirements', [])}"
        )

    prompt = (
        f"You are selecting Jenkins deployment configurations for "
        f"z-stream {version} bug fixes.\n\n"
        f"For each fix, pick the BEST matching deployment job from "
        f"the catalog based on the platform, install type, deployment "
        f"type, and special requirements from the Jira bug.\n\n"
        f"Rules:\n"
        f"- Match platform first (aws/vsphere/baremetal/azure/gcp)\n"
        f"- Match install type (ipi/upi). Prefer UPI when install type "
        f"is not specified or is unknown\n"
        f"- Match special requirements (fips, encryption, kms, "
        f"external, lso, proxy, disconnected, ipv6, etc.)\n"
        f"- For 'all platforms' or 'platform agnostic', JOIN an existing "
        f"deployment from another bug instead of creating a new one. "
        f"If no other deployment exists, pick from this priority: "
        f"{', '.join(PLATFORM_PRIORITY)}.\n"
        f"- For 'internal' deployment, use standard UPI when available\n"
        f"- For 'external' deployment, use an external job\n"
        f"- For 'provider/client', use the top priority platform "
        f"(vsphere IPI preferred, then ibmcloud, then aws)\n"
        f"- When uncertain, prefer the simplest matching config\n\n"
        f"Available deployment configs:\n"
        + "\n".join(catalog_summary)
        + f"\n\nFixes:\n"
        + "\n".join(fixes_text)
        + "\n\nOutput ONLY valid JSON mapping fix ID to job name."
    )

    try:
        result = run_node(prompt, "topology_selector")
        if result:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                valid_jobs = {e["job"] for e in catalog}
                valid = {
                    k: v for k, v in parsed.items() if v in valid_jobs
                }
                if valid:
                    logger.info(
                        "AI selected %d deployments: %s",
                        len(valid), valid,
                    )
                    return valid
    except Exception as e:
        logger.warning("AI deployment selection failed: %s", e)
    return {}


def _select_deployments_heuristic(manifest, fix_details, catalog):
    """Fallback: match on platform + install type.

    Two passes:
    1. Assign bugs with a specific platform to their matching deployment.
    2. Assign platform-agnostic bugs to an existing deployment (preferring
       higher-priority platforms). Only create a new deployment if none exist.
    """
    catalog_by_key = {}
    for e in catalog:
        key = f"{e['platform']}_{e['install']}"
        if not e["features"]:
            catalog_by_key.setdefault(key, e["job"])

    selections = {}
    deferred = []

    # Pass 1: bugs with a specific platform
    for change in manifest.changes:
        detail = fix_details.get(change.id, {})
        platform = detail.get("platform", "unknown")
        install = detail.get("install_type", "ipi")
        if install in ("any", "unknown"):
            install = "upi"

        if platform in ("all", "unknown"):
            deferred.append(change.id)
        else:
            key = f"{platform}_{install}"
            fallback = catalog_by_key.get(f"{PLATFORM_PRIORITY[0]}_upi", "")
            selections[change.id] = catalog_by_key.get(key, fallback)

    # Pass 2: platform-agnostic bugs join an existing deployment
    existing_jobs = set(selections.values())
    for fix_id in deferred:
        if existing_jobs:
            # Pick the existing deployment on the highest-priority platform
            best = None
            for p in PLATFORM_PRIORITY:
                for job in existing_jobs:
                    if p in job:
                        best = job
                        break
                if best:
                    break
            selections[fix_id] = best or next(iter(existing_jobs))
        else:
            # No existing deployments — create one using priority
            for p in PLATFORM_PRIORITY:
                job = catalog_by_key.get(f"{p}_upi") or catalog_by_key.get(f"{p}_ipi")
                if job:
                    selections[fix_id] = job
                    existing_jobs.add(job)
                    break

    return selections


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_jira_analysis(fix_details: dict):
    if not fix_details:
        return
    print(f"\n{'='*60}")
    print("  JIRA PLATFORM & DEPLOYMENT ANALYSIS")
    print(f"{'='*60}\n")
    for fix_id, d in fix_details.items():
        print(f"  {fix_id}: {d['summary'][:60]}")
        print(f"    Component:    {d['component']}")
        print(f"    Platform:     {d['platform']} ({d['platform_raw'] or 'n/a'})")
        print(f"    Install:      {d['install_type']}")
        print(f"    Deploy type:  {d['deployment_type']} ({d['deployment_raw'] or 'n/a'})")
        if d.get("special_requirements"):
            print(f"    Special:      {d['special_requirements']}")
        print()


def _print_deployment_plan(specs: list[dict], version: str):
    print(f"{'='*60}")
    print(f"  DEPLOYMENT PLAN -- z-stream {version}")
    print(f"{'='*60}")
    print(f"\n  {len(specs)} deployment(s) needed:\n")

    for i, spec in enumerate(specs, 1):
        features = ", ".join(spec["features"]) if spec["features"] else "standard"
        print(f"  [{i}] {spec['job_name']}")
        print(f"      Platform:  {spec['platform']} {spec['install']}")
        print(f"      Features:  {features}")
        print(f"      Config:    {spec['cluster_conf']}")
        print(f"      Fixes:     {', '.join(spec['fix_ids'])}")
        print(f"      Tests:     {spec.get('test_count', '?')}")
        expr = spec["jenkins_params"].get("TEST_MARK_EXPRESSION", "?")
        print(f"      Markers:   {expr}")
        print(f"\n      Jenkins API call:")
        params = spec["jenkins_params"]
        print(
            f"      POST /job/qe-deploy-ocs-cluster-prod"
            f"/buildWithParameters"
        )
        for k, v in params.items():
            print(f"        {k}={v}")
        print()

    print(f"{'='*60}")
    total_tests = sum(s.get('test_count', 0) for s in specs)
    print(
        f"  Total: {sum(s['fix_count'] for s in specs)} fixes, "
        f"{total_tests} tests across {len(specs)} deployment(s)"
    )
    print(
        f"  RUN_TEARDOWN=false -- clusters kept alive "
        f"for verification"
    )
    print(f"{'='*60}\n")


def _format_deployment_comment(specs: list[dict], version: str) -> str:
    """Format deployment plan as a GitHub PR comment in Markdown."""
    lines = [
        f"## Deployment Plan -- z-stream {version}",
        "",
        f"**{len(specs)} deployment(s)** needed "
        f"({sum(s['fix_count'] for s in specs)} fixes total). "
        f"All clusters set to `RUN_TEARDOWN=false` for "
        f"post-regression verification.",
        "",
    ]

    for i, spec in enumerate(specs, 1):
        features = (
            ", ".join(spec["features"]) if spec["features"] else "standard"
        )
        lines.extend([
            f"### [{i}] `{spec['job_name']}`",
            "",
            "| | |",
            "|---|---|",
            f"| **Platform** | {spec['platform']} {spec['install']} |",
            f"| **Features** | {features} |",
            f"| **Config** | `{spec['cluster_conf']}` |",
            f"| **Fixes** | {', '.join(spec['fix_ids'])} |",
            f"| **Tests** | {spec.get('test_count', '?')} |",
            f"| **Markers** | `{spec['jenkins_params'].get('TEST_MARK_EXPRESSION', '?')}` |",
            "",
            "<details>",
            "<summary>Jenkins parameters</summary>",
            "",
            "```",
            "POST /job/qe-deploy-ocs-cluster-prod/buildWithParameters",
        ])
        for k, v in spec["jenkins_params"].items():
            lines.append(f"  {k}={v}")
        lines.extend(["```", "", "</details>", ""])

    lines.append("---")
    lines.append("*Generated by ODF Z-Stream Pipeline*")
    return "\n".join(lines)
