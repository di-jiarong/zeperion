"""Reproducible end-to-end demo for ``examples/auth-system``.

Runs the full multi-agent graph against deterministic ``FakeAgent``
outputs and dumps the resulting on-disk artefacts into
``examples/auth-system/transcript/``. The point is not to show the
LLMs producing brilliant code (they're mocked here), but to give a
new user a concrete answer to the question:

    "What does zeperion *actually* leave on disk after a run?"

The committed contents of ``transcript/`` are the literal output of
``python examples/auth-system/run_demo.py``. To regenerate, delete
the directory and re-run the script. There are no API keys, no
network calls, and no hidden state.

Why FakeAgent rather than a live AnthropicAgent run:

* The CI sandbox has no ``ANTHROPIC_API_KEY`` and we don't want to
  burn user money to ship a demo.
* AnthropicAgent has no tool surface anyway (see the README yellow
  warning) — a live run would still produce only text artefacts, so
  the *shape* of the output is identical to what we mock here.
* Determinism: a committed transcript stays diff-stable across
  re-runs, which is what makes it useful as documentation.

If you want to see what a *real* live run looks like, set
``ANTHROPIC_API_KEY`` in your environment, switch
``developer_agent_type`` to ``claude_code`` (so files actually get
written), and invoke ``zeperion run --mode multi_agent`` from a
real project directory.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Iterable

from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    GlobalStatus,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_DIR = EXAMPLE_DIR / "transcript"


# ---------------------------------------------------------------------------
# FakeAgent — same contract as AnthropicAgent / ClaudeCodeAgent but
# returns a pre-seeded queue of AgentOutput values without any I/O.
# ---------------------------------------------------------------------------


class FakeAgent:
    """Yields a pre-seeded sequence of :class:`AgentOutput` values.

    ``role`` and ``model`` are accepted for signature compatibility
    with ``BaseAgent`` subclasses (the graph factory passes them in)
    but otherwise ignored.
    """

    outputs: list[AgentOutput] = []

    def __init__(self, role, model):
        self.role = role
        self.model = model

    async def invoke(self, prompt, session_id=None) -> AgentOutput:
        if not FakeAgent.outputs:
            raise RuntimeError(
                "FakeAgent ran out of pre-seeded outputs — script is buggy"
            )
        return FakeAgent.outputs.pop(0)


# ---------------------------------------------------------------------------
# Realistic-looking outputs for a 2-round auth-system implementation:
#
# Round 1: Planner cuts the work into 3 P-tasks, Developer reports it
# implemented the first slice, Tester finds two test failures.
#
# Round 2: Planner trims scope, Developer fixes the bugs, Tester
# confirms PASS and Planner/Tester sign off with GLOBAL_STATUS=DONE.
#
# These strings are deliberately written to match the real prompt
# templates' output contract (TASK_ID / PR_TITLE / GLOBAL_STATUS /
# PLAN / RISKS / HANDOFF / LESSONS for Planner; CHANGES / VERIFY_HINTS
# / LESSONS for Developer; TEST_STATUS / TEST_CASES / BUGS / LESSONS
# for Tester). Any drift between these strings and the templates would
# make the transcript misleading.
# ---------------------------------------------------------------------------


_PLANNER_R1 = """\
TASK_ID: auth_v1_bootstrap
PR_TITLE: feat: bootstrap user-auth project (registration + bcrypt + JWT)
GLOBAL_STATUS: CONTINUE

PLAN:
- [P1] 搭建 FastAPI 项目骨架 + SQLAlchemy User 模型，验收：``alembic upgrade head`` 生效。
- [P2] 实现密码 bcrypt 加密 + JWT 工具函数，验收：``pytest tests/test_security.py`` 通过。
- [P3] 实现 ``POST /register`` API，验收：注册返回 user_id 且 DB 中密码字段非明文。

RISKS:
- bcrypt cost factor 过大会拖慢 register API。
- JWT secret 必须从环境变量读，不能硬编码到代码里。

HANDOFF_TO_DEVELOPER:
- 本轮完成 P1+P2，``POST /register`` 留到下一轮再做。
- 输出：``app/__init__.py``、``app/models.py``、``app/security.py``、``tests/test_security.py``。

LESSONS:
- 第一轮先打地基，不要急着做 API；登录 / 限流是更难的部分。
"""


_DEVELOPER_R1 = """\
GLOBAL_STATUS: CONTINUE

CHANGES:
- 新增 app/__init__.py、app/models.py（User 表定义）、app/security.py（bcrypt + JWT）。
- 新增 tests/test_security.py，覆盖 bcrypt 哈希校验和 JWT 编解码。

VERIFY_HINTS:
- pytest tests/test_security.py 应该全绿。
- alembic revision --autogenerate -m "init users" 不应报错。

BLOCKERS: NONE

LESSONS:
- 用 passlib[bcrypt] 而不是裸 bcrypt：自带版本兼容层，省去 deprecation 噪声。
"""


_TESTER_R1 = """\
TEST_STATUS: FAIL
GLOBAL_STATUS: CONTINUE

TEST_CASES:
- bcrypt round-trip：PASS
- JWT 编解码：PASS
- User 表迁移：FAIL（缺少 created_at 默认值）
- security.verify_password 错误密码场景：FAIL（返回 None，应该返回 False）

BUGS:
- app/models.py: User.created_at 没设 server_default=func.now()，alembic 升级时报错。
- app/security.py: verify_password 的失败分支返回 None 而不是 False，调用方会被 truthy 判断坑。

FIX_REQUEST:
- 修复 User.created_at 默认值。
- 让 verify_password 在所有失败分支都明确 ``return False``。

LESSONS:
- SQLAlchemy 的 created_at 必须用 server_default 而不是 Python-side default，否则 alembic 不识别。
"""


_DEVELOPER_R1_FIX = """\
GLOBAL_STATUS: CONTINUE

CHANGES:
- app/models.py: User.created_at 改为 server_default=func.now()。
- app/security.py: verify_password 显式 return False，加单元测试覆盖错误密码分支。

VERIFY_HINTS:
- pytest tests/test_security.py 现在应该全部 PASS。
- alembic upgrade head 应可执行。

BLOCKERS: NONE

LESSONS:
- 失败分支别返回 None；调用方 truthy 检查会把 None 当成"没失败"，是经典坑。
"""


_TESTER_R1_FIX = """\
TEST_STATUS: PASS
GLOBAL_STATUS: CONTINUE

TEST_CASES:
- bcrypt round-trip：PASS
- JWT 编解码：PASS
- User 表迁移：PASS
- verify_password 错误密码：PASS

BUGS: NONE

LESSONS:
- 修完 created_at 后，alembic autogenerate 不再产出 spurious diff。
"""


_PLANNER_R2 = """\
TASK_ID: auth_v1_register_endpoint
PR_TITLE: feat: add POST /register with rate-limited login skeleton
GLOBAL_STATUS: CONTINUE

PLAN:
- [P1] 实现 POST /register（接收 email+password，写库，返回 user_id）。
- [P2] 加 fastapi-limiter 中间件 skeleton（限流 5/min，但先 stub Redis）。

RISKS:
- 真接 Redis 在本地环境不一定可用，先做接口对齐。

HANDOFF_TO_DEVELOPER:
- 本轮完成 P1+P2 的 stub，POST /login 留到下一轮。

LESSONS:
- 限流先做接口和测试 stub，不要在第一轮接真实 Redis。
"""


_DEVELOPER_R2 = """\
GLOBAL_STATUS: CONTINUE

CHANGES:
- 新增 app/routers/auth.py 实现 POST /register。
- 新增 app/middleware/ratelimit.py，提供 RateLimiter 接口（默认内存实现，预留 Redis 后端）。
- tests/test_register.py 覆盖正常注册和重复 email 场景。

VERIFY_HINTS:
- pytest tests/test_register.py 全绿。
- curl POST /register 应返回 {"user_id": <int>}。

BLOCKERS: NONE

LESSONS:
- 内存 RateLimiter 适合单元测试，但生产必须用 Redis。
"""


_TESTER_R2 = """\
TEST_STATUS: PASS
GLOBAL_STATUS: DONE

TEST_CASES:
- POST /register 正常路径：PASS
- POST /register 重复 email：PASS（返回 409）
- 密码字段在 DB 中已 bcrypt 哈希：PASS
- RateLimiter 内存实现：PASS（5 次/分钟阈值生效）

BUGS: NONE

LESSONS:
- 第一轮的 P3 被拆到第二轮独立做，节奏更顺；下次直接这样规划。
"""


SCRIPTED_OUTPUTS: list[AgentOutput] = []
_REVIEWER_PASS = "REVIEW_STATUS: PASS\nGLOBAL_STATUS: CONTINUE\nLESSONS:\n- Review passed\n"


def _seed(role_outputs: Iterable[tuple[str, str, dict]]) -> None:
    """Build :class:`AgentOutput` queue from ``(role, raw, kwargs)`` triples.

    ``kwargs`` is forwarded to :class:`AgentOutput` and is how each
    scripted entry advertises ``test_status`` / ``global_status`` —
    we do not re-parse the strings, so a typo in the demo template
    doesn't accidentally break the workflow path under test.
    """
    for _, raw, kwargs in role_outputs:
        SCRIPTED_OUTPUTS.append(AgentOutput(raw_output=raw, **kwargs))


_seed([
    # Round 1
    ("planner",   _PLANNER_R1,        {"task_id": "auth_v1_bootstrap",
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["第一轮先打地基"]}),
    ("developer", _DEVELOPER_R1,      {"global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["passlib[bcrypt] 自带兼容层"]}),
    ("reviewer",  _REVIEWER_PASS,     {"review_status": ReviewStatus.PASS,
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["Review passed"]}),
    ("tester",    _TESTER_R1,         {"test_status": TestStatus.FAIL,
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["created_at 用 server_default"]}),
    # Round 1 fix attempt
    ("developer", _DEVELOPER_R1_FIX,  {"global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["失败分支不要返回 None"]}),
    ("reviewer",  _REVIEWER_PASS,     {"review_status": ReviewStatus.PASS,
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["Review passed"]}),
    ("tester",    _TESTER_R1_FIX,     {"test_status": TestStatus.PASS,
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["修完 created_at 后 alembic 不再 spurious diff"]}),
    # Round 2
    ("planner",   _PLANNER_R2,        {"task_id": "auth_v1_register_endpoint",
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["限流先做 stub"]}),
    ("developer", _DEVELOPER_R2,      {"global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["内存 RateLimiter 仅用于测试"]}),
    ("reviewer",  _REVIEWER_PASS,     {"review_status": ReviewStatus.PASS,
                                        "global_status": GlobalStatus.CONTINUE,
                                        "lessons": ["Review passed"]}),
    ("tester",    _TESTER_R2,         {"test_status": TestStatus.PASS,
                                        "global_status": GlobalStatus.DONE,
                                        "lessons": ["先拆任务再实现，节奏更顺"]}),
])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _reset_transcript_dir() -> Path:
    """Wipe the transcript dir so each run starts from a clean state."""
    if TRANSCRIPT_DIR.exists():
        shutil.rmtree(TRANSCRIPT_DIR)
    TRANSCRIPT_DIR.mkdir(parents=True)
    return TRANSCRIPT_DIR


def _build_config(state_dir: Path) -> WorkflowConfig:
    """Construct a config that points everything at the transcript dir.

    We deliberately set ``github_repo=None`` and ``github_token=None``
    so the Tester ``GLOBAL_STATUS=DONE`` does NOT auto-enter the PR
    pipeline — the demo is pure multi-agent and should not try to
    talk to GitHub from inside this script.
    """
    requirement = EXAMPLE_DIR / "requirement.txt"
    return WorkflowConfig(
        requirement_file=str(requirement),
        state_dir=str(state_dir),
        prompts_dir=str(REPO_ROOT / "zeperion" / "prompts" / "templates"),
        project_dir=str(EXAMPLE_DIR),
        max_rounds=3,
        max_fix_attempts=2,
        github_repo=None,
        github_token=None,
    )


async def _run() -> dict:
    state_dir = _reset_transcript_dir()
    FakeAgent.outputs = list(SCRIPTED_OUTPUTS)

    config = _build_config(state_dir)
    graph = create_multi_agent_graph(
        config,
        agent_class=FakeAgent,
        thread_id="demo",
    )
    initial_state = create_initial_state(config)

    final = None
    async for event in graph.astream(
        initial_state, {"configurable": {"thread_id": "demo"}}
    ):
        for node_name, node_state in event.items():
            final = node_state
            print(f"\u2192 {node_name}: phase={node_state.get('phase')} "
                  f"round={node_state.get('round')} "
                  f"test={node_state.get('test_status')} "
                  f"global={node_state.get('global_status')}")
    return final or {}


def main() -> int:
    final = asyncio.run(_run())
    print("\n--- final state -----------------------------------")
    for key in (
        "phase",
        "round",
        "fix_attempt",
        "task_id",
        "pr_title",
        "test_status",
        "global_status",
    ):
        print(f"  {key}: {final.get(key)}")

    print(f"\nWrote transcript to: {TRANSCRIPT_DIR.relative_to(REPO_ROOT)}")
    leaves: list[str] = []
    for path in sorted(TRANSCRIPT_DIR.rglob("*")):
        if path.is_file():
            leaves.append(str(path.relative_to(TRANSCRIPT_DIR)))
    for leaf in leaves:
        print(f"  - {leaf}")
    if not leaves:
        print("  (empty — something went wrong)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
