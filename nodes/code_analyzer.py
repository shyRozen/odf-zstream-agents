"""Code Analyzer node -- identifies test-relevant code areas from changes.

Uses the unified agent runner to analyze the change manifest and determine
which test directories and components are relevant.  Falls back to
SQUAD_MAPPING from config when the agent is unavailable.
"""

from __future__ import annotations

import json
import logging

from core.agent_runner import run_node_json
from core import config
from core.models import ChangeManifest
from core.state import MapState

logger = logging.getLogger(__name__)


def code_analyzer(state: MapState) -> dict:
    """Analyze changed code to identify search areas and component mappings.

    Returns a dict with ``search_areas`` and ``component_test_mapping``.
    """
    manifest: ChangeManifest | None = state.get("change_manifest")
    if not manifest or not manifest.changes:
        logger.warning("No change manifest or empty changes, nothing to analyze")
        return {"search_areas": [], "component_test_mapping": {}}

    # Gather unique components
    components = list({c.component for c in manifest.changes})

    # Build initial component-to-test-dir mapping from squad mapping config
    raw_mapping: dict[str, list[str]] = {}
    squad_mapping = config.SQUAD_MAPPING or {}
    for component in components:
        if component in squad_mapping:
            dirs = squad_mapping[component]
            raw_mapping[component] = dirs if isinstance(dirs, list) else [dirs]
        else:
            raw_mapping[component] = _fallback_dirs(component)

    # Use agent to refine mapping and identify additional search areas
    try:
        changes_data = [c.model_dump(mode="json") for c in manifest.changes]

        prompt = (
            f"You are a test infrastructure expert for ODF (OpenShift Data "
            f"Foundation).\n\n"
            f"Given these changed components for z-stream version "
            f"{manifest.zstream_version}, find relevant ocs-ci test "
            f"directories.\n\n"
            f"Changes:\n{json.dumps(changes_data, indent=2)}\n\n"
            f"Initial component-to-test-directory mapping:\n"
            f"{json.dumps(raw_mapping, indent=2)}\n\n"
            f"Instructions:\n"
            f"1. Use find/grep/ls to explore the ocs-ci test tree and verify "
            f"   which directories exist.\n"
            f"2. Consider cross-cutting concerns:\n"
            f"   - Changes to core libraries (ocs_ci/ocs/) may affect many "
            f"     test directories.\n"
            f"   - Deployment changes may require upgrade and install tests.\n"
            f"   - Security fixes may need security-specific test suites.\n"
            f"   - Storage class changes affect PV/PVC tests across multiple "
            f"     components.\n"
            f"3. Refine the mapping and add any additional directories.\n\n"
            f"Return a JSON object with:\n"
            f'- "component_test_mapping": dict mapping component names to '
            f"  lists of test directories\n"
            f'- "search_areas": flat list of all unique test directory paths '
            f"  to search\n\n"
            f"Return ONLY the JSON object."
        )

        result = run_node_json(
            prompt,
            "code_analyzer",
            allowed_tools=["Read", "Bash(find*)", "Bash(grep*)", "Bash(ls*)"],
            cwd=config.OCS_CI_REPO_PATH,
        )

        if result and isinstance(result, dict):
            component_mapping = result.get("component_test_mapping", raw_mapping)
            search_areas = result.get("search_areas", [])

            # Ensure all dirs from the mapping are in search_areas
            for dirs in component_mapping.values():
                for d in dirs:
                    if d not in search_areas:
                        search_areas.append(d)

            logger.info(
                "Agent refined mapping: %d components -> %d search areas",
                len(component_mapping),
                len(search_areas),
            )
            return {
                "search_areas": search_areas,
                "component_test_mapping": component_mapping,
            }

    except Exception as e:
        logger.error("Agent code analysis failed: %s, using raw mapping", e)

    # Fallback: flatten the raw mapping
    all_dirs: list[str] = []
    for dirs in raw_mapping.values():
        for d in dirs:
            if d not in all_dirs:
                all_dirs.append(d)

    logger.info(
        "Code analysis complete (fallback): %d components -> %d search areas",
        len(raw_mapping),
        len(all_dirs),
    )

    return {
        "search_areas": all_dirs,
        "component_test_mapping": raw_mapping,
    }


def _fallback_dirs(component: str) -> list[str]:
    """Provide fallback test directories for known ODF components."""
    fallback_map = {
        "ocs-operator": [
            "tests/functional/ocs",
            "tests/functional/storageclass",
        ],
        "odf-operator": [
            "tests/functional/odf",
        ],
        "rook-ceph": [
            "tests/functional/ceph",
            "tests/functional/monitoring",
        ],
        "noobaa": [
            "tests/functional/object",
            "tests/functional/bucket",
            "tests/functional/rgw",
        ],
        "ceph-csi": [
            "tests/functional/pv",
            "tests/functional/storageclass",
        ],
        "odf-console": [
            "tests/functional/ui",
        ],
        "deployment": [
            "tests/functional/deployment",
            "tests/functional/upgrade",
        ],
    }
    return fallback_map.get(component, [f"tests/functional/{component}"])
