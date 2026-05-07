"""Download and load the ocs-ci codebase map for test selection.

The map lives at https://github.com/shyRozen/ocs-ci-codebase-map.
It is cloned/pulled into a local cache directory and parsed into
structured data that pipeline nodes can query.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

MAP_REPO = "https://github.com/shyRozen/ocs-ci-codebase-map.git"
MAP_CACHE_DIR = Path.home() / ".cache" / "ocs-ci-codebase-map"


def ensure_map(force_pull: bool = False, version: str | None = None) -> Path:
    """Clone or pull the codebase map repo. Returns the local path.

    Args:
        force_pull: Always fetch latest from origin.
        version: If given (e.g. "4.20.5" or "4.20"), checkout the
                 matching release-X.Y branch for version-specific data.
    """
    if not MAP_CACHE_DIR.exists():
        logger.info("Cloning codebase map from %s ...", MAP_REPO)
        MAP_CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", MAP_REPO, str(MAP_CACHE_DIR)],
            capture_output=True,
            timeout=60,
        )

    if force_pull:
        logger.info("Fetching latest codebase map...")
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=MAP_CACHE_DIR,
            capture_output=True,
            timeout=30,
        )

    if version:
        branch = _version_branch(version)
        if branch:
            _checkout_branch(branch)

    return MAP_CACHE_DIR


def _version_branch(version: str) -> str | None:
    """Extract release branch name from version string.

    "4.20.5" -> "release-4.20"
    "4.20"   -> "release-4.20"
    """
    parts = version.split(".")
    if len(parts) >= 2:
        try:
            int(parts[0])
            int(parts[1])
            return f"release-{parts[0]}.{parts[1]}"
        except ValueError:
            return None
    return None


def _checkout_branch(branch: str):
    """Checkout a branch in the cached map repo."""
    r = subprocess.run(
        ["git", "checkout", branch],
        cwd=MAP_CACHE_DIR,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode == 0:
        logger.info("Map repo: checked out %s", branch)
    else:
        r2 = subprocess.run(
            ["git", "checkout", f"origin/{branch}"],
            cwd=MAP_CACHE_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r2.returncode == 0:
            logger.info("Map repo: checked out origin/%s", branch)
        else:
            logger.warning("Map repo: branch %s not found, staying on current", branch)


def _parse_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file."""
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def _parse_body(path: Path) -> str:
    """Get the markdown body (after frontmatter)."""
    text = path.read_text()
    if text.startswith("---"):
        end = text.index("---", 3) + 3
        return text[end:].strip()
    return text.strip()


def load_test_areas(map_dir: Path | None = None) -> list[dict]:
    """Load all test area notes with their frontmatter metadata.

    Returns a list of dicts like:
        {
            "name": "tests-functional-pv",
            "directory": "tests/functional/pv/",
            "squad": "green_squad",
            "test_files": 84,
            "test_functions": 113,
            "tiers": {"tier1": 30, "tier2": 46, ...},
            "body": "# PV (Persistent Volumes)\\n...",
        }
    """
    root = map_dir or ensure_map()
    areas = []

    for test_dir in [
        root / "tests" / "functional",
        root / "tests" / "cross_functional",
        root / "tests" / "libtest",
    ]:
        if not test_dir.exists():
            continue
        for md_file in sorted(test_dir.glob("*.md")):
            fm = _parse_frontmatter(md_file)
            areas.append(
                {
                    "name": md_file.stem,
                    "directory": fm.get("directory", ""),
                    "squad": fm.get("squad", ""),
                    "test_files": fm.get("test_files", 0),
                    "test_functions": fm.get("test_functions", 0),
                    "tiers": fm.get("tiers", {}),
                    "body": _parse_body(md_file),
                }
            )

    return areas


def load_squads(map_dir: Path | None = None) -> dict[str, dict]:
    """Load squad notes. Returns {squad_name: {metadata + body}}."""
    root = map_dir or ensure_map()
    squads = {}
    squad_dir = root / "squads"
    if not squad_dir.exists():
        return squads

    for md_file in sorted(squad_dir.glob("*.md")):
        fm = _parse_frontmatter(md_file)
        squads[md_file.stem] = {
            **fm,
            "body": _parse_body(md_file),
        }

    return squads


def load_components(map_dir: Path | None = None) -> dict[str, dict]:
    """Load component notes. Returns {component_name: {metadata + body}}."""
    root = map_dir or ensure_map()
    components = {}
    comp_dir = root / "components"
    if not comp_dir.exists():
        return components

    for md_file in sorted(comp_dir.glob("*.md")):
        fm = _parse_frontmatter(md_file)
        components[md_file.stem] = {
            **fm,
            "body": _parse_body(md_file),
        }

    return components


def find_tests_for_component(component: str, map_dir: Path | None = None) -> list[dict]:
    """Find test areas relevant to a given ODF component."""
    components = load_components(map_dir)
    areas = load_test_areas(map_dir)

    comp_data = components.get(component, {})
    squad = comp_data.get("squad", "")
    test_area_names = comp_data.get("test_areas", [])

    matches = []
    for area in areas:
        if area["squad"] == squad:
            matches.append(area)
        elif area["name"] in test_area_names:
            matches.append(area)

    return matches


def find_tests_for_squad(squad_name: str, map_dir: Path | None = None) -> list[dict]:
    """Find all test areas owned by a squad."""
    areas = load_test_areas(map_dir)
    return [a for a in areas if a["squad"] == squad_name]


def get_map_summary(map_dir: Path | None = None) -> str:
    """Return a text summary of the map for use in LLM prompts."""
    areas = load_test_areas(map_dir)
    squads = load_squads(map_dir)
    components = load_components(map_dir)

    lines = [
        f"OCS-CI Test Map: {len(areas)} test areas, {len(squads)} squads, {len(components)} components",
        "",
        "Test Areas:",
    ]
    for area in areas:
        lines.append(
            f"  - {area['name']}: {area['test_functions']} tests, "
            f"squad={area['squad']}, dir={area['directory']}"
        )

    lines.append("")
    lines.append("Components:")
    for name, data in components.items():
        lines.append(
            f"  - {name}: squad={data.get('squad', '?')}, " f"areas={data.get('test_areas', [])}"
        )

    return "\n".join(lines)
