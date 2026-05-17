"""GitHub tools for branch management, test marking, and PR creation."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from core import config


def _signed_msg(msg: str) -> str:
    name = config.GIT_AUTHOR_NAME
    email = config.GIT_AUTHOR_EMAIL
    if name and email:
        return f"{msg}\n\nSigned-off-by: {name} <{email}>"
    return msg


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
        base_branch: The branch to base the new branch on (e.g. "release-4.20").

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
    """Add a pytest mark to test classes/functions in a file.

    Follows ocs-ci conventions:
    - Adds import from marks.py (not raw @pytest.mark)
    - Applies @mark_name at class level if class exists, else per function
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        try:
            file_content = repo.get_contents(file_path, ref=branch)
        except Exception:
            return json.dumps(
                {"error": f"File '{file_path}' not found on branch '{branch}'"}
            )

        content = file_content.decoded_content.decode("utf-8")
        if mark_name in content:
            return json.dumps(
                {"message": f"Mark '{mark_name}' already present", "file": file_path}
            )

        lines = content.split("\n")

        # Step 1: Add import — append to existing marks import or add new one
        marks_import = "ocs_ci.framework.pytest_customization.marks"
        import_added = False
        new_lines = []
        in_marks_import = False
        for line in lines:
            if not import_added and marks_import in line and "import" in line:
                in_marks_import = True
                new_lines.append(line)
                # Single-line import (no parenthesis)
                if "(" not in line:
                    new_lines[-1] = f"{line.rstrip()}, {mark_name}"
                    import_added = True
                    in_marks_import = False
                continue
            if in_marks_import:
                if line.strip() == ")":
                    new_lines.append(f"    {mark_name},")
                    new_lines.append(line)
                    import_added = True
                    in_marks_import = False
                    continue
            new_lines.append(line)

        if not import_added:
            insert_idx = 0
            for i, line in enumerate(new_lines):
                if line.startswith("import ") or line.startswith("from "):
                    insert_idx = i + 1
                    while insert_idx < len(new_lines) and (
                        new_lines[insert_idx].startswith("    ")
                        or new_lines[insert_idx].strip() == ")"
                    ):
                        insert_idx += 1
            new_lines.insert(
                insert_idx,
                f"from {marks_import} import {mark_name}",
            )

        lines = new_lines

        # Step 2: Add @mark_name decorator at class or function level
        modified_lines = []
        marks_added = 0
        classes_marked = set()

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Mark at class level (top-level classes containing test methods)
            if stripped.startswith("class ") and indent == 0:
                has_test_methods = any(
                    l.lstrip().startswith("def test_")
                    for l in lines[i + 1 : min(i + 200, len(lines))]
                    if l.startswith("    ")
                )
                if has_test_methods:
                    already = any(
                        mark_name in lines[j]
                        for j in range(max(0, i - 5), i)
                        if lines[j].lstrip().startswith("@")
                    )
                    if not already:
                        modified_lines.append(f"@{mark_name}")
                        marks_added += 1
                        classes_marked.add(i)

            # Mark standalone test functions (not in a class)
            elif (
                stripped.startswith("def test_")
                and indent == 0
            ):
                already = any(
                    mark_name in lines[j]
                    for j in range(max(0, i - 5), i)
                    if lines[j].lstrip().startswith("@")
                )
                if not already:
                    modified_lines.append(f"@{mark_name}")
                    marks_added += 1

            modified_lines.append(line)

        if marks_added == 0:
            return json.dumps(
                {
                    "message": f"No test classes/functions found in {file_path}",
                    "file": file_path,
                }
            )

        new_content = "\n".join(modified_lines)
        commit_message = _signed_msg(f"Add @{mark_name} to {file_path}")
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
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to add marks: {str(exc)}"})


def github_create_pr(branch: str, title: str, body: str, base_branch: str = "master") -> str:
    """Create a pull request in the ocs-ci repository.

    Args:
        branch: The source branch for the PR.
        title: The PR title.
        body: The PR description body (supports Markdown).
        base_branch: Target branch for the PR (e.g. "release-4.20").

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
            base=base_branch,
        )

        try:
            pr.add_to_labels("Automatic AI Generated")
        except Exception:
            pass

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


def github_get_pr_files(pr_url: str) -> str:
    """Fetch the list of changed files from a GitHub PR.

    Works with any GitHub repo, not just ocs-ci. Parses the PR URL
    to extract owner/repo/number.

    Args:
        pr_url: Full GitHub PR URL (e.g. "https://github.com/red-hat-storage/rook/pull/1136")

    Returns:
        JSON string with repo, pr_number, and list of changed files
        with filename, status, additions, deletions, and patch snippet.
    """
    import re

    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        return json.dumps({"error": f"Cannot parse PR URL: {pr_url}"})

    owner, repo_name, pr_number = match.group(1), match.group(2), int(match.group(3))

    if not config.GITHUB_TOKEN:
        return json.dumps({"error": "GITHUB_TOKEN not configured"})

    try:
        from github import Github

        gh = Github(config.GITHUB_TOKEN)
        repo = gh.get_repo(f"{owner}/{repo_name}")
        pr = repo.get_pull(pr_number)

        files = []
        for f in pr.get_files():
            files.append(
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "changes": f.changes,
                    "patch": (f.patch or "")[:500],
                }
            )

        return json.dumps(
            {
                "repo": f"{owner}/{repo_name}",
                "pr_number": pr_number,
                "title": pr.title,
                "state": pr.state,
                "files_changed": len(files),
                "files": files,
            },
            indent=2,
        )

    except Exception as exc:
        return json.dumps({"error": f"Failed to fetch PR {pr_url}: {str(exc)}"})


def github_register_marker(branch: str, mark_name: str, description: str) -> str:
    """Register a pytest marker in pytest.ini on the given branch.

    Appends the marker to the markers list in pytest.ini.

    Args:
        branch: The branch to modify.
        mark_name: The marker name (e.g. "zstream_4_20_5").
        description: Description for the marker.

    Returns:
        JSON string with result or error.
    """
    repo, error = _get_repo()
    if error:
        return error

    try:
        file_content = repo.get_contents("pytest.ini", ref=branch)
        content = file_content.decoded_content.decode("utf-8")

        if mark_name in content:
            return json.dumps({"message": f"Marker '{mark_name}' already registered"})

        lines = content.split("\n")
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip().startswith("markers"):
                new_lines.append(f"    {mark_name}: {description}")
                inserted = True

        if not inserted:
            return json.dumps({"error": "Could not find markers section in pytest.ini"})

        new_content = "\n".join(new_lines)
        repo.update_file(
            path="pytest.ini",
            message=_signed_msg(
                f"Register @pytest.mark.{mark_name} in pytest.ini"
            ),
            content=new_content,
            sha=file_content.sha,
            branch=branch,
        )

        return json.dumps({"message": f"Marker '{mark_name}' registered in pytest.ini"})

    except Exception as exc:
        return json.dumps({"error": f"Failed to register marker: {str(exc)}"})


def github_register_mark_in_marks_py(branch: str, mark_name: str) -> str:
    """Add a marker variable to marks.py on the given branch.

    Appends e.g. `zstream_4_16_13 = pytest.mark.zstream_4_16_13`
    so test files can import it.
    """
    repo, error = _get_repo()
    if error:
        return error

    marks_path = "ocs_ci/framework/pytest_customization/marks.py"
    try:
        file_content = repo.get_contents(marks_path, ref=branch)
        content = file_content.decoded_content.decode("utf-8")

        if f"{mark_name} = " in content:
            return json.dumps(
                {"message": f"Mark '{mark_name}' already in marks.py"}
            )

        new_line = f"\n# z-stream marker\n{mark_name} = pytest.mark.{mark_name}\n"
        new_content = content.rstrip() + new_line

        repo.update_file(
            path=marks_path,
            message=_signed_msg(
                f"Add {mark_name} to marks.py"
            ),
            content=new_content,
            sha=file_content.sha,
            branch=branch,
        )

        return json.dumps(
            {"message": f"Mark '{mark_name}' added to marks.py"}
        )

    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to update marks.py: {str(exc)}"}
        )


# Tool-wrapped versions for LangGraph ReAct agents
github_create_branch_tool = tool(github_create_branch)
github_add_mark_to_test_tool = tool(github_add_mark_to_test)
github_create_pr_tool = tool(github_create_pr)
github_comment_pr_tool = tool(github_comment_pr)
github_get_pr_files_tool = tool(github_get_pr_files)
