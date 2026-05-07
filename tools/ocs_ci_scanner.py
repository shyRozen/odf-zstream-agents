"""Scan ocs-ci test files and build a per-file test index.

Parses every test_*.py file using AST to extract:
- Test function/method names
- Decorators (marks, squad, tier, polarion_id, skipif)
- Docstrings
- Class membership
- Keywords from function names and docstrings

Output: JSON index at ~/.cache/ocs-ci-test-index.json
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

from core import config

logger = logging.getLogger(__name__)

OCS_CI_ROOT = Path(config.OCS_CI_REPO_PATH)
INDEX_PATH = Path.home() / ".cache" / "ocs-ci-test-index.json"


def scan_all_tests() -> list[dict]:
    """Scan all test_*.py files and return structured metadata."""
    tests_dir = OCS_CI_ROOT / "tests"
    if not tests_dir.exists():
        logger.error("Tests directory not found: %s", tests_dir)
        return []

    test_files = sorted(tests_dir.rglob("test_*.py"))
    logger.info("Scanning %d test files...", len(test_files))

    results = []
    for test_file in test_files:
        try:
            file_info = _parse_test_file(test_file)
            if file_info and file_info.get("test_functions"):
                results.append(file_info)
        except Exception as e:
            logger.warning("Failed to parse %s: %s", test_file, e)

    logger.info("Scanned %d files, %d with test functions", len(test_files), len(results))
    return results


def _parse_test_file(file_path: Path) -> dict | None:
    """Parse a single test file and extract metadata."""
    try:
        source = file_path.read_text(errors="ignore")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return None

    rel_path = str(file_path.relative_to(OCS_CI_ROOT))

    # Collect file-level decorators (on classes)
    file_marks = []
    file_squad = ""

    test_functions = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_marks = _extract_decorators(node)
            class_squad = _find_squad(class_marks)
            if class_squad:
                file_squad = class_squad
            file_marks.extend(class_marks)

            # Find test methods in the class
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("test_"):
                        func_info = _extract_function_info(item, node.name, class_marks)
                        test_functions.append(func_info)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") and node.col_offset == 0:
                func_marks = _extract_decorators(node)
                func_squad = _find_squad(func_marks)
                if func_squad and not file_squad:
                    file_squad = func_squad
                file_marks.extend(func_marks)

                func_info = _extract_function_info(node, None, [])
                test_functions.append(func_info)

    if not test_functions:
        return None

    # Extract keywords from function names and docstrings
    keywords = set()
    for func in test_functions:
        for word in func["name"].replace("test_", "").split("_"):
            if len(word) > 2:
                keywords.add(word.lower())
        for word in func.get("docstring", "").lower().split():
            if len(word) > 3:
                keywords.add(word)

    # Determine directory category
    parts = rel_path.split("/")
    category = parts[1] if len(parts) > 2 else "root"
    subcategory = parts[2] if len(parts) > 3 else ""

    all_marks = list(set(file_marks))

    return {
        "file_path": rel_path,
        "category": category,
        "subcategory": subcategory,
        "squad": file_squad,
        "test_count": len(test_functions),
        "test_functions": test_functions,
        "marks": all_marks,
        "tiers": _extract_tiers(all_marks),
        "polarion_ids": _extract_polarion_ids(all_marks),
        "skip_conditions": _extract_skip_conditions(all_marks),
        "keywords": sorted(keywords)[:30],
        "description": _file_description(test_functions, rel_path),
    }


def _extract_function_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: str | None,
    class_marks: list[str],
) -> dict:
    """Extract info from a test function/method."""
    marks = _extract_decorators(node)
    all_marks = list(set(marks + class_marks))

    docstring = ast.get_docstring(node) or ""
    if len(docstring) > 200:
        docstring = docstring[:200] + "..."

    node_id = f"{class_name}::{node.name}" if class_name else node.name

    return {
        "name": node.name,
        "node_id": node_id,
        "marks": all_marks,
        "docstring": docstring,
        "line": node.lineno,
    }


def _extract_decorators(node: ast.AST) -> list[str]:
    """Extract decorator names from a class or function."""
    marks = []
    for dec in getattr(node, "decorator_list", []):
        mark_str = _decorator_to_string(dec)
        if mark_str:
            marks.append(mark_str)
    return marks


def _decorator_to_string(dec: ast.AST) -> str:
    """Convert a decorator AST node to a readable string."""
    if isinstance(dec, ast.Name):
        return dec.id
    elif isinstance(dec, ast.Attribute):
        parts = []
        current = dec
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    elif isinstance(dec, ast.Call):
        func_str = _decorator_to_string(dec.func)
        args = []
        for arg in dec.args:
            if isinstance(arg, ast.Constant):
                args.append(repr(arg.value))
            elif isinstance(arg, ast.Name):
                args.append(arg.id)
        if args:
            return f"{func_str}({', '.join(args)})"
        return func_str
    return ""


def _find_squad(marks: list[str]) -> str:
    """Find squad mark from a list of marks."""
    for mark in marks:
        if "_squad" in mark and not mark.startswith("pytest.mark."):
            return mark
        if "pytest.mark." in mark and "_squad" in mark:
            return mark.split("pytest.mark.")[-1].split("(")[0]
    return ""


def _extract_tiers(marks: list[str]) -> list[str]:
    """Extract tier marks."""
    tiers = []
    for mark in marks:
        for tier in ["tier0", "tier1", "tier2", "tier3", "tier4", "tier4a", "tier4b", "tier4c"]:
            if tier in mark.lower():
                tiers.append(tier)
    return sorted(set(tiers))


def _extract_polarion_ids(marks: list[str]) -> list[str]:
    """Extract Polarion IDs from marks."""
    ids = []
    for mark in marks:
        match = re.findall(r"OCS-\d+", mark)
        ids.extend(match)
    return ids


def _extract_skip_conditions(marks: list[str]) -> list[str]:
    """Extract skip conditions from marks."""
    conditions = []
    for mark in marks:
        if "skipif" in mark.lower() or "skip_" in mark.lower():
            conditions.append(mark)
    return conditions


def _file_description(test_functions: list[dict], rel_path: str) -> str:
    """Generate a one-line description from test function names."""
    if not test_functions:
        return ""

    # Use the first docstring if available
    for func in test_functions:
        ds = func.get("docstring", "")
        if ds and len(ds) > 10:
            first_line = ds.split("\n")[0].strip()
            if len(first_line) > 10:
                return first_line[:150]

    # Fall back to summarizing function names
    func_names = [f["name"].replace("test_", "") for f in test_functions[:5]]
    summary = ", ".join(func_names)
    return summary[:150]


def build_index(force: bool = False) -> Path:
    """Build and save the test index. Returns the index file path."""
    if INDEX_PATH.exists() and not force:
        logger.info("Index already exists at %s (use force=True to rebuild)", INDEX_PATH)
        return INDEX_PATH

    results = scan_all_tests()

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump(
            {
                "source": str(OCS_CI_ROOT),
                "total_files": len(results),
                "total_tests": sum(r["test_count"] for r in results),
                "files": results,
            },
            f,
            indent=2,
        )

    logger.info(
        "Index saved to %s (%d files, %d tests)",
        INDEX_PATH,
        len(results),
        sum(r["test_count"] for r in results),
    )
    return INDEX_PATH


def load_index(version: str | None = None) -> dict:
    """Load the test index from the codebase map repo.

    Args:
        version: Z-stream version (e.g., "4.20.5") or release version
                 (e.g., "4.20"). The map repo's matching release-X.Y
                 branch is checked out to load version-specific data.
    """
    from core.test_map import ensure_map

    map_dir = ensure_map(version=version)
    map_index = map_dir / "test-index.json"

    if map_index.exists():
        logger.info("Loading test index from %s", map_index)
        with open(map_index) as f:
            return json.load(f)

    # Fallback to local cache
    if INDEX_PATH.exists():
        with open(INDEX_PATH) as f:
            return json.load(f)

    # Last resort: build from source
    build_index()
    with open(INDEX_PATH) as f:
        return json.load(f)
