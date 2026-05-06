"""Code Analyzer node -- maps changed components to test directories.

Uses the pre-built codebase map and squad_mapping config to find
relevant test areas. No LLM needed for this structured lookup.
"""

from __future__ import annotations

import logging

from core import config
from core.models import ChangeManifest
from core.state import MapState

logger = logging.getLogger(__name__)


def code_analyzer(state: MapState) -> dict:
    manifest: ChangeManifest | None = state.get("change_manifest")
    if not manifest or not manifest.changes:
        logger.warning("No change manifest or empty changes, nothing to analyze")
        return {"search_areas": [], "component_test_mapping": {}}

    components = list({c.component for c in manifest.changes})
    squad_mapping = config.SQUAD_MAPPING or {}

    component_test_mapping: dict[str, list[str]] = {}
    all_search_areas: list[str] = []

    for component in components:
        if component in squad_mapping:
            entry = squad_mapping[component]
            if isinstance(entry, dict):
                paths = entry.get("paths", [])
            elif isinstance(entry, list):
                paths = entry
            else:
                paths = [str(entry)]
        else:
            paths = _fallback_dirs(component)

        component_test_mapping[component] = paths
        all_search_areas.extend(paths)

    search_areas = sorted(set(all_search_areas))

    logger.info(
        "Mapped %d components to %d test directories: %s",
        len(components),
        len(search_areas),
        search_areas,
    )

    return {
        "search_areas": search_areas,
        "component_test_mapping": component_test_mapping,
    }


def _fallback_dirs(component: str) -> list[str]:
    """Guess test dirs for components not in squad_mapping."""
    c = component.lower().replace("-", "").replace("_", "")
    mappings = {
        "cephcsi": ["tests/functional/pv/", "tests/functional/storageclass/"],
        "mcg": ["tests/functional/object/mcg/"],
        "noobaa": ["tests/functional/object/mcg/"],
        "rgw": ["tests/functional/object/rgw/"],
        "rookceph": ["tests/functional/z_cluster/", "tests/functional/pod_and_daemons/"],
        "ocsoperator": ["tests/functional/z_cluster/", "tests/functional/deployment/"],
        "odfoperator": ["tests/functional/z_cluster/", "tests/functional/deployment/"],
        "odfconsole": ["tests/functional/ui/", "tests/cross_functional/ui/"],
        "managementconsole": ["tests/functional/ui/", "tests/cross_functional/ui/"],
        "monitoring": ["tests/functional/monitoring/"],
        "nfs": ["tests/functional/nfs_feature/"],
        "lvmo": ["tests/functional/lvmo/"],
        "lvm": ["tests/functional/lvmo/"],
    }
    return mappings.get(c, ["tests/functional/"])
