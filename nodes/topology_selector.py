"""Topology Selector node -- classifies fixes by required cluster topology.

Parses Jira bug descriptions to extract platform and deployment type
from the standard DFBUGS template, then uses AI to map each fix to
the Jenkins deployment configuration needed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from core.agent_runner import run_node
from core.models import ChangeManifest, StageError
from core.state import PipelineState

logger = logging.getLogger(__name__)

TOPOLOGY_CONFIGS = {
    "standard_ipi": {
        "description": "Standard IPI cluster (AWS)",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
    },
    "regional_dr": {
        "description": "Regional DR multi-cluster pair",
        "job_name": "qe-deploy-ocs-cluster-multi-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
    },
    "metro_dr": {
        "description": "Metro DR stretched cluster with arbiter",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
    },
    "provider_client": {
        "description": "Provider-Client managed service pair",
        "job_name": "qe-deploy-ocs-cluster-multi-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
    },
    "external_mode": {
        "description": "External Ceph + ODF cluster",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
    },
    "lso_baremetal": {
        "description": "LSO / Baremetal deployment",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_1az_rhcos_lso_3m_3w.yaml",
    },
}

# Maps parsed Jira values to topology keys
PLATFORM_MAP = {
    "aws": "aws",
    "azure": "azure",
    "gcp": "gcp",
    "vsphere": "vsphere",
    "vmware": "vsphere",
    "bare metal": "baremetal",
    "baremetal": "baremetal",
    "ibm": "ibm",
    "rosa": "rosa",
    "all platforms": "all",
    "platform agnostic": "all",
}

DEPLOYMENT_TYPE_MAP = {
    "internal": "standard_ipi",
    "internal-attached": "lso_baremetal",
    "lso": "lso_baremetal",
    "external": "external_mode",
    "external mode": "external_mode",
    "multicluster": "standard_ipi",
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
    """Classify fixes by required topology and generate Jenkins deploy specs."""
    manifest: ChangeManifest | None = state.get("change_manifest")
    version = state.get("zstream_version", "unknown")
    selected_tests = state.get("selected_tests") or []

    if not manifest or not manifest.changes:
        logger.warning("No changes to classify")
        return {"deployment_specs": []}

    parts = version.split(".")
    ocs_version = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version
    mark_name = f"zstream_{version.replace('.', '_').replace('-', '_')}"

    # Step 1: Fetch Jira descriptions and parse platform/deployment info
    fix_details = _fetch_jira_details(manifest)

    # Step 2: Print what we found in Jira
    _print_jira_analysis(fix_details)

    # Step 3: AI classification using parsed Jira data
    classifications = _classify_with_ai(manifest, version, fix_details)

    if not classifications:
        classifications = _classify_from_jira(fix_details, manifest)

    # Group by topology
    topology_groups: dict[str, list[str]] = {}
    for fix_id, topology in classifications.items():
        topology_groups.setdefault(topology, []).append(fix_id)

    # Generate deployment specs
    specs = []
    for topology, fix_ids in topology_groups.items():
        config = TOPOLOGY_CONFIGS.get(topology, TOPOLOGY_CONFIGS["standard_ipi"])
        topology_tests = _tests_for_fixes(fix_ids, manifest, selected_tests)

        spec = {
            "topology": topology,
            "description": config["description"],
            "fix_ids": fix_ids,
            "fix_count": len(fix_ids),
            "test_count": len(topology_tests),
            "jenkins_params": {
                "JOB_NAME": config["job_name"],
                "OCS_VERSION": ocs_version,
                "OCP_VERSION": ocs_version,
                "CLUSTER_CONF": config["cluster_conf"],
                "TEST_MARK_EXPRESSION": mark_name,
                "TEST_PATH": "tests/",
                "RUN_INSTALL_OCP": True,
                "RUN_INSTALL_OCS": True,
                "RUN_TEST": True,
                "RUN_TEARDOWN": False,
                "PRODUCTION_RUN": True,
                "REPORT_PORTAL": True,
                "DISPLAY_NAME": f"z-stream-{version}-{topology}",
            },
        }
        specs.append(spec)

    _print_deployment_plan(specs, version)
    return {"deployment_specs": specs}


# ---------------------------------------------------------------------------
# Jira description parsing
# ---------------------------------------------------------------------------


def _fetch_jira_details(manifest: ChangeManifest) -> dict[str, dict]:
    """Fetch full Jira descriptions and parse platform/deployment info."""
    try:
        from tools.jira_tools import jira_get_issue
    except ImportError:
        logger.warning("jira_tools not available")
        return {}

    details = {}
    for change in manifest.changes:
        if not change.id.startswith("DFBUGS"):
            continue
        try:
            raw = json.loads(jira_get_issue(change.id))
            desc = raw.get("description", "")
            platform_info = _parse_platform(desc)
            deploy_info = _parse_deployment_type(desc)
            details[change.id] = {
                "summary": change.summary,
                "component": change.component,
                "platform_raw": platform_info["raw"],
                "platform_parsed": platform_info["parsed"],
                "deployment_raw": deploy_info["raw"],
                "deployment_parsed": deploy_info["parsed"],
                "topology_hint": deploy_info["topology"],
            }
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", change.id, e)

    return details


def _parse_platform(description: str) -> dict:
    """Extract platform from the DFBUGS template question."""
    pattern = (
        r"(?:The OCP platform infrastructure and deployment type|"
        r"platform.*infrastructure.*deployment)"
        r"[^:]*:\s*\n(.*?)(?:\n\s*\n|\nThe ODF|\nThe version|\nDoes this)"
    )
    match = re.search(pattern, description, re.IGNORECASE | re.DOTALL)
    raw = match.group(1).strip() if match else ""

    parsed = "unknown"
    if raw:
        raw_lower = raw.lower()
        for keyword, platform in PLATFORM_MAP.items():
            if keyword in raw_lower:
                parsed = platform
                break

    install_type = "unknown"
    if raw:
        raw_lower = raw.lower()
        if "ipi" in raw_lower:
            install_type = "ipi"
        elif "upi" in raw_lower:
            install_type = "upi"
        elif "all" in raw_lower or "agnostic" in raw_lower:
            install_type = "any"

    return {"raw": raw, "parsed": parsed, "install_type": install_type}


def _parse_deployment_type(description: str) -> dict:
    """Extract ODF deployment type from the DFBUGS template question."""
    pattern = (
        r"(?:The ODF deployment type|"
        r"ODF.*deployment.*type)"
        r"[^:]*:\s*\n(.*?)(?:\n\s*\n|\nThe version|\nDoes this)"
    )
    match = re.search(pattern, description, re.IGNORECASE | re.DOTALL)
    raw = match.group(1).strip() if match else ""

    topology = "standard_ipi"
    parsed = "unknown"
    if raw:
        raw_lower = raw.lower()
        for keyword, topo in DEPLOYMENT_TYPE_MAP.items():
            if keyword in raw_lower:
                topology = topo
                parsed = keyword
                break

    return {"raw": raw, "parsed": parsed, "topology": topology}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify_with_ai(
    manifest: ChangeManifest, version: str, fix_details: dict
) -> dict[str, str]:
    """Use AI to classify with real Jira platform/deployment data."""
    if not fix_details:
        return {}

    topology_list = "\n".join(
        f"- {name}: {cfg['description']}"
        for name, cfg in TOPOLOGY_CONFIGS.items()
    )

    fixes_text = []
    for change in manifest.changes:
        detail = fix_details.get(change.id, {})
        line = f"- {change.id} [{change.component}]: {change.summary}"
        if detail:
            plat = detail.get("platform_raw", "?")
            deploy = detail.get("deployment_raw", "?")
            line += f"\n  Platform: {plat}\n  Deployment type: {deploy}"
        fixes_text.append(line)

    prompt = (
        f"Classify each bug fix by the cluster topology needed to test it.\n\n"
        f"Available topologies:\n{topology_list}\n\n"
        f"Fixes in z-stream {version} (with platform and deployment info "
        f"from Jira):\n" + "\n".join(fixes_text) + "\n\n"
        f"Rules:\n"
        f"- 'Internal' deployment → standard_ipi\n"
        f"- 'External' or 'External mode' → external_mode\n"
        f"- 'Provider' or 'Provider-Client' or 'Consumer' → provider_client\n"
        f"- 'DR' or 'Regional DR' → regional_dr\n"
        f"- 'Metro DR' or 'Stretch' or 'Arbiter' → metro_dr\n"
        f"- 'LSO' or 'Internal-Attached' → lso_baremetal\n"
        f"- 'Multicluster' with NooBaa/MCG → standard_ipi (MCG multicluster "
        f"runs on standard IPI)\n"
        f"- 'All platforms' or 'platform agnostic' → standard_ipi\n"
        f"- When uncertain, default to standard_ipi\n\n"
        f"Output ONLY valid JSON mapping fix ID to topology name."
    )

    try:
        result = run_node(prompt, "topology_selector")
        if result:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                valid = {
                    k: v for k, v in parsed.items()
                    if v in TOPOLOGY_CONFIGS
                }
                if valid:
                    logger.info("AI classified %d fixes: %s", len(valid), valid)
                    return valid
    except Exception as e:
        logger.warning("AI topology classification failed: %s", e)

    return {}


def _classify_from_jira(
    fix_details: dict, manifest: ChangeManifest
) -> dict[str, str]:
    """Fallback: classify using parsed Jira platform/deployment data."""
    classifications = {}
    for change in manifest.changes:
        detail = fix_details.get(change.id, {})
        if detail and detail.get("topology_hint"):
            classifications[change.id] = detail["topology_hint"]
        else:
            classifications[change.id] = "standard_ipi"
    return classifications


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tests_for_fixes(fix_ids, manifest, selected_tests):
    fix_components = set()
    for change in manifest.changes:
        if change.id in fix_ids:
            fix_components.add(change.component.lower())
    return [
        t for t in selected_tests
        if any(c in t.file_path.lower() for c in fix_components)
    ]


def _print_jira_analysis(fix_details: dict):
    """Print what we extracted from Jira."""
    if not fix_details:
        return
    print(f"\n{'='*60}")
    print("  JIRA PLATFORM & DEPLOYMENT ANALYSIS")
    print(f"{'='*60}\n")
    for fix_id, detail in fix_details.items():
        print(f"  {fix_id}: {detail['summary'][:60]}")
        print(f"    Component:       {detail['component']}")
        print(f"    Platform (raw):  {detail['platform_raw'] or '(not specified)'}")
        print(f"    Platform:        {detail['platform_parsed']}")
        print(f"    Deploy (raw):    {detail['deployment_raw'] or '(not specified)'}")
        print(f"    Deploy type:     {detail['deployment_parsed']}")
        print(f"    Topology hint:   {detail['topology_hint']}")
        print()


def _print_deployment_plan(specs: list[dict], version: str):
    """Print what would be sent to Jenkins."""
    print(f"{'='*60}")
    print(f"  DEPLOYMENT PLAN -- z-stream {version}")
    print(f"{'='*60}")
    print(f"\n  {len(specs)} deployment(s) needed:\n")

    for i, spec in enumerate(specs, 1):
        print(f"  [{i}] {spec['description']}")
        print(f"      Topology:  {spec['topology']}")
        print(f"      Fixes:     {', '.join(spec['fix_ids'])}")
        print(f"      Tests:     {spec['test_count']} selected")
        print(f"\n      Jenkins API call:")
        params = spec["jenkins_params"]
        print(
            f"      POST /job/{params['JOB_NAME']}"
            f"/buildWithParameters"
        )
        for k, v in params.items():
            if k != "JOB_NAME":
                print(f"        {k}={v}")
        print()

    print(f"{'='*60}")
    print(
        f"  Total: {sum(s['fix_count'] for s in specs)} fixes "
        f"across {len(specs)} topology/topologies"
    )
    print(
        f"  RUN_TEARDOWN=false on all -- clusters kept alive "
        f"for verification"
    )
    print(f"{'='*60}\n")
