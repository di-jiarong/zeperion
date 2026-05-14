"""GitHub operations using gh CLI."""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_CODEX_LOGINS: tuple[str, ...] = (
    "chatgpt-codex-connector",
    "chatgpt-codex-connector[bot]",
    "codex",
    "codex[bot]",
)


class GitHubClient:
    """GitHub operations wrapper using gh CLI."""

    def __init__(
        self,
        token: Optional[str] = None,
        codex_logins: Optional[Iterable[str]] = None,
    ):
        """
        Initialize GitHub client.

        Args:
            token: GitHub token (defaults to GITHUB_TOKEN env var)
            codex_logins: GitHub login names that should be treated as the
                Codex review bot. Defaults to the historically observed
                identities. Any login ending with ``[bot]`` containing
                "codex" is also recognised.
        """
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if self.token:
            os.environ["GITHUB_TOKEN"] = self.token

        self._codex_logins: set[str] = {
            login.lower() for login in (codex_logins or DEFAULT_CODEX_LOGINS)
        }

    def is_codex_user(self, login: Optional[str]) -> bool:
        """Return True when the GitHub login matches a Codex bot."""
        if not login:
            return False
        normalized = login.lower()
        if normalized in self._codex_logins:
            return True
        return normalized.endswith("[bot]") and "codex" in normalized

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
        with _temporary_body_file(body) as body_file:
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
            with _temporary_body_file(body) as body_file:
                args.extend(["--body-file", str(body_file)])
                await self.run_gh(args)
            return

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
        pr_info = await self.run_gh([
            "api", f"repos/{repo}/pulls/{pr_number}"
        ])
        pr_data = json.loads(pr_info)
        latest_commit = pr_data["head"]["sha"]

        pr_reactions = await self._get_paginated(
            f"repos/{repo}/issues/{pr_number}/reactions"
        )

        issue_comments = await self._get_paginated(
            f"repos/{repo}/issues/{pr_number}/comments"
        )

        review_comments = await self._get_paginated(
            f"repos/{repo}/pulls/{pr_number}/comments"
        )

        reviews = await self._get_paginated(
            f"repos/{repo}/pulls/{pr_number}/reviews"
        )

        codex_thumbs = 0

        for reaction in pr_reactions:
            if (
                self.is_codex_user(reaction.get("user", {}).get("login"))
                and reaction.get("content") == "+1"
            ):
                codex_thumbs += 1

        for comment in issue_comments:
            if self.is_codex_user(comment.get("user", {}).get("login")):
                comment_reactions = await self._get_paginated(
                    f"repos/{repo}/issues/comments/{comment['id']}/reactions"
                )
                for reaction in comment_reactions:
                    if reaction.get("content") == "+1":
                        codex_thumbs += 1

        for comment in review_comments:
            if self.is_codex_user(comment.get("user", {}).get("login")):
                comment_reactions = await self._get_paginated(
                    f"repos/{repo}/pulls/comments/{comment['id']}/reactions"
                )
                for reaction in comment_reactions:
                    if reaction.get("content") == "+1":
                        codex_thumbs += 1

        codex_comments = sum(
            1
            for c in issue_comments + review_comments
            if self.is_codex_user(c.get("user", {}).get("login"))
        )

        # Latest Codex review on the latest commit wins.
        reviewed_commit = None
        for review in reviews:
            if not self.is_codex_user(review.get("user", {}).get("login")):
                continue
            reviewed_commit = review.get("commit_id")
            if reviewed_commit == latest_commit:
                break

        return {
            "thumbs_count": codex_thumbs,
            "comments_count": codex_comments,
            "reviewed_commit": reviewed_commit,
        }

    async def _get_paginated(self, endpoint: str) -> list:
        """Get paginated API results.

        ``gh api --paginate`` concatenates the JSON document from each page;
        for list-typed endpoints that means multiple ``[...]`` arrays glued
        back-to-back, which is not a single valid JSON document. We instead
        walk pages explicitly and merge the results.
        """
        items: list = []
        page = 1
        while True:
            paged = _with_page(endpoint, page)
            try:
                output = await self.run_gh(["api", paged])
            except RuntimeError as exc:
                logger.warning("gh api %s failed: %s", paged, exc)
                break

            if not output:
                break

            try:
                data = json.loads(output)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON from %s: %s", paged, exc)
                break

            if isinstance(data, list):
                if not data:
                    break
                items.extend(data)
                if len(data) < 100:
                    break
                page += 1
            else:
                if isinstance(data, dict):
                    items.append(data)
                break

        return items

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _with_page(endpoint: str, page: int, per_page: int = 100) -> str:
    """Append/replace the page/per_page query parameters on a gh endpoint."""
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}per_page={per_page}&page={page}"


class _temporary_body_file:
    """Context manager yielding a temp file populated with ``body``."""

    def __init__(self, body: str):
        self.body = body
        self._path: Optional[Path] = None

    def __enter__(self) -> Path:
        fd, path = tempfile.mkstemp(prefix="zeperion_pr_body_", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.body)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        self._path = Path(path)
        return self._path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None
