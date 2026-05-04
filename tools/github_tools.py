"""GitHub tools for branch management, test marking, and PR creation."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from core import config


def _get_repo():
    """Get a PyGithub Repository object, or return an error string."""
    if not config.GITHUB_TOKEN:
        return None, json.dumps({"error": "GITHUB_TOKEN not configured"})

    try:
        from github import Github

        gh = Github(config.GITHUB_TOKEN)
        repo = gh.get_repo(config.GITHUB_REPO)
        return repo, None
    except Exception as exc:
        return None, json.dumps({"error": f"Failed to connect to GitHub: {str(exc)}"})


def github_create_branch(branch_name: str, base_branch: str = "master") -> str:
    """Create a new branch in the ocs-ci GitHub repository.

    Creates a branch from the HEAD of the specified base branch.

    Args:
        branch_name: Name for the new branch (e.g. "zstream/4.16.1-marks").
        base_branch: The branch to base the new branch on. Defaults to "master".

    Returns:
        JSON string with the branch name and SHA, or an error message.
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        # Get the base branch reference
        base_ref = repo.get_branch(base_branch)
        base_sha = base_ref.commit.sha

        # Create the new branch
        ref_name = f"refs/heads/{branch_name}"
        repo.create_git_ref(ref=ref_name, sha=base_sha)

        return json.dumps(
            {
                "branch": branch_name,
                "base_branch": base_branch,
                "sha": base_sha,
                "message": f"Branch '{branch_name}' created from '{base_branch}'",
            },
            indent=2,
        )

    except Exception as exc:
        error_msg = str(exc)
        if "Reference already exists" in error_msg:
            return json.dumps(
                {
                    "error": f"Branch '{branch_name}' already exists",
                    "suggestion": "Use a different branch name or delete the existing branch first",
                }
            )
        return json.dumps({"error": f"Failed to create branch: {error_msg}"})


def github_add_mark_to_test(branch: str, file_path: str, mark_name: str) -> str:
    """Add a pytest mark decorator to test functions in a file on a branch.

    Reads the specified test file from the branch, adds @pytest.mark.{mark_name}
    before each test function that doesn't already have it, and commits the change.

    Args:
        branch: The branch to modify (e.g. "zstream/4.16.1-marks").
        file_path: Path to the test file relative to the repo root
                   (e.g. "tests/functional/pv/test_pvc_creation.py").
        mark_name: The pytest mark name to add (e.g. "zstream_4_16_1").

    Returns:
        JSON string with the commit SHA and modified file info, or an error.
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        # Get the current file content from the branch
        try:
            file_content = repo.get_contents(file_path, ref=branch)
        except Exception:
            return json.dumps(
                {
                    "error": f"File '{file_path}' not found on branch '{branch}'",
                }
            )

        content = file_content.decoded_content.decode("utf-8")
        lines = content.split("\n")

        mark_decorator = f"@pytest.mark.{mark_name}"

        # Check if pytest import exists, add if needed
        has_pytest_import = any("import pytest" in line for line in lines)

        new_lines = []
        if not has_pytest_import:
            # Insert import after the last import line
            import_inserted = False
            for line in lines:
                new_lines.append(line)
                if not import_inserted and (line.startswith("import ") or line.startswith("from ")):
                    # Keep going to find the last import
                    pass
            # Simpler: just add at the top after existing imports
            new_lines = []
            inserted = False
            past_imports = False
            for line in lines:
                if not past_imports and not inserted:
                    if (
                        line.strip()
                        and not line.startswith("import ")
                        and not line.startswith("from ")
                        and not line.startswith("#")
                        and not line.startswith('"""')
                        and not line.startswith("'''")
                        and line.strip() != ""
                    ):
                        # We're past the import block
                        if not any("import pytest" in line for line in new_lines):
                            new_lines.append("import pytest")
                            new_lines.append("")
                            inserted = True
                        past_imports = True
                new_lines.append(line)
            if not inserted and not has_pytest_import:
                new_lines.insert(0, "import pytest")
            lines = new_lines

        # Add the mark to test functions that don't have it
        modified_lines = []
        marks_added = 0
        i = 0
        while i < len(lines):
            line = lines[i]

            # Detect test function definitions
            stripped = line.lstrip()
            if stripped.startswith("def test_") or stripped.startswith("async def test_"):
                # Check if the mark is already present in preceding decorators
                has_mark = False
                j = i - 1
                while j >= 0 and (lines[j].lstrip().startswith("@") or lines[j].strip() == ""):
                    if mark_name in lines[j]:
                        has_mark = True
                        break
                    j -= 1

                if not has_mark:
                    indent = len(line) - len(stripped)
                    modified_lines.append(" " * indent + mark_decorator)
                    marks_added += 1

            modified_lines.append(line)
            i += 1

        if marks_added == 0:
            return json.dumps(
                {
                    "message": (
                        f"No modifications needed - mark "
                        f"'{mark_name}' already present "
                        f"or no test functions found"
                    ),
                    "file": file_path,
                    "branch": branch,
                }
            )

        new_content = "\n".join(modified_lines)

        # Commit the change
        commit_message = f"Add @pytest.mark.{mark_name} to tests in {file_path}"
        result = repo.update_file(
            path=file_path,
            message=commit_message,
            content=new_content,
            sha=file_content.sha,
            branch=branch,
        )

        return json.dumps(
            {
                "file": file_path,
                "branch": branch,
                "marks_added": marks_added,
                "mark_name": mark_name,
                "commit_sha": result["commit"].sha,
                "message": f"Added {mark_decorator} to {marks_added} test(s)",
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to add marks: {str(exc)}"})


def github_create_pr(branch: str, title: str, body: str) -> str:
    """Create a pull request in the ocs-ci repository.

    Creates a PR from the specified branch targeting master.

    Args:
        branch: The source branch for the PR.
        title: The PR title.
        body: The PR description body (supports Markdown).

    Returns:
        JSON string with the PR number, URL, and title, or an error message.
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base="master",
        )

        return json.dumps(
            {
                "pr_number": pr.number,
                "url": pr.html_url,
                "title": pr.title,
                "state": pr.state,
                "branch": branch,
                "message": f"PR #{pr.number} created successfully",
            },
            indent=2,
        )

    except Exception as exc:
        error_msg = str(exc)
        if "A pull request already exists" in error_msg:
            return json.dumps(
                {
                    "error": f"A PR already exists for branch '{branch}'",
                    "suggestion": "Check existing PRs or use a different branch",
                }
            )
        return json.dumps({"error": f"Failed to create PR: {error_msg}"})


def github_comment_pr(pr_number: int, comment: str) -> str:
    """Add a comment to an existing pull request.

    Posts an issue comment on the specified PR. Useful for posting
    test results, coverage reports, or status updates.

    Args:
        pr_number: The PR number to comment on.
        comment: The comment text (supports Markdown).

    Returns:
        JSON string confirming the comment was posted, or an error message.
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        pr = repo.get_pull(pr_number)
        issue_comment = pr.create_issue_comment(comment)

        return json.dumps(
            {
                "pr_number": pr_number,
                "comment_id": issue_comment.id,
                "message": f"Comment posted on PR #{pr_number}",
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to comment on PR #{pr_number}: {str(exc)}"})


# Tool-wrapped versions for LangGraph ReAct agents
github_create_branch_tool = tool(github_create_branch)
github_add_mark_to_test_tool = tool(github_add_mark_to_test)
github_create_pr_tool = tool(github_create_pr)
github_comment_pr_tool = tool(github_comment_pr)
