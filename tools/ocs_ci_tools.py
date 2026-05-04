"""Tools for reading and analyzing the ocs-ci test framework."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from langchain_core.tools import tool

from core import config


def _ocs_ci_root() -> Path:
    return Path(config.OCS_CI_REPO_PATH)


def list_tests(directory: str) -> str:
    """List test files under a directory relative to the ocs-ci tests/ root.

    Recursively finds all Python test files (test_*.py) under the specified
    directory within the ocs-ci repository's tests/ folder.

    Args:
        directory: Directory path relative to tests/ (e.g. "functional/pv"
                   or "functional/object/mcg").

    Returns:
        JSON string with the list of test file paths relative to the repo root.
    """
    root = _ocs_ci_root()
    tests_dir = root / "tests" / directory

    if not tests_dir.exists():
        return json.dumps({
            "error": f"Directory not found: {tests_dir}",
            "ocs_ci_root": str(root),
        })

    try:
        test_files = sorted(
            str(p.relative_to(root))
            for p in tests_dir.rglob("test_*.py")
            if p.is_file()
        )

        return json.dumps({
            "directory": f"tests/{directory}",
            "file_count": len(test_files),
            "files": test_files,
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to list tests: {str(exc)}"})


def read_test_marks(file_path: str) -> str:
    """Read a test file and extract all pytest marks and test function names.

    Uses Python AST parsing to extract decorators from test functions,
    including marks like @tier1, @green_squad, @polarion_id("..."),
    @skipif_ocs_version("..."), @pytest.mark.*, etc.

    Args:
        file_path: Path to the test file, either absolute or relative to the
                   ocs-ci repository root.

    Returns:
        JSON string with test functions and their marks, or an error message.
    """
    root = _ocs_ci_root()

    # Resolve the file path
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path

    if not path.exists():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return json.dumps({"error": f"Syntax error parsing {path}: {str(exc)}"})
    except Exception as exc:
        return json.dumps({"error": f"Failed to read {path}: {str(exc)}"})

    test_functions = []

    for node in ast.walk(tree):
        # Look for test functions and test methods in classes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                continue

            marks = _extract_marks(node.decorator_list)

            test_functions.append({
                "name": node.name,
                "line": node.lineno,
                "marks": marks,
            })

    # Also extract class-level marks for test classes
    class_marks = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_marks[node.name] = _extract_marks(node.decorator_list)

    return json.dumps({
        "file": str(path.relative_to(root)) if str(path).startswith(str(root)) else str(path),
        "test_count": len(test_functions),
        "tests": test_functions,
        "class_marks": class_marks,
    }, indent=2)


def _extract_marks(decorators: list) -> list[dict]:
    """Extract mark information from a list of AST decorator nodes."""
    marks = []

    for dec in decorators:
        mark_info = _parse_decorator(dec)
        if mark_info:
            marks.append(mark_info)

    return marks


def _parse_decorator(node) -> dict | None:
    """Parse a single decorator node into a mark info dict."""
    # Simple name decorator: @tier1, @green_squad
    if isinstance(node, ast.Name):
        return {"name": node.id, "args": []}

    # Call decorator: @polarion_id("OCS-1234"), @skipif_ocs_version("<4.16")
    if isinstance(node, ast.Call):
        func = node.func

        # Direct call: @polarion_id("...")
        if isinstance(func, ast.Name):
            args = _extract_call_args(node)
            return {"name": func.id, "args": args}

        # Attribute call: @pytest.mark.parametrize(...)
        if isinstance(func, ast.Attribute):
            name = _get_attribute_name(func)
            args = _extract_call_args(node)
            return {"name": name, "args": args}

    # Attribute without call: @pytest.mark.tier1
    if isinstance(node, ast.Attribute):
        name = _get_attribute_name(node)
        return {"name": name, "args": []}

    return None


def _get_attribute_name(node: ast.Attribute) -> str:
    """Reconstruct dotted name from Attribute node (e.g. pytest.mark.tier1)."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _extract_call_args(node: ast.Call) -> list[str]:
    """Extract string arguments from a Call node."""
    args = []
    for arg in node.args:
        if isinstance(arg, ast.Constant):
            args.append(str(arg.value))
        elif isinstance(arg, ast.Name):
            args.append(arg.id)
        else:
            args.append(ast.dump(arg))
    for kw in node.keywords:
        if isinstance(kw.value, ast.Constant):
            args.append(f"{kw.arg}={kw.value.value}")
        else:
            args.append(f"{kw.arg}=...")
    return args


def squad_map_lookup(component: str) -> str:
    """Look up an ODF component in the squad mapping configuration.

    Returns the squad name and associated test paths for the given component.
    The mapping is defined in config.yaml under squad_mapping.

    Args:
        component: The component name to look up (e.g. "ceph-csi", "mcg", "rook").

    Returns:
        JSON string with squad name and test paths, or available components.
    """
    mapping = config.SQUAD_MAPPING

    if not mapping:
        return json.dumps({
            "error": "SQUAD_MAPPING not configured. Check config.yaml.",
        })

    # Normalize the component name for lookup
    component_lower = component.lower().strip()

    # Direct match
    if component_lower in mapping:
        entry = mapping[component_lower]
        return json.dumps({
            "component": component_lower,
            "squad": entry.get("squad", "unknown"),
            "test_paths": entry.get("paths", []),
        }, indent=2)

    # Try partial/fuzzy match
    matches = [
        k for k in mapping
        if component_lower in k or k in component_lower
    ]

    if matches:
        results = {}
        for m in matches:
            entry = mapping[m]
            results[m] = {
                "squad": entry.get("squad", "unknown"),
                "test_paths": entry.get("paths", []),
            }
        return json.dumps({
            "component_query": component,
            "partial_matches": results,
        }, indent=2)

    return json.dumps({
        "error": f"Component '{component}' not found in squad mapping",
        "available_components": list(mapping.keys()),
    }, indent=2)


def read_test_source(file_path: str) -> str:
    """Read the source code of a test file (first 200 lines).

    Returns the first 200 lines of a test file for inspection by an agent.
    Useful for understanding test logic, fixtures, and assertions.

    Args:
        file_path: Path to the test file, either absolute or relative to the
                   ocs-ci repository root.

    Returns:
        JSON string with the file source code, or an error message.
    """
    root = _ocs_ci_root()

    path = Path(file_path)
    if not path.is_absolute():
        path = root / path

    if not path.exists():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        total_lines = len(lines)
        truncated = total_lines > 200
        source = "\n".join(lines[:200])

        return json.dumps({
            "file": str(path.relative_to(root)) if str(path).startswith(str(root)) else str(path),
            "total_lines": total_lines,
            "lines_returned": min(total_lines, 200),
            "truncated": truncated,
            "source": source,
        }, indent=2)

    except Exception as exc:
        return json.dumps({"error": f"Failed to read {path}: {str(exc)}"})


# Tool-wrapped versions for LangGraph ReAct agents
list_tests_tool = tool(list_tests)
read_test_marks_tool = tool(read_test_marks)
squad_map_lookup_tool = tool(squad_map_lookup)
read_test_source_tool = tool(read_test_source)
