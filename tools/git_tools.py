"""Git tools for analyzing diffs and commit history between z-stream tags."""

from __future__ import annotations

import json
import subprocess

from langchain_core.tools import tool


def _run_git(repo_path: str, args: list[str], max_output: int = 50000) -> str:
    """Run a git command and return stdout, with output truncation."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"git command failed (exit {result.returncode})",
                    "stderr": result.stderr[:2000],
                    "command": f"git {' '.join(args)}",
                }
            )

        output = result.stdout
        truncated = False
        if len(output) > max_output:
            output = output[:max_output]
            truncated = True

        return json.dumps(
            {
                "output": output,
                "truncated": truncated,
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"git command timed out: git {' '.join(args)}"})
    except FileNotFoundError:
        return json.dumps({"error": f"Repository path not found: {repo_path}"})
    except Exception as exc:
        return json.dumps({"error": f"git command failed: {str(exc)}"})


def git_diff_files(repo_path: str, from_tag: str, to_tag: str) -> str:
    """List files changed between two git tags using git diff --name-only.

    Useful for understanding the scope of changes in a z-stream release
    by seeing which files were modified between version tags.

    Args:
        repo_path: Absolute path to the git repository.
        from_tag: The starting tag (e.g. "v4.16.0-1").
        to_tag: The ending tag (e.g. "v4.16.1-1").

    Returns:
        JSON string with the list of changed file paths, or an error message.
    """
    result = _run_git(repo_path, ["diff", "--name-only", f"{from_tag}...{to_tag}"])

    try:
        parsed = json.loads(result)
        if "error" in parsed:
            return result

        files = [f for f in parsed["output"].strip().split("\n") if f]
        return json.dumps(
            {
                "from_tag": from_tag,
                "to_tag": to_tag,
                "file_count": len(files),
                "files": files,
                "truncated": parsed.get("truncated", False),
            },
            indent=2,
        )
    except (json.JSONDecodeError, KeyError):
        return result


def git_log_between(repo_path: str, from_tag: str, to_tag: str) -> str:
    """Get the git log (oneline format) between two tags.

    Shows all commits between two version tags, useful for understanding
    what changes were introduced in a z-stream release.

    Args:
        repo_path: Absolute path to the git repository.
        from_tag: The starting tag (e.g. "v4.16.0-1").
        to_tag: The ending tag (e.g. "v4.16.1-1").

    Returns:
        JSON string with commit entries (sha + message), or an error message.
    """
    result = _run_git(
        repo_path,
        ["log", "--oneline", "--no-merges", f"{from_tag}...{to_tag}"],
    )

    try:
        parsed = json.loads(result)
        if "error" in parsed:
            return result

        commits = []
        for line in parsed["output"].strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            commits.append(
                {
                    "sha": parts[0],
                    "message": parts[1] if len(parts) > 1 else "",
                }
            )

        return json.dumps(
            {
                "from_tag": from_tag,
                "to_tag": to_tag,
                "commit_count": len(commits),
                "commits": commits,
                "truncated": parsed.get("truncated", False),
            },
            indent=2,
        )
    except (json.JSONDecodeError, KeyError):
        return result


def git_show_commit(repo_path: str, commit_sha: str) -> str:
    """Show detailed information for a specific git commit.

    Displays the commit message, author, date, and the diff of changes.
    The diff output is capped at 10000 characters to prevent overflow.

    Args:
        repo_path: Absolute path to the git repository.
        commit_sha: The commit SHA (full or abbreviated).

    Returns:
        JSON string with commit details and diff, or an error message.
    """
    result = _run_git(
        repo_path,
        ["show", "--stat", "--format=fuller", commit_sha],
        max_output=10000,
    )

    try:
        parsed = json.loads(result)
        if "error" in parsed:
            return result

        output = parsed["output"]

        # Parse structured fields from the fuller format
        lines = output.split("\n")
        commit_info = {
            "sha": commit_sha,
            "raw_output": output,
            "truncated": parsed.get("truncated", False),
        }

        for line in lines[:15]:
            if line.startswith("Author:"):
                commit_info["author"] = line[len("Author:") :].strip()
            elif line.startswith("AuthorDate:"):
                commit_info["author_date"] = line[len("AuthorDate:") :].strip()
            elif line.startswith("Commit:"):
                commit_info["committer"] = line[len("Commit:") :].strip()
            elif line.startswith("CommitDate:"):
                commit_info["commit_date"] = line[len("CommitDate:") :].strip()

        return json.dumps(commit_info, indent=2)
    except (json.JSONDecodeError, KeyError):
        return result


# Tool-wrapped versions for LangGraph ReAct agents
git_diff_files_tool = tool(git_diff_files)
git_log_between_tool = tool(git_log_between)
git_show_commit_tool = tool(git_show_commit)
