"""GitHub operations using gh CLI."""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GitHubClient:
    """GitHub operations wrapper using gh CLI."""

    def __init__(self, token: Optional[str] = None):
        """
        Initialize GitHub client.

        Args:
            token: GitHub token (defaults to GITHUB_TOKEN env var)
        """
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if self.token:
            os.environ["GITHUB_TOKEN"] = self.token

    async def run_gh(self, args: list[str]) -> str:
        """
        Run gh CLI command.

        Args:
            args: Command arguments (without 'gh' prefix)

        Returns:
            Command stdout

        Raises:
            RuntimeError: If command fails
        """
        cmd = ["gh"] + args
        logger.debug(f"Running: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            raise RuntimeError(f"gh command failed: {error_msg}")

        return stdout.decode().strip()

    async def run_git(self, args: list[str]) -> str:
        """
        Run git command.

        Args:
            args: Command arguments (without 'git' prefix)

        Returns:
            Command stdout

        Raises:
            RuntimeError: If command fails
        """
        cmd = ["git"] + args
        logger.debug(f"Running: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            raise RuntimeError(f"git command failed: {error_msg}")

        return stdout.decode().strip()

    async def is_git_repo(self) -> bool:
        """Check if current directory is a git repository."""
        try:
            await self.run_git(["rev-parse", "--git-dir"])
            return True
        except RuntimeError:
            return False

    async def has_gh_cli(self) -> bool:
        """Check if gh CLI is installed."""
        try:
            process = await asyncio.create_subprocess_exec(
                "gh", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0
        except FileNotFoundError:
            return False

    async def get_current_branch(self) -> str:
        """Get current git branch name."""
        return await self.run_git(["branch", "--show-current"])

    async def get_github_repo(self) -> str:
        """
        Get GitHub repo in owner/repo format.

        Returns:
            Repo name (e.g., "owner/repo")

        Raises:
            RuntimeError: If not a GitHub repo
        """
        remote_url = await self.run_git(["remote", "get-url", "origin"])

        # Parse GitHub URL
        # SSH: git@github.com:owner/repo.git
        # HTTPS: https://github.com/owner/repo.git
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote_url)
        if not match:
            raise RuntimeError(f"Not a GitHub repository: {remote_url}")

        return match.group(1)

    async def check_git_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        # Check untracked files
        untracked = await self.run_git([
            "ls-files", "--others", "--exclude-standard",
            "--directory", "--no-empty-directory"
        ])

        # Check modified files
        modified = await self.run_git(["diff", "--name-only"])

        return bool(untracked or modified)

    async def get_changed_files(self) -> list[str]:
        """Get list of changed files."""
        # Untracked files
        untracked = await self.run_git([
            "ls-files", "--others", "--exclude-standard"
        ])
        untracked_files = untracked.split("\n") if untracked else []

        # Modified files
        modified = await self.run_git(["diff", "--name-only"])
        modified_files = modified.split("\n") if modified else []

        return list(set(untracked_files + modified_files))

    async def get_last_commit_subject(self) -> str:
        """Get the subject of the last commit."""
        try:
            return await self.run_git(["log", "-1", "--format=%s"])
        except RuntimeError:
            return "Initial commit"

    async def commit_changes(self, message: str, body: str = "") -> str:
        """
        Commit all changes.

        Args:
            message: Commit message (subject)
            body: Commit body (optional)

        Returns:
            Commit SHA
        """
        # Stage all changes
        await self.run_git(["add", "-A"])

        # Commit
        full_message = message
        if body:
            full_message += f"\n\n{body}"

        await self.run_git(["commit", "-m", full_message])

        # Get commit SHA
        return await self.run_git(["rev-parse", "HEAD"])

    async def push_branch(self, branch: str) -> None:
        """Push branch to origin."""
        await self.run_git(["push", "origin", branch])

    async def find_existing_pr(
        self, repo: str, head: str, base: str
    ) -> Optional[dict]:
        """
        Find existing PR for the given head and base branches.

        Args:
            repo: Repository (owner/repo)
            head: Head branch
            base: Base branch

        Returns:
            PR info dict or None if not found
        """
        try:
            output = await self.run_gh([
                "pr", "list",
                "--repo", repo,
                "--head", head,
                "--base", base,
                "--json", "number,url,title,state",
            ])

            if not output:
                return None

            prs = json.loads(output)
            # Return first open PR
            for pr in prs:
                if pr.get("state") == "OPEN":
                    return pr

            return None
        except RuntimeError:
            return None

    async def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> str:
        """
        Create a new PR.

        Args:
            repo: Repository (owner/repo)
            head: Head branch
            base: Base branch
            title: PR title
            body: PR body

        Returns:
            PR URL
        """
        # Write body to temp file
        body_file = Path("/tmp/zeperion_pr_body.md")
        body_file.write_text(body)

        pr_url = await self.run_gh([
            "pr", "create",
            "--repo", repo,
            "--base", base,
            "--head", head,
            "--title", title,
            "--body-file", str(body_file),
        ])

        return pr_url

    async def update_pr(
        self, repo: str, pr_number: int, title: Optional[str] = None, body: Optional[str] = None
    ) -> None:
        """Update PR title and/or body."""
        args = ["pr", "edit", str(pr_number), "--repo", repo]

        if title:
            args.extend(["--title", title])

        if body:
            body_file = Path("/tmp/zeperion_pr_body.md")
            body_file.write_text(body)
            args.extend(["--body-file", str(body_file)])

        await self.run_gh(args)

    async def generate_pr_body(self, head: str, base: str) -> str:
        """
        Generate PR body from git history.

        Args:
            head: Head branch
            base: Base branch

        Returns:
            PR body markdown
        """
        # Get commits
        commits_output = await self.run_git([
            "log", f"{base}..{head}", "--format=%h %s"
        ])
        commits = commits_output.split("\n") if commits_output else []

        # Get changed files
        files_output = await self.run_git([
            "diff", "--name-only", f"{base}...{head}"
        ])
        files = files_output.split("\n") if files_output else []

        # Build body
        body_parts = []

        if commits:
            body_parts.append("## Commits")
            for commit in commits:
                body_parts.append(f"- {commit}")

        if files:
            body_parts.append("\n## Changed Files")
            for file in files[:20]:  # Limit to 20 files
                body_parts.append(f"- `{file}`")
            if len(files) > 20:
                body_parts.append(f"- ... and {len(files) - 20} more files")

        return "\n".join(body_parts)

    def extract_pr_number(self, pr_url: str) -> int:
        """Extract PR number from URL."""
        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            raise ValueError(f"Cannot extract PR number from URL: {pr_url}")
        return int(match.group(1))

    async def collect_codex_feedback(
        self, repo: str, pr_number: int
    ) -> dict:
        """
        Collect Codex review feedback.

        Args:
            repo: Repository (owner/repo)
            pr_number: PR number

        Returns:
            Dict with thumbs_count, comments_count, reviewed_commit
        """
        # Get PR info
        pr_info = await self.run_gh([
            "api", f"repos/{repo}/pulls/{pr_number}"
        ])
        pr_data = json.loads(pr_info)
        latest_commit = pr_data["head"]["sha"]

        # Get all reactions (PR level)
        pr_reactions = await self._get_reactions(
            f"repos/{repo}/issues/{pr_number}/reactions"
        )

        # Get issue comments
        issue_comments = await self._get_paginated(
            f"repos/{repo}/issues/{pr_number}/comments"
        )

        # Get review comments
        review_comments = await self._get_paginated(
            f"repos/{repo}/pulls/{pr_number}/comments"
        )

        # Get reviews
        reviews = await self._get_paginated(
            f"repos/{repo}/pulls/{pr_number}/reviews"
        )

        # Count Codex thumbs
        codex_thumbs = 0

        # PR reactions
        for reaction in pr_reactions:
            if reaction.get("user", {}).get("login") == "codex" and reaction.get("content") == "+1":
                codex_thumbs += 1

        # Issue comment reactions
        for comment in issue_comments:
            if comment.get("user", {}).get("login") == "codex":
                comment_reactions = await self._get_reactions(
                    f"repos/{repo}/issues/comments/{comment['id']}/reactions"
                )
                for reaction in comment_reactions:
                    if reaction.get("content") == "+1":
                        codex_thumbs += 1

        # Review comment reactions
        for comment in review_comments:
            if comment.get("user", {}).get("login") == "codex":
                comment_reactions = await self._get_reactions(
                    f"repos/{repo}/pulls/comments/{comment['id']}/reactions"
                )
                for reaction in comment_reactions:
                    if reaction.get("content") == "+1":
                        codex_thumbs += 1

        # Count Codex comments
        codex_comments = sum(
            1 for c in issue_comments + review_comments
            if c.get("user", {}).get("login") == "codex"
        )

        # Check if Codex reviewed latest commit
        reviewed_commit = None
        for review in reviews:
            if review.get("user", {}).get("login") == "codex":
                reviewed_commit = review.get("commit_id")
                if reviewed_commit == latest_commit:
                    break

        return {
            "thumbs_count": codex_thumbs,
            "comments_count": codex_comments,
            "reviewed_commit": reviewed_commit,
        }

    async def _get_paginated(self, endpoint: str) -> list:
        """Get paginated API results."""
        try:
            output = await self.run_gh(["api", "--paginate", endpoint])
            return json.loads(output) if output else []
        except RuntimeError:
            return []

    async def _get_reactions(self, endpoint: str) -> list:
        """Get reactions for a resource."""
        try:
            output = await self.run_gh(["api", endpoint])
            return json.loads(output) if output else []
        except RuntimeError:
            return []

    async def enable_auto_merge(self, pr_url: str) -> None:
        """Enable auto-merge for PR."""
        await self.run_gh([
            "pr", "merge", pr_url,
            "--auto",
            "--squash",
            "--delete-branch",
        ])

    async def add_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Add a comment to PR."""
        await self.run_gh([
            "pr", "comment", str(pr_number),
            "--repo", repo,
            "--body", body,
        ])
