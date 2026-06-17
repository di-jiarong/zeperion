"""Tests for Run Workspace: worktree lifecycle, manifest, and the
scoped ``changes`` / ``accept`` / ``discard`` CLI闭环.

The workspace turns one ``multi_agent`` run into an isolated git
transaction (a worktree on a ``zeperion/run/<thread>`` branch) so the
run can be reviewed, accepted, or discarded without touching the user's
working tree. These tests drive real ``git`` against temporary repos.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import types
from pathlib import Path

from typer.testing import CliRunner

from tests.conftest import strip_ansi

from zeperion.cli import _run_post_run_verify, app
from zeperion.models import RunManifest, RunStatus, WorkflowConfig
from zeperion.storage import StateStorage
from zeperion.utils.workspace import (
    apply_workspace_to_current,
    create_run_workspace,
    discard_run_workspace,
    finalize_run_workspace,
    run_branch_for,
    workspace_diff,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "z",
    "GIT_AUTHOR_EMAIL": "z@e.x",
    "GIT_COMMITTER_NAME": "z",
    "GIT_COMMITTER_EMAIL": "z@e.x",
    "PATH": os.environ.get("PATH", ""),
}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=_GIT_ENV, capture_output=True)


def _init_repo(project: Path) -> str:
    """Init a repo with one commit + a .zeperion/ gitignore. Returns HEAD sha."""
    _git(["init", "-b", "main"], project)
    (project / ".gitignore").write_text(".zeperion/\n", encoding="utf-8")
    (project / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "init"], project)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project),
        check=True,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


class TestWorkspaceLifecycle:
    def test_create_finalize_diff(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"

        res = create_run_workspace(tmp_path, "feat-x", worktree_parent=parent)
        assert res.ok, res.error
        ws = res.workspace
        assert ws.run_branch == run_branch_for("feat-x") == "zeperion/run/feat-x"
        assert ws.base_commit == base
        assert Path(ws.worktree_path).is_dir()

        # Simulate an agent: edit a tracked file + add a new one.
        (Path(ws.worktree_path) / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (Path(ws.worktree_path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")

        fin = finalize_run_workspace(tmp_path, ws)
        assert fin.ok, fin.error
        assert fin.final_commit and fin.final_commit != base
        assert sorted(fin.changed_files) == ["feature.py", "tracked.txt"]

        # The user's working tree is untouched.
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "original\n"
        assert not (tmp_path / "feature.py").exists()

        diff = workspace_diff(tmp_path, base, fin.final_commit)
        assert diff.ok
        assert "feature.py" in diff.diff
        assert "changed" in diff.diff

    def test_finalize_clean_worktree_is_noop(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "noop", worktree_parent=parent)
        assert res.ok
        fin = finalize_run_workspace(tmp_path, res.workspace)
        assert fin.ok
        assert fin.final_commit == base
        assert fin.changed_files == []

    def test_apply_stages_changes_without_commit(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "apply", worktree_parent=parent)
        (Path(res.workspace.worktree_path) / "feature.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        fin = finalize_run_workspace(tmp_path, res.workspace)

        applied = apply_workspace_to_current(tmp_path, base, fin.final_commit)
        assert applied.ok, applied.error
        # The new file is now present and *staged* in the user's tree, but
        # HEAD has not moved (apply-only, no commit).
        assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            check=True,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head_after == base
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(tmp_path),
            check=True,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert "feature.py" in staged

    def test_discard_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "drop", worktree_parent=parent)
        ws = res.workspace
        assert Path(ws.worktree_path).exists()

        result = discard_run_workspace(tmp_path, ws.run_branch, ws.worktree_path)
        assert result.ok, result.error
        assert not Path(ws.worktree_path).exists()
        # Branch is gone.
        branches = subprocess.run(
            ["git", "branch", "--list", ws.run_branch],
            cwd=str(tmp_path),
            check=True,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
        ).stdout
        assert ws.run_branch not in branches

    def test_create_non_repo_returns_structured_error(self, tmp_path: Path) -> None:
        res = create_run_workspace(tmp_path, "x", worktree_parent=tmp_path / "wt")
        assert res.ok is False
        assert res.is_repo is False

    def test_create_reuses_existing_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        first = create_run_workspace(tmp_path, "resume", worktree_parent=parent)
        assert first.ok
        second = create_run_workspace(tmp_path, "resume", worktree_parent=parent)
        assert second.ok, second.error
        assert second.workspace.worktree_path == first.workspace.worktree_path

    def test_create_reset_rebases_on_new_head(self, tmp_path: Path) -> None:
        """A fresh run (reset=True) must not inherit the prior transaction.

        After a first run commits work, advancing HEAD and re-creating with
        reset=True should anchor base_commit at the *new* HEAD and start the
        run branch empty (no carried-over commits from the previous run).
        """
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"

        first = create_run_workspace(tmp_path, "feat", worktree_parent=parent)
        (Path(first.workspace.worktree_path) / "old.py").write_text("1\n")
        finalize_run_workspace(tmp_path, first.workspace)

        # Advance the user's HEAD independently.
        (tmp_path / "tracked.txt").write_text("v2\n", encoding="utf-8")
        _git(["commit", "-am", "advance"], tmp_path)
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(tmp_path),
            check=True,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_head != base

        second = create_run_workspace(
            tmp_path, "feat", worktree_parent=parent, reset=True
        )
        assert second.ok, second.error
        assert second.workspace.base_commit == new_head
        # The previous run's file is gone from the fresh worktree.
        assert not (Path(second.workspace.worktree_path) / "old.py").exists()

    def test_apply_failure_leaves_tree_untouched(self, tmp_path: Path) -> None:
        """When the patch cannot apply cleanly, nothing is written.

        The run edits ``tracked.txt`` from the original; meanwhile the user
        rewrites the same file. The run's diff no longer applies — and the
        user's content must survive intact (no conflict markers).
        """
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "conflict", worktree_parent=parent)
        (Path(res.workspace.worktree_path) / "tracked.txt").write_text(
            "run version\n", encoding="utf-8"
        )
        fin = finalize_run_workspace(tmp_path, res.workspace)

        # Diverge the user's copy and commit so HEAD != base.
        (tmp_path / "tracked.txt").write_text("totally different\n", encoding="utf-8")
        _git(["commit", "-am", "user edit"], tmp_path)

        applied = apply_workspace_to_current(tmp_path, base, fin.final_commit)
        assert applied.ok is False
        assert "untouched" in (applied.error or "")
        # User's content is intact; no conflict markers were written.
        content = (tmp_path / "tracked.txt").read_text(encoding="utf-8")
        assert content == "totally different\n"
        assert "<<<<<<<" not in content

    def test_apply_supports_binary_files(self, tmp_path: Path) -> None:
        base = _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "bin", worktree_parent=parent)
        # A binary blob with NUL bytes the agent "produced".
        blob = bytes(range(256)) * 4
        (Path(res.workspace.worktree_path) / "asset.bin").write_bytes(blob)
        fin = finalize_run_workspace(tmp_path, res.workspace)
        assert "asset.bin" in fin.changed_files

        applied = apply_workspace_to_current(tmp_path, base, fin.final_commit)
        assert applied.ok, applied.error
        assert (tmp_path / "asset.bin").read_bytes() == blob

    def test_run_git_never_raises_on_bad_cwd(self, tmp_path: Path) -> None:
        from zeperion.utils.workspace import _run_git

        proc = _run_git(["status"], tmp_path / "does-not-exist", timeout=5)
        assert proc.returncode != 0
        assert proc.stderr

    def test_create_invalid_parent_returns_structured_error(
        self, tmp_path: Path
    ) -> None:
        _init_repo(tmp_path)
        # worktree_parent lives *under a regular file* → mkdir would raise
        # NotADirectoryError; create_run_workspace must convert that into a
        # structured ok=False instead of propagating.
        a_file = tmp_path / "afile"
        a_file.write_text("x", encoding="utf-8")
        res = create_run_workspace(
            tmp_path, "x", worktree_parent=a_file / "nested"
        )
        assert res.ok is False
        assert res.error

    def test_finalize_reports_failure_not_empty_on_git_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failing ``git status`` must NOT be reported as 'no changes'.

        Otherwise the manifest would claim the run produced nothing and a
        later fresh run could delete real uncommitted work.
        """
        import zeperion.utils.workspace as wsmod

        _init_repo(tmp_path)
        parent = tmp_path / ".zeperion" / "state" / "worktrees"
        res = create_run_workspace(tmp_path, "feat", worktree_parent=parent)
        (Path(res.workspace.worktree_path) / "feature.py").write_text("1\n")

        real_run_git = wsmod._run_git

        def _fail_status(args, cwd, **kwargs):
            if args[:2] == ["status", "--porcelain"]:
                import subprocess

                return subprocess.CompletedProcess(
                    args=["git", *args], returncode=1, stdout="", stderr="boom"
                )
            return real_run_git(args, cwd, **kwargs)

        monkeypatch.setattr(wsmod, "_run_git", _fail_status)
        fin = finalize_run_workspace(tmp_path, res.workspace)
        assert fin.ok is False
        assert fin.changed_files == []
        assert "boom" in (fin.error or "")


class TestRunManifestStorage:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path / "state", thread_id="feat")
        manifest = RunManifest(
            thread_id="feat",
            status=RunStatus.FINISHED,
            base_commit="abc123",
            run_branch="zeperion/run/feat",
            worktree_path=str(tmp_path / "wt"),
            final_commit="def456",
            changed_files=["a.py", "b.py"],
        )
        storage.save_run_manifest(manifest.model_dump(mode="json"))

        loaded = storage.load_run_manifest()
        assert loaded is not None
        assert loaded["thread_id"] == "feat"
        assert loaded["status"] == "finished"
        assert loaded["changed_files"] == ["a.py", "b.py"]

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path / "state", thread_id="none")
        assert storage.load_run_manifest() is None


def _write_config(project: Path) -> Path:
    state_dir = project / ".zeperion" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (project / "requirement.txt").write_text("build", encoding="utf-8")
    # Commit it so the working tree stays clean — ``accept`` refuses to
    # apply onto a dirty tree (it would mix the run with unrelated edits).
    if (project / ".git").exists():
        _git(["add", "requirement.txt"], project)
        _git(["commit", "-m", "add requirement"], project)
    config_path = project / ".zeperion" / "config.yaml"
    config_path.write_text(
        f"requirement_file: {project / 'requirement.txt'}\n"
        f"state_dir: {state_dir}\n"
        f"project_dir: {project}\n"
        "planner_agent_type: anthropic\n"
        "developer_agent_type: anthropic\n"
        "reviewer_agent_type: anthropic\n"
        "tester_agent_type: anthropic\n",
        encoding="utf-8",
    )
    return config_path


def _seed_finished_run(project: Path, thread: str) -> RunManifest:
    """Create a worktree, make an edit, finalize, and persist the manifest.

    Mimics what ``zeperion run`` does so the CLI commands have a real run
    to operate on.
    """
    parent = project / ".zeperion" / "state" / "worktrees"
    res = create_run_workspace(project, thread, worktree_parent=parent)
    assert res.ok, res.error
    ws = res.workspace
    (Path(ws.worktree_path) / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
    fin = finalize_run_workspace(project, ws)
    assert fin.ok, fin.error
    manifest = RunManifest(
        thread_id=thread,
        status=RunStatus.FINISHED,
        base_branch=ws.base_branch,
        base_commit=ws.base_commit,
        run_branch=ws.run_branch,
        worktree_path=ws.worktree_path,
        final_commit=fin.final_commit,
        changed_files=fin.changed_files,
    )
    StateStorage(project / ".zeperion" / "state", thread_id=thread).save_run_manifest(
        manifest.model_dump(mode="json")
    )
    return manifest


class TestScopedChangesAcceptDiscard:
    def test_changes_shows_only_this_run(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")

        result = CliRunner().invoke(
            app, ["changes", "-c", str(config_path), "-t", "feat"]
        )
        assert result.exit_code == 0, result.output
        assert "feature.py" in result.output
        assert "zeperion accept -t feat" in result.output

    def test_accept_applies_staged_and_marks_manifest(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")

        result = CliRunner().invoke(
            app, ["accept", "-c", str(config_path), "-t", "feat"]
        )
        assert result.exit_code == 0, result.output
        plain = strip_ansi(result.output)
        assert "Applied (staged)" in plain
        # The run's file landed in the user's working tree.
        assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "VALUE = 42\n"
        # Manifest flipped to accepted.
        loaded = StateStorage(
            tmp_path / ".zeperion" / "state", thread_id="feat"
        ).load_run_manifest()
        assert loaded["status"] == "accepted"
        assert loaded["accepted_at"]

    def test_discard_removes_run_not_working_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        manifest = _seed_finished_run(tmp_path, "feat")
        # Put an unrelated user edit in the working tree; discard must keep it.
        (tmp_path / "tracked.txt").write_text("my own edit\n", encoding="utf-8")

        result = CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert "Discarded run" in result.output
        # User's edit is preserved; the run's worktree/branch are gone.
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "my own edit\n"
        assert not Path(manifest.worktree_path).exists()
        loaded = StateStorage(
            tmp_path / ".zeperion" / "state", thread_id="feat"
        ).load_run_manifest()
        assert loaded["status"] == "discarded"

    def test_discard_refuses_without_yes(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        manifest = _seed_finished_run(tmp_path, "feat")

        result = CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat"]
        )
        assert result.exit_code == 1, result.output
        assert "Refusing to discard" in result.output
        # Nothing removed.
        assert Path(manifest.worktree_path).exists()

    def test_accept_refuses_on_dirty_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")
        # Unrelated user edit makes the tree dirty; accept must refuse so it
        # does not interleave the run's result with the user's own work.
        (tmp_path / "tracked.txt").write_text("my wip\n", encoding="utf-8")

        result = CliRunner().invoke(
            app, ["accept", "-c", str(config_path), "-t", "feat"]
        )
        assert result.exit_code == 1, result.output
        assert "dirty working tree" in result.output
        # Run's file was NOT applied.
        assert not (tmp_path / "feature.py").exists()

    def test_accept_allow_dirty_applies_anyway(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")
        (tmp_path / "tracked.txt").write_text("my wip\n", encoding="utf-8")

        result = CliRunner().invoke(
            app, ["accept", "-c", str(config_path), "-t", "feat", "--allow-dirty"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "VALUE = 42\n"
        # The user's own edit is still there too.
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "my wip\n"

    def test_discard_twice_is_noop_and_keeps_working_tree(
        self, tmp_path: Path
    ) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")

        first = CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat", "--yes"]
        )
        assert first.exit_code == 0, first.output

        # User makes edits AFTER discarding. A second discard must NOT fall
        # into the legacy whole-tree reset and wipe them.
        (tmp_path / "tracked.txt").write_text("precious\n", encoding="utf-8")
        (tmp_path / "new_user_file.txt").write_text("keep me\n", encoding="utf-8")

        second = CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat", "--yes"]
        )
        assert second.exit_code == 0, second.output
        assert "already discarded" in second.output
        # Working tree edits survived.
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "precious\n"
        assert (tmp_path / "new_user_file.txt").exists()

    def test_changes_on_discarded_run_reports_discarded(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")
        CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat", "--yes"]
        )
        # Make the whole tree dirty; ``changes`` on a discarded run must not
        # show it as if it were the run's diff.
        (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        result = CliRunner().invoke(
            app, ["changes", "-c", str(config_path), "-t", "feat"]
        )
        assert result.exit_code == 0, result.output
        assert "was discarded" in result.output
        assert "tracked.txt" not in result.output

    def test_accept_without_manifest_errors(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        result = CliRunner().invoke(
            app, ["accept", "-c", str(config_path), "-t", "absent"]
        )
        assert result.exit_code == 1
        assert "No Run Workspace" in result.output

    def test_new_run_refuses_to_clobber_unreviewed_run(
        self, tmp_path: Path
    ) -> None:
        """A new (non-resume) run must NOT silently delete a prior
        active/finished/blocked Run Workspace — that could lose unreviewed
        work or yank a worktree from a still-running task."""
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")  # status = finished (non-terminal)

        result = CliRunner().invoke(
            app,
            [
                "run",
                "--mode",
                "multi_agent",
                "-c",
                str(config_path),
                "-t",
                "feat",
                "--yes",
            ],
        )
        assert result.exit_code == 1, result.output
        assert "Refusing to start a new run" in result.output
        # The prior run's worktree/branch are untouched.
        manifest = StateStorage(
            tmp_path / ".zeperion" / "state", thread_id="feat"
        ).load_run_manifest()
        assert manifest["status"] == "finished"
        assert Path(manifest["worktree_path"]).exists()

    def test_new_run_proceeds_after_discard(self, tmp_path: Path) -> None:
        """Once the prior run is discarded (terminal), a new run is free to
        reset without the safety refusal."""
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        _seed_finished_run(tmp_path, "feat")
        CliRunner().invoke(
            app, ["discard", "-c", str(config_path), "-t", "feat", "--yes"]
        )

        result = CliRunner().invoke(
            app,
            [
                "run",
                "--mode",
                "multi_agent",
                "-c",
                str(config_path),
                "-t",
                "feat",
                "--yes",
            ],
        )
        # It must get *past* the refusal gate (it may then fail for unrelated
        # reasons — no real agent backend — but never with the refusal text).
        assert "Refusing to start a new run" not in result.output

    def test_changes_falls_back_to_whole_tree_without_manifest(
        self, tmp_path: Path
    ) -> None:
        # No manifest for thread "main" → legacy whole-tree view.
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        result = CliRunner().invoke(
            app, ["changes", "-c", str(config_path), "-t", "main"]
        )
        assert result.exit_code == 0, result.output
        assert "tracked.txt" in result.output


class TestStatusSurfacesRunWorkspace:
    def test_status_banner_and_verify_when_pending(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        config_path = _write_config(tmp_path)
        manifest = _seed_finished_run(tmp_path, "feat")
        # Mark the run's verification as failed so status surfaces it.
        manifest.verify_status = "fail"
        manifest.verify_results = [
            {
                "command": "pytest -q",
                "passed": False,
                "exit_code": 1,
                "duration_ms": 5,
                "timed_out": False,
                "tail": "boom",
            }
        ]
        StateStorage(
            tmp_path / ".zeperion" / "state", thread_id="feat"
        ).save_run_manifest(manifest.model_dump(mode="json"))

        result = CliRunner().invoke(app, ["status", "-c", str(config_path), "-t", "feat"])
        assert result.exit_code == 0, result.output
        assert "awaiting your review" in result.output
        assert "verify FAILED" in result.output or "FAILED" in result.output
        # Verify-failed steers the operator toward fixing (resume) first.
        assert "--resume" in result.output


class _CaptureConsole:
    """Minimal stand-in for rich.Console capturing printed text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:  # noqa: D401 - test stub
        self.lines.append(" ".join(str(a) for a in args))


class TestPostRunVerify:
    def test_records_pass(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        config = WorkflowConfig(
            project_dir=str(tmp_path),
            requirement_file="requirement.txt",
            tester_verify_commands=["true"],
            tester_verify_timeout_seconds=30,
        )
        manifest = RunManifest(
            thread_id="feat",
            base_commit="abc",
            run_branch="zeperion/run/feat",
            worktree_path=str(worktree),
        )
        ws = types.SimpleNamespace(worktree_path=str(worktree))
        asyncio.run(
            _run_post_run_verify(
                config=config, workspace=ws, manifest=manifest, out=_CaptureConsole()
            )
        )
        assert manifest.verify_status == "pass"
        assert manifest.verify_passed is True
        assert manifest.verify_results and manifest.verify_results[0]["passed"]

    def test_records_fail_with_tail(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        config = WorkflowConfig(
            project_dir=str(tmp_path),
            requirement_file="requirement.txt",
            tester_verify_commands=["echo boom; exit 1"],
            tester_verify_timeout_seconds=30,
        )
        manifest = RunManifest(
            thread_id="feat",
            base_commit="abc",
            run_branch="zeperion/run/feat",
            worktree_path=str(worktree),
        )
        ws = types.SimpleNamespace(worktree_path=str(worktree))
        asyncio.run(
            _run_post_run_verify(
                config=config, workspace=ws, manifest=manifest, out=_CaptureConsole()
            )
        )
        assert manifest.verify_status == "fail"
        assert manifest.verify_passed is False
        assert "boom" in manifest.verify_results[0]["tail"]

    def test_no_commands_marks_skipped(self, tmp_path: Path) -> None:
        # An empty project dir → no auto-detected commands either.
        worktree = tmp_path / "wt"
        worktree.mkdir()
        config = WorkflowConfig(
            project_dir=str(tmp_path),
            requirement_file="requirement.txt",
            tester_verify_commands=[],
        )
        manifest = RunManifest(
            thread_id="feat",
            base_commit="abc",
            run_branch="zeperion/run/feat",
            worktree_path=str(worktree),
        )
        ws = types.SimpleNamespace(worktree_path=str(worktree))
        asyncio.run(
            _run_post_run_verify(
                config=config, workspace=ws, manifest=manifest, out=_CaptureConsole()
            )
        )
        assert manifest.verify_status == "skipped"
        assert manifest.verify_passed is None
