"""Prompt template management."""

from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader


PACKAGED_TEMPLATES_DIR = Path(__file__).parent / "templates"


def resolve_templates_dir(templates_dir: Optional[Path | str] = None) -> Path:
    """Resolve the prompt templates directory.

    - If ``templates_dir`` is provided, it is returned as-is (even if it does
      not yet exist) so callers can fail loudly via Jinja2 when their
      configuration is wrong.
    - If ``templates_dir`` is ``None``, the packaged
      ``zeperion/prompts/templates`` directory is used. This works regardless
      of the process working directory (relevant after ``pip install``).
    """
    if templates_dir is not None:
        return Path(templates_dir)
    return PACKAGED_TEMPLATES_DIR


class PromptTemplate:
    """Manages prompt templates for agents."""

    def __init__(self, templates_dir: Optional[Path | str] = None):
        """Initialize prompt template manager.

        Args:
            templates_dir: Directory containing template files. When omitted
                or missing on disk, the packaged ``templates/`` directory
                shipped with ``zeperion.prompts`` is used.
        """
        self.templates_dir = resolve_templates_dir(templates_dir)
        self.env = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **context) -> str:
        """Render a template with given context.

        Args:
            template_name: Name of template file (e.g., "planner.txt")
            **context: Variables to pass to template

        Returns:
            Rendered prompt string
        """
        template = self.env.get_template(template_name)
        return template.render(**context)

    def render_planner(
        self,
        requirement: str,
        current_plan: Optional[str] = None,
        test_report: Optional[str] = None,
        lessons: Optional[list[str]] = None,
        round_num: int = 1,
    ) -> str:
        """Render planner prompt.

        Args:
            requirement: User requirement text
            current_plan: Previous plan (if any)
            test_report: Latest test report (if any)
            lessons: Lessons learned so far
            round_num: Current round number

        Returns:
            Rendered planner prompt
        """
        return self.render(
            "planner.txt",
            requirement=requirement,
            current_plan=current_plan or "无",
            test_report=test_report or "无",
            lessons=lessons or [],
            round_num=round_num,
        )

    def render_developer(
        self,
        requirement: str,
        plan: str,
        test_report: Optional[str] = None,
        lessons: Optional[list[str]] = None,
        fix_attempt: int = 0,
        uses_claude_code: bool = False,
    ) -> str:
        """Render developer prompt.

        Args:
            requirement: User requirement text
            plan: Current plan from planner
            test_report: Test report (if fixing bugs)
            lessons: Lessons learned so far
            fix_attempt: Current fix attempt number (0 = first implementation)
            uses_claude_code: Whether the agent can directly edit project files

        Returns:
            Rendered developer prompt
        """
        return self.render(
            "developer.txt",
            requirement=requirement,
            plan=plan,
            test_report=test_report or "无",
            lessons=lessons or [],
            fix_attempt=fix_attempt,
            is_fixing=fix_attempt > 0,
            uses_claude_code=uses_claude_code,
        )

    def render_tester(
        self,
        requirement: str,
        plan: str,
        dev_output: str,
        lessons: Optional[list[str]] = None,
    ) -> str:
        """Render tester prompt.

        Args:
            requirement: User requirement text
            plan: Current plan from planner
            dev_output: Developer's output
            lessons: Lessons learned so far

        Returns:
            Rendered tester prompt
        """
        return self.render(
            "tester.txt",
            requirement=requirement,
            plan=plan,
            dev_output=dev_output,
            lessons=lessons or [],
        )


# Global instance for convenience
_default_template_manager: Optional[PromptTemplate] = None


def get_template_manager(templates_dir: Optional[Path] = None) -> PromptTemplate:
    """Get or create the default template manager.

    Args:
        templates_dir: Optional custom templates directory

    Returns:
        PromptTemplate instance
    """
    global _default_template_manager

    if _default_template_manager is None or templates_dir is not None:
        _default_template_manager = PromptTemplate(templates_dir)

    return _default_template_manager
