"""Code Analyzer node — identifies test-relevant code areas from changes.

Uses Sonnet to analyze the change manifest and determine which test
directories and components are relevant. Calls squad_map_lookup to
get test directory paths for each component.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from core.models import ChangeManifest, StageError
from core.state import MapState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a test infrastructure expert for ODF (OpenShift Data Foundation).

Given a change manifest describing z-stream changes and a mapping of components
to their test directories, identify:

1. Which test directories should be searched for relevant tests.
2. Any additional search areas that may be affected by the changes but are not
   directly in the component's primary test directory.

Consider these cross-cutting concerns:
- Changes to core libraries (ocs_ci/ocs/) may affect many test directories.
- Deployment changes may require upgrade and install tests.
- Security fixes may need security-specific test suites.
- Storage class changes affect PV/PVC tests across multiple components.

Output a JSON object with:
- "component_test_mapping": dict mapping component names to lists of test directories
- "search_areas": flat list of all unique test directory paths to search

Output ONLY the JSON object, no other text.
"""


def code_analyzer(state: MapState) -> dict:
    """Analyze changed code to identify search areas and component mappings.

    Returns a dict with ``search_areas`` and ``component_test_mapping``.
    """
    manifest: ChangeManifest | None = state.get("change_manifest")
    if not manifest or not manifest.changes:
        logger.warning("No change manifest or empty changes, nothing to analyze")
        return {"search_areas": [], "component_test_mapping": {}}

    # Look up test directories for each component using the squad map
    try:
        from tools.ocs_ci_tools import squad_map_lookup
    except ImportError:
        squad_map_lookup = None
        logger.warning("ocs_ci_tools not available, using fallback component mapping")

    # Gather unique components
    components = list({c.component for c in manifest.changes})

    # Build initial component-to-test-dir mapping from squad map
    raw_mapping: dict[str, list[str]] = {}
    for component in components:
        if squad_map_lookup is not None:
            try:
                dirs = squad_map_lookup(component)
                if dirs:
                    raw_mapping[component] = dirs if isinstance(dirs, list) else [dirs]
                else:
                    raw_mapping[component] = _fallback_dirs(component)
            except Exception as e:
                logger.warning(
                    "squad_map_lookup failed for %s: %s, using fallback", component, e
                )
                raw_mapping[component] = _fallback_dirs(component)
        else:
            raw_mapping[component] = _fallback_dirs(component)

    # Use LLM to refine mapping and identify additional search areas
    llm = get_llm("code_analyzer")
    if llm is not None:
        try:
            result = _analyze_with_llm(llm, manifest, raw_mapping)
            if result:
                return result
        except Exception as e:
            logger.error("LLM code analysis failed: %s, using raw mapping", e)

    # Fallback: flatten the raw mapping
    all_dirs: list[str] = []
    for dirs in raw_mapping.values():
        for d in dirs:
            if d not in all_dirs:
                all_dirs.append(d)

    logger.info(
        "Code analysis complete: %d components -> %d search areas",
        len(raw_mapping),
        len(all_dirs),
    )

    return {
        "search_areas": all_dirs,
        "component_test_mapping": raw_mapping,
    }


def _analyze_with_llm(
    llm, manifest: ChangeManifest, raw_mapping: dict[str, list[str]]
) -> dict | None:
    """Use LLM to refine the component-to-test mapping."""
    changes_data = [c.model_dump(mode="json") for c in manifest.changes]

    prompt = (
        f"Analyze these z-stream changes for ODF version {manifest.zstream_version} "
        f"and refine the test directory mapping.\n\n"
        f"Changes:\n{json.dumps(changes_data, indent=2)}\n\n"
        f"Initial component-to-test-directory mapping:\n"
        f"{json.dumps(raw_mapping, indent=2)}\n\n"
        f"Consider cross-cutting concerns and identify any additional test "
        f"directories that should be searched."
    )

    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])

    text = response.content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        component_mapping = result.get("component_test_mapping", raw_mapping)
        search_areas = result.get("search_areas", [])

        # Ensure all dirs from the mapping are in search_areas
        for dirs in component_mapping.values():
            for d in dirs:
                if d not in search_areas:
                    search_areas.append(d)

        logger.info(
            "LLM refined mapping: %d components -> %d search areas",
            len(component_mapping),
            len(search_areas),
        )

        return {
            "search_areas": search_areas,
            "component_test_mapping": component_mapping,
        }
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM code analysis response")
        return None


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
