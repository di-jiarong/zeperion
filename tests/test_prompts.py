"""Tests for prompt templates."""

from pathlib import Path

from zeperion.prompts import PromptTemplate, get_template_manager


class TestPromptTemplate:
    """Test prompt template rendering."""

    def test_init_default_dir(self):
        """Test initialization with default templates directory."""
        manager = PromptTemplate()
        assert manager.templates_dir.exists()
        assert (manager.templates_dir / "planner.txt").exists()
        assert (manager.templates_dir / "developer.txt").exists()
        assert (manager.templates_dir / "reviewer.txt").exists()
        assert (manager.templates_dir / "tester.txt").exists()

    def test_render_planner_minimal(self):
        """Test planner prompt with minimal context."""
        manager = PromptTemplate()
        prompt = manager.render_planner(
            requirement="Build a REST API",
            round_num=1,
        )

        assert "计划智能体" in prompt
        assert "Build a REST API" in prompt
        assert "第 1 轮" in prompt
        assert "TASK_ID:" in prompt
        assert "GLOBAL_STATUS:" in prompt

    def test_render_planner_with_context(self):
        """Test planner prompt with full context."""
        manager = PromptTemplate()
        prompt = manager.render_planner(
            requirement="Build a REST API",
            current_plan="Previous plan content",
            test_report="Test failed: missing auth",
            lessons=["Always validate input", "Use async handlers"],
            round_num=3,
        )

        assert "Build a REST API" in prompt
        assert "Previous plan content" in prompt
        assert "Test failed: missing auth" in prompt
        assert "Always validate input" in prompt
        assert "Use async handlers" in prompt
        assert "第 3 轮" in prompt

    def test_render_developer_first_implementation(self):
        """Test developer prompt for first implementation."""
        manager = PromptTemplate()
        prompt = manager.render_developer(
            requirement="Build a REST API",
            plan="Implement user authentication",
            fix_attempt=0,
        )

        assert "开发智能体" in prompt
        assert "Build a REST API" in prompt
        assert "Implement user authentication" in prompt
        assert "首次实现" in prompt
        assert "GLOBAL_STATUS:" in prompt
        assert "CHANGES:" in prompt

    def test_render_developer_fixing_bugs(self):
        """Test developer prompt when fixing bugs."""
        manager = PromptTemplate()
        prompt = manager.render_developer(
            requirement="Build a REST API",
            plan="Implement user authentication",
            test_report="Test failed: password not hashed",
            lessons=["Use bcrypt for passwords"],
            fix_attempt=2,
        )

        assert "Build a REST API" in prompt
        assert "Implement user authentication" in prompt
        assert "Test failed: password not hashed" in prompt
        assert "Use bcrypt for passwords" in prompt
        assert "第 2 次" in prompt
        assert "修复尝试" in prompt

    def test_render_developer_claude_code_mode(self):
        """Test developer prompt for Claude CLI mode."""
        manager = PromptTemplate()
        prompt = manager.render_developer(
            requirement="Build a REST API",
            plan="Implement user authentication",
            uses_claude_code=True,
        )

        assert "必须真实读取并修改项目文件" in prompt
        assert "实际改动过的文件" in prompt

    def test_render_tester(self):
        """Test tester prompt."""
        manager = PromptTemplate()
        prompt = manager.render_tester(
            requirement="Build a REST API",
            plan="Implement user authentication",
            dev_output="Implemented JWT authentication",
            lessons=["Check edge cases"],
        )

        assert "测试智能体" in prompt
        assert "Build a REST API" in prompt
        assert "Implement user authentication" in prompt
        assert "Implemented JWT authentication" in prompt
        assert "Check edge cases" in prompt
        assert "TEST_STATUS:" in prompt
        assert "TEST_CASES:" in prompt

    def test_render_reviewer(self):
        """Test reviewer prompt."""
        manager = PromptTemplate()
        prompt = manager.render_reviewer(
            requirement="Build a REST API",
            plan="Implement user authentication",
            dev_output="Implemented JWT authentication",
            lessons=["Check scope before tests"],
        )

        assert "代码审查智能体" in prompt
        assert "Build a REST API" in prompt
        assert "Implement user authentication" in prompt
        assert "Implemented JWT authentication" in prompt
        assert "Check scope before tests" in prompt
        assert "REVIEW_STATUS:" in prompt
        assert "FINDINGS:" in prompt
        assert "FIX_REQUEST:" in prompt

    def test_render_pr_fixer_lists_comments_and_format(self):
        """PR Fixer prompt should embed every Codex comment and the required output markers."""
        manager = PromptTemplate()
        prompt = manager.render_pr_fixer(
            pr_number=42,
            pr_branch="feature/x",
            pr_target_branch="main",
            comments=[
                {"path": "src/foo.py", "line": 11, "body": "Null check missing"},
                {"path": None, "line": None, "body": "Add a regression test"},
            ],
            lessons=["watch out for off-by-one"],
        )

        assert "PR Fixer" in prompt
        assert "PR 编号：42" in prompt
        assert "feature/x" in prompt
        assert "main" in prompt
        # Both comments should be enumerated.
        assert "Null check missing" in prompt
        assert "Add a regression test" in prompt
        assert "src/foo.py" in prompt
        # General comments should fall back to "(general)" label, not crash.
        assert "(general)" in prompt
        assert "watch out for off-by-one" in prompt
        # Strict output-format markers must survive in the rendered prompt.
        for marker in ("FIX_STATUS:", "FIXED_ISSUES:", "FALSE_POSITIVES:", "REMAINING:", "LESSONS:"):
            assert marker in prompt

    def test_render_pr_fixer_claude_code_branch(self):
        manager = PromptTemplate()
        prompt = manager.render_pr_fixer(
            pr_number=1,
            pr_branch="b",
            pr_target_branch="main",
            comments=[{"body": "fix"}],
            uses_claude_code=True,
        )
        assert "本地 Claude CLI 环境" in prompt

    def test_get_template_manager_singleton(self):
        """``get_template_manager()`` (default dir) is cached per process."""
        manager1 = get_template_manager()
        manager2 = get_template_manager()
        assert manager1 is manager2

    def test_get_template_manager_custom_dir(self):
        """A custom ``templates_dir`` returns a one-shot manager.

        The previous implementation stashed the custom-dir manager
        into the cache, so a later ``get_template_manager(None)``
        returned that custom (likely stale) manager instead of the
        packaged one. Verify that's no longer the case: a custom-dir
        call must NOT mutate the cache.
        """
        custom_dir = Path("/tmp/custom_templates")
        custom_manager = get_template_manager(custom_dir)
        assert custom_manager.templates_dir == custom_dir

        # The default-dir manager must still be the packaged one.
        default_manager = get_template_manager()
        assert default_manager is not custom_manager
        assert default_manager.templates_dir != custom_dir

    def test_custom_dir_calls_do_not_share_state(self):
        """Two calls with different custom dirs must not contaminate each
        other (the singleton-pollution bug rediscovered while wiring up
        ``tester_verify_commands``)."""
        a = get_template_manager(Path("/tmp/a"))
        b = get_template_manager(Path("/tmp/b"))
        assert a is not b
        assert a.templates_dir != b.templates_dir
