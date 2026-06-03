"""Topology Selector node -- classifies fixes by required cluster topology.

Uses AI to analyze fix descriptions and determine which Jenkins deployment
configurations are needed. Outputs deployment specs that would be sent
to the Jenkins API.
"""

from __future__ import annotations

import json
import logging
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
        "typical_fixes": "RBD, CephFS, PVC, snapshot, clone, CSI, MCG, NooBaa, "
        "bucket, S3, OBC, OCS Operator, upgrade, deployment, monitoring",
    },
    "regional_dr": {
        "description": "Regional DR multi-cluster pair",
        "job_name": "qe-deploy-ocs-cluster-multi-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
        "typical_fixes": "Regional DR, RDR, ramen, failover, relocate, "
        "replication, multicluster",
    },
    "metro_dr": {
        "description": "Metro DR stretched cluster with arbiter",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
        "typical_fixes": "Metro DR, MDR, stretch, arbiter, netsplit",
    },
    "provider_client": {
        "description": "Provider-Client managed service pair",
        "job_name": "qe-deploy-ocs-cluster-multi-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
        "typical_fixes": "Provider, consumer, managed service, ROSA, HCI, "
        "provider-client, storageclient",
    },
    "external_mode": {
        "description": "External Ceph + ODF cluster",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_3az_rhcos_3m_3w.yaml",
        "typical_fixes": "External Ceph, external mode, RHCS, "
        "external cluster",
    },
    "lso_baremetal": {
        "description": "LSO / Baremetal deployment",
        "job_name": "qe-deploy-ocs-cluster-prod",
        "cluster_conf": "conf/deployment/aws/ipi_1az_rhcos_lso_3m_3w.yaml",
        "typical_fixes": "LSO, local storage, baremetal, NVMe",
    },
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

    # Use AI to classify each fix by topology
    classifications = _classify_with_ai(manifest, version)

    # If AI fails, fall back to heuristic
    if not classifications:
        classifications = _classify_heuristic(manifest)

    # Group fixes by topology
    topology_groups: dict[str, list[str]] = {}
    for fix_id, topology in classifications.items():
        topology_groups.setdefault(topology, []).append(fix_id)

    # Generate deployment specs
    specs = []
    for topology, fix_ids in topology_groups.items():
        config = TOPOLOGY_CONFIGS.get(topology, TOPOLOGY_CONFIGS["standard_ipi"])

        # Collect test paths for this topology's fixes
        topology_tests = _tests_for_fixes(
            fix_ids, manifest, selected_tests
        )

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

    # Print what would be sent
    _print_deployment_plan(specs, version)

    return {"deployment_specs": specs}


def _classify_with_ai(manifest: ChangeManifest, version: str) -> dict[str, str]:
    """Use AI to classify each fix by required topology."""
    topology_descriptions = "\n".join(
        f"- {name}: {cfg['description']} (keywords: {cfg['typical_fixes']})"
        for name, cfg in TOPOLOGY_CONFIGS.items()
    )

    changes_text = "\n".join(
        f"- {c.id} [{c.component}]: {c.summary}"
        for c in manifest.changes
    )

    prompt = (
        f"Classify each bug fix by the cluster topology needed to test it.\n\n"
        f"Available topologies:\n{topology_descriptions}\n\n"
        f"Fixes in z-stream {version}:\n{changes_text}\n\n"
        f"For each fix, output a JSON object mapping fix ID to topology name. "
        f"Most fixes need standard_ipi unless they specifically mention DR, "
        f"provider-client, external mode, or LSO/baremetal. "
        f"When uncertain, default to standard_ipi.\n\n"
        f"Output ONLY valid JSON, no markdown fences."
    )

    try:
        result = run_node(prompt, "topology_selector")
        if result:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                valid = {k: v for k, v in parsed.items() if v in TOPOLOGY_CONFIGS}
                if valid:
                    logger.info("AI classified %d fixes: %s", len(valid), valid)
                    return valid
    except Exception as e:
        logger.warning("AI topology classification failed: %s", e)

    return {}


def _classify_heuristic(manifest: ChangeManifest) -> dict[str, str]:
    """Fallback: classify fixes by component and keywords."""
    keyword_map = {
        "regional_dr": ["regional dr", "rdr", "ramen", "failover", "relocate"],
        "metro_dr": ["metro dr", "mdr", "stretch", "arbiter", "netsplit"],
        "provider_client": [
            "provider", "consumer", "managed service", "rosa",
            "storageclient", "hci",
        ],
        "external_mode": ["external ceph", "external mode", "rhcs"],
        "lso_baremetal": ["lso", "local storage", "baremetal", "nvme"],
    }

    classifications = {}
    for change in manifest.changes:
        text = f"{change.component} {change.summary}".lower()
        matched = "standard_ipi"
        for topology, keywords in keyword_map.items():
            if any(kw in text for kw in keywords):
                matched = topology
                break
        classifications[change.id] = matched

    logger.info("Heuristic classified %d fixes: %s", len(classifications), classifications)
    return classifications


def _tests_for_fixes(
    fix_ids: list[str],
    manifest: ChangeManifest,
    selected_tests: list,
) -> list:
    """Find selected tests relevant to given fix IDs."""
    fix_components = set()
    for change in manifest.changes:
        if change.id in fix_ids:
            fix_components.add(change.component.lower())
    return [
        t for t in selected_tests
        if any(c in t.file_path.lower() for c in fix_components)
    ]


def _print_deployment_plan(specs: list[dict], version: str):
    """Print what would be sent to Jenkins."""
    print(f"\n{'='*60}")
    print(f"  DEPLOYMENT PLAN — z-stream {version}")
    print(f"{'='*60}")
    print(f"\n  {len(specs)} deployment(s) needed:\n")

    for i, spec in enumerate(specs, 1):
        print(f"  [{i}] {spec['description']}")
        print(f"      Topology:  {spec['topology']}")
        print(f"      Fixes:     {', '.join(spec['fix_ids'])}")
        print(f"      Tests:     {spec['test_count']} selected")
        print(f"\n      Jenkins API call:")
        print(f"      POST /job/{spec['jenkins_params']['JOB_NAME']}/buildWithParameters")
        params = spec["jenkins_params"]
        for k, v in params.items():
            if k != "JOB_NAME":
                print(f"        {k}={v}")
        print()

    print(f"{'='*60}")
    print(f"  Total: {sum(s['fix_count'] for s in specs)} fixes across "
          f"{len(specs)} topology/topologies")
    print(f"  RUN_TEARDOWN=false on all — clusters kept alive for verification")
    print(f"{'='*60}\n")
