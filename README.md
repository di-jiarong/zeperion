# ZEPERION

**多智能体开发与 PR 交付管线框架**

ZEPERION 是一个基于 LangGraph 的多智能体协作框架，用于自动化软件开发工作流。它通过 Planner、Developer、Reviewer、Tester 四个智能体的协作，实现从需求到代码交付的完整闭环，并支持自动化 GitHub PR 创建、审查和合并。

> **⚠️ 重要：`anthropic` agent 不会修改你的项目文件**
>
> `AnthropicAgent` 只发起一次 `messages.create` 调用并解析返回文本，
> **不带任何工具能力**（没有 file IO，没有 shell）。如果把 Developer
> 配成 `anthropic`，工作流会产出文本到
> `.zeperion/state/threads/<thread_id>/*_output.txt`，但**不会写入任何源代码**。
>
> 默认配置已经把 Developer / Reviewer / Tester 放在 `pi` 后端上。`pi` 通过
> Pi Coding Agent 的 JSONL RPC 模式调起本地 Pi CLI；`claude_code` 通过
> `claude --print` CLI 调起 Claude Code。文件读写由对应 CLI 自身完成。
> 只有显式改回 `anthropic` 时才是“只产出建议、不改文件”。

---

## 目录

- [30 秒上手](#30-秒上手)
- [安装](#安装)
- [命令速查](#命令速查)
- [Multi-Agent 模式（本地开发）](#multi-agent-模式本地开发)
- [PR Pipeline 模式（GitHub 交付）](#pr-pipeline-模式github-交付)
- [后台运行与日志](#后台运行与日志)
- [Web UI 监控](#web-ui-监控)
- [配置](#配置)
- [使用自定义 Claude 代理 / 中转](#使用自定义-claude-代理--中转)
- [Agent 后端说明](#agent-后端说明)
- [状态管理与续跑](#状态管理与续跑)
- [更新与卸载](#更新与卸载)
- [工作原理](#工作原理)
- [开发与测试](#开发与测试)
- [故障排查](#故障排查)

---

## 30 秒上手

```bash
# 1. 把 zeperion 装成全局命令（详见“安装”）
./scripts/install.sh

# 2. 进入你的目标项目，初始化
cd ~/your-project
zeperion init

# 3. 写需求
$EDITOR requirement.txt

# 4. 跑起来（默认 multi_agent 模式，thread_id 取当前 git 分支名）
zeperion run -t feature-x

# 5. 看进度 / 看状态
zeperion logs -t feature-x --follow
zeperion status -t feature-x
```

跑完后 Developer/Reviewer/Tester 的真实改动就落在你项目的工作区里，用
`git status` / `git diff` 检查后再决定是否提交。

---

## 安装

### 方式 A：全局命令（推荐，像 `claude` 一样随处可用）

```bash
# 在 zeperion 仓库根目录执行
./scripts/install.sh
```

脚本用 [`pipx`](https://pipx.pypa.io/) 把 `zeperion` 安装到独立环境，并在你的
全局 PATH 上放一个 shim——它的依赖（langgraph、typer……）不会污染你当前的
conda/venv。脚本没有 `pipx` 时会自动尝试 bootstrap。

```bash
# 常用参数
./scripts/install.sh --extras "anthropic,web"   # 额外装 Web UI
./scripts/install.sh --no-editable --force       # 干净快照重装
```

- `--extras LIST`：逗号分隔的可选 extra，默认 `anthropic`（Planner 默认走
  anthropic 后端，需要它）。可选：`web`（浏览器 UI）、`github`（PR 管线）、
  `tracing`（OpenTelemetry）、`dev`（测试 + lint）。
- `--no-editable`：装快照而非可编辑安装（默认可编辑，仓库 `git pull` 后即时生效）。
- `--force`：覆盖已有的 pipx 安装。

### 方式 B：pip 直接装（装进当前激活的环境）

```bash
git clone https://github.com/yourusername/zeperion.git
cd zeperion
pip install -e ".[anthropic]"        # 基础 + Planner 的 anthropic 后端
pip install -e ".[dev]"              # 想跑测试/lint 用这个
pip install -e ".[anthropic,web]"    # 想用 Web UI 加 web
```

可选 extra 一览：

| extra | 作用 |
|-------|------|
| `anthropic` | `AnthropicAgent`（Planner 默认后端）所需的 `anthropic` SDK |
| `github` | PR Pipeline 用到的 `PyGithub` |
| `web` | `zeperion serve` 的 FastAPI + uvicorn |
| `tracing` | OpenTelemetry 导出（默认是 no-op span，可不装） |
| `dev` | 上述全部 + pytest / black / ruff / mypy |

> `claude_code` 后端**没有** Python 侧依赖，它直接调用你本地安装的 `claude`
> 二进制，按 [Claude Code 文档](https://docs.anthropic.com/claude/docs/claude-code)
> 单独安装即可。`pi` 后端同理，需要本地有 `pi` CLI。

验证安装：

```bash
zeperion version
zeperion --help
```

---

## 命令速查

| 命令 | 作用 | 常用参数 |
|------|------|----------|
| `zeperion init [dir]` | 初始化项目（生成 `.zeperion/config.yaml`、`requirement.txt`） | `-b/--backend pi\|claude_code\|anthropic`、`-f/--force` |
| `zeperion doctor` | 检查本地环境是否可运行 | `-c/--config`、`--probe/--no-probe` |
| `zeperion verify` | 单独运行 / 探测 Tester 验收命令 | `-c/--config`、`--command`、`--timeout`、`--detect`、`--write-config`、`--tail` |
| `zeperion run` | 运行工作流 | `-m/--mode`、`-t/--thread-id`、`-r/--resume`、`-d/--detach`、`--log-format`、`--no-pr-pipeline`、`--yes`、`--allow-dirty` |
| `zeperion ship` | 一条龙：multi_agent → PR pipeline | `-t/--thread-id`、`--yes`、`--allow-dirty` |
| `zeperion status` | 查看单个 thread 状态 | `-t/--thread-id`、`--watch`、`--interval` |
| `zeperion list` | 列出所有 thread | `--wide` |
| `zeperion logs` | 查看 / 跟随事件流 | `-t`、`-f/--follow`、`-n/--tail`、`--poll-interval` |
| `zeperion stop` | 停止后台运行 | `-t`、`--force`、`--timeout` |
| `zeperion serve` | 启动 Web UI（需 `[web]`） | `--host`、`-p/--port`、`--poll-interval` |
| `zeperion update` | 原地自更新 | `--extras`、`--no-pull` |
| `zeperion version` | 打印版本 | — |

> **关于 `-t/--thread-id`**：不传时默认取**当前 git 分支名**（做过文件系统安全
> 字符过滤），不在 git 仓库或 detached HEAD 时回退到 `main`。同一项目里不同分支
> 自动隔离 checkpoint，互不覆盖。

---

## Multi-Agent 模式（本地开发）

### 1. 初始化项目

```bash
cd ~/your-project
zeperion init                 # 默认写代码后端为 pi
zeperion init -b claude_code  # 改用 Claude Code CLI
zeperion init -b anthropic    # 只产出建议、不改文件
```

生成内容：

```
your-project/
  requirement.txt             # 需求描述（你来填）
  .zeperion/
    config.yaml               # 配置（机器生成）
    state/                    # 运行时状态、checkpoint、日志、产物
```

> `.zeperion/` 整个目录会被自动加进项目的 `.gitignore`——里面是机器生成的
> 配置和运行时状态，不应随源码提交。

### 2. 编写需求

编辑 `requirement.txt`：

```
实现一个用户认证系统，包括：
1. 用户注册（邮箱 + 密码）
2. 用户登录（JWT token）
3. 密码加密存储（bcrypt）
4. 登录失败限流（5次/分钟）
```

### 3. 运行工作流

```bash
# 前台运行（thread_id 默认取 git 分支名）
zeperion run

# 指定 thread_id，方便管理多个任务
zeperion run -t feature-auth

# 后台运行，立即返回 shell
zeperion run -t feature-auth --detach

# 查看状态 / 实时跟随日志
zeperion status -t feature-auth
zeperion logs -t feature-auth --follow

# 中断后从 checkpoint 续跑
zeperion run -t feature-auth --resume
```

四个角色的协作循环：

```
Planner ──▶ Developer ──▶ Reviewer ──▶ Tester ──▶ 循环或完成
   │分析需求    │实现代码      │审查质量      │验收测试
   └────────────── 失败时回到上一环修复 ──────────────┘
```

Reviewer 默认开启。只想保留旧的 Developer → Tester 流程，在配置里设
`enable_reviewer: false`。

---

## PR Pipeline 模式（GitHub 交付）

### 前置条件

1. 项目是 Git 仓库；
2. 安装并登录 GitHub CLI；
3. 设置 `GITHUB_TOKEN`（或在配置里给 `github_repo`）。

```bash
# macOS
brew install gh
gh auth login
```

### 用法

```bash
# 1. 先跑 multi_agent 完成开发（跳过自动 PR 阶段）
zeperion run -m multi_agent -t feature-auth --no-pr-pipeline

# 2. 再跑 PR pipeline 创建 PR（建议用 <thread>-pr 后缀）
zeperion run -m pr_pipeline -t feature-auth-pr

# 3. 查看 PR 状态
zeperion status -t feature-auth-pr

# 4. Codex 还没审完时，稍后恢复检查
zeperion run -m pr_pipeline -t feature-auth-pr --resume
```

`-pr` 后缀有特殊待遇：PR 阶段会自动从同名去掉 `-pr` 的兄弟 thread 里回收
Planner 产出的 `PR_TITLE` / `TASK_ID`，作为 commit subject 和 PR 标题。

### 一条龙：`zeperion ship`

把上面两步合成一条命令（跑 multi_agent，DONE 后自动接 PR pipeline；没到
DONE 会短路、不会把半成品推上去）：

```bash
zeperion ship -t feature-auth
```

### PR 流程

```
Validate Git ──▶ Commit ──▶ Push ──▶ Create/Update PR ──▶ Check Codex Review
                                                                 │
              ┌──────────────────────────────────────────────────┤
              ▼                    ▼                     ▼
        👍≥阈值 → Auto-merge   评论>阈值 → 需修复     未审/等待 → 暂停
```

---

## 后台运行与日志

```bash
# 后台起一个任务
zeperion run -t feature-x --detach
# ✓ Detached run started: pid=... thread=feature-x
#   日志写到 .zeperion/state/runs/feature-x/run.log

# 实时跟随事件流（跑完会自动打印 "✓ Workflow finished" 并退出）
zeperion logs -t feature-x --follow

# 只看最近 N 条
zeperion logs -t feature-x --tail 20

# 停止后台任务（先 SIGTERM，超时再 SIGKILL）
zeperion stop -t feature-x
zeperion stop -t feature-x --force      # 直接 SIGKILL
```

`logs --follow` 跟踪的是 `events.jsonl`（结构化事件）。工作流结束时会写入一条
`workflow_finished` 终止事件，`--follow` 读到后会打印明确横幅并自动退出，不会
空转。

---

## Web UI 监控

需要 `[web]` extra：

```bash
pip install -e ".[web]"      # 或 ./scripts/install.sh --extras web

zeperion serve               # 默认 http://127.0.0.1:8765/threads
zeperion serve --port 9000
zeperion serve --host 0.0.0.0   # 暴露到局域网（无鉴权，仅限可信网络）
```

Web UI 提供 thread 列表 + 详情下钻 + 基于 SSE 的实时事件流。它读的是同一份
`.zeperion/state/`，所以**可以在任务正在跑（包括 `--detach` 后台任务）时打开**，
实时看进度，不影响运行。

---

## 配置

编辑 `.zeperion/config.yaml`。配置是一份**扁平**的 YAML（直接对应
`WorkflowConfig` 的 Pydantic 字段），**不要写嵌套的 `anthropic:` / `github:` /
`cli:` block**——加载器只把顶层 key 透传给 `WorkflowConfig(**)`。

```yaml
# 入口需求（相对路径以 .zeperion/ 为基准解析）
requirement_file: ../requirement.txt
project_dir: ..
state_dir: state

# 四个 role 各自的后端：anthropic（API）、pi（Pi CLI）、claude_code（Claude Code CLI）
planner_agent_type: anthropic
developer_agent_type: pi            # 默认会真正改文件
reviewer_agent_type: pi
tester_agent_type: pi

# 模型
planner_model: claude-opus-4-7
developer_model: claude-sonnet-4-6
reviewer_model: claude-sonnet-4-6
tester_model: claude-opus-4-7

# 工作流
max_rounds: 10                      # 默认 10，防止解析失败时烧 token
max_fix_attempts: 3
enable_reviewer: true
max_total_tokens: 0                 # >0 时作为累计 token 预算护栏，到顶即 BLOCKED

# Tester 真实验收命令（init 会按项目类型自动探测常见命令）
tester_verify_commands:
  - pytest -q
tester_verify_timeout_seconds: 300  # 每条验收命令的超时（秒）

# Pi RPC 调谐（仅 *_agent_type=pi 时生效）
pi_cli_tool: pi
pi_cli_timeout: 600                 # 单次 agent 调用的超时（秒）
pi_rpc_no_session: false
pi_rpc_progress_interval_seconds: 30
pi_rpc_auto_respond_ui_requests: true

# Claude Code CLI 调谐（仅 *_agent_type=claude_code 时生效）
claude_cli_tool: claude
claude_cli_timeout: 600             # 单次 agent 调用的超时（秒）；代理慢可调大到 1800
claude_cli_progress_interval_seconds: 30
claude_cli_use_worktree: false
claude_cli_keep_worktree: true

# GitHub PR Pipeline（顶层字段，不是嵌套 block）
github_repo: owner/repo-name        # 可选，未填则从 git remote 自动识别
pr_target_branch: main
pr_auto_merge: true
codex_poll_minutes: 30
max_pr_fixer_rounds: 5

# 凭据一律走环境变量，不要写进配置：
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export GITHUB_TOKEN=ghp_...
```

> **超时怎么调**：超时是**每个角色单次调用**的上限，字段是
> `claude_cli_timeout` / `pi_cli_timeout`（单位秒，默认 600）。用代理或任务复杂
> 时容易在 600s 触顶，调大到 `1800` 即可。**没有 `cli.timeout` 这种嵌套写法**。

---

## 使用自定义 Claude 代理 / 中转

如果你用的是自建的 Claude 中转（LiteLLM / 网关等），`claude_code` 后端会读
Claude CLI 那套环境变量。流程：

1. 配置里把要改文件的角色设成 `claude_code`，模型填你代理认得的名字：

```yaml
planner_agent_type: claude_code
developer_agent_type: claude_code
reviewer_agent_type: claude_code
tester_agent_type: claude_code

planner_model: your-proxy-model-name
developer_model: your-proxy-model-name
reviewer_model: your-proxy-model-name
tester_model: your-proxy-model-name

claude_cli_timeout: 1800            # 代理通常更慢，给足时间
```

2. 跑之前在 shell 里导出代理地址和 token（和你 `~/.claude/settings.json` 里
   `env` 的那几项一致）：

```bash
export ANTHROPIC_BASE_URL="http://your-proxy-host:4000"
export ANTHROPIC_AUTH_TOKEN="sk-xxxx"     # 部分网关也接受 ANTHROPIC_API_KEY
```

3. 正常运行：

```bash
zeperion run -t feature-x --detach
zeperion logs -t feature-x --follow
```

---

## Agent 后端说明

**PiAgent（默认写代码后端）**
- 通过 subprocess 调起 `pi --mode rpc`，复用 ZEPERION 的结构化输出解析。
- 能读写文件、跑命令，适合 Developer / Reviewer / Tester。
- 需要本地安装 `pi` CLI。

**ClaudeCodeAgent**
- 通过 subprocess 调起 `claude --print` CLI，CLI 自身可读写文件。
- 适合在 Claude Code 生态内运行，也支持自建代理（见上一节）。
- 需要本地安装 `claude` CLI（无 Python 依赖）。

**AnthropicAgent**
- 直接调 Anthropic Messages API，**不带工具能力、只产出文本**（见顶部黄牌）。
- 适合 Planner，或“只要建议、不改文件”的 dry-run。
- 需要 `pip install zeperion[anthropic]` + `ANTHROPIC_API_KEY`。

接其他后端（OpenAI / Gemini / Ollama 等）：继承 `BaseAgent` 实现 `invoke()`，
复用 `self.parse_output(raw)` 即可得到一致的字段解析。详见
[`AGENT_GUIDE.md`](AGENT_GUIDE.md)。

---

## 状态管理与续跑

ZEPERION 用 LangGraph 的 SQLite checkpoint 持久化状态，每个 `thread_id` 一份历史。

```bash
zeperion list                       # 列出所有 thread 及当前阶段
zeperion list --wide                # 更宽的输出
zeperion status -t feature-auth     # 单个 thread 详情（含 PR Pipeline）
zeperion status -t feature-auth --watch --interval 3   # 每 3 秒刷新
zeperion run -t feature-auth --resume                  # 从 checkpoint 续跑
```

**续跑的粒度是“节点级”**：LangGraph 在每个 agent 节点**执行完之后**才写
checkpoint。

- 在两个 agent 之间中断 → 续跑从下一个节点开始，不重跑已完成的；
- 某个 agent 跑到一半被中断 / kill → 那个节点没落盘，续跑会**重跑这个节点**。

**命名建议**：用描述性短名（`auth-system` / `bug-fix-123`）；不要在不同项目复用
同一 `thread_id`；不要让两个并发 `zeperion run` 共享同一个 `thread_id`（会让
`events.jsonl` 和 checkpoint 被两个进程同时写，最难调试）。

想直接读 checkpoint，用 LangGraph 官方 API（不要 `pickle.loads` 走内部表）：

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async with AsyncSqliteSaver.from_conn_string(".zeperion/state/checkpoints.db") as saver:
    async for snapshot in saver.alist(None):
        print(snapshot.config["configurable"]["thread_id"],
              snapshot.checkpoint["channel_values"])
```

---

## 更新与卸载

```bash
# 原地自更新：可编辑安装会 git pull + 重装到当前环境（新依赖也会装上）
zeperion update

# 顺带补装 extra
zeperion update --extras "anthropic,web"

# 只重装、不 git pull
zeperion update --no-pull
```

快照安装（从 wheel/PyPI 装的，没有源码 checkout）无法自更新，`update` 会提示你
正确的 `pipx upgrade zeperion` 或 `pip install -U zeperion`。

卸载（pipx 安装时）：

```bash
pipx uninstall zeperion
```

---

## 工作原理

### Multi-Agent 状态机

```
PLANNING ──▶ DEVELOPMENT ──▶ REVIEWING ──▶ TESTING
    ▲             │              │             │
    └──── (fix) ◀──── (review fail) ◀──── (test fail)
                                              │
                                              ▼
                                          COMPLETED
```

| Role | 读取 | 写入状态 | 可置 DONE |
|------|------|----------|-----------|
| Planner | 需求 + 上一版计划 + 最新测试报告 + lessons | `task_id`、`global_status`、`phase=DEVELOPMENT` | 是 |
| Developer | 需求 + 当前计划 + reviewer/tester 报告 | `phase=REVIEWING/TESTING`、`lessons` | 否（被解析器强制收敛为 CONTINUE） |
| Reviewer | 需求 + 计划 + developer 产出 | `review_status`、`global_status`、`last_error` | 是 |
| Tester | 需求 + 计划 + developer 产出 | `test_status`、`global_status`、`last_error` | 是 |

### 输出契约（由 `SectionParser` 解析）

- **Planner**：`TASK_ID:`、`GLOBAL_STATUS: CONTINUE|DONE|BLOCKED`、`PLAN:`、`RISKS:`、`HANDOFF_TO_DEVELOPER:`、`LESSONS:`
- **Developer**：`GLOBAL_STATUS: CONTINUE|BLOCKED`、`CHANGES:`、`VERIFY_HINTS:`、`BLOCKERS:`、`LESSONS:`
- **Reviewer**：`REVIEW_STATUS: PASS|FAIL|BLOCKED`、`GLOBAL_STATUS: ...`、`FINDINGS:`、`FIX_REQUEST:`、`LESSONS:`
- **Tester**：`TEST_STATUS: PASS|FAIL|ERROR`、`GLOBAL_STATUS: ...`、`TEST_CASES:`、`BUGS:`、`FIX_REQUEST:`、`LESSONS:`

解析大小写不敏感，并容忍前后散文、Markdown 标题（`## GLOBAL_STATUS:`）和粗体
标签（`**GLOBAL_STATUS:**`）。

### 状态目录布局

```
.zeperion/state/
  checkpoints.db                      # LangGraph SQLite（多 thread）
  lessons_learned.txt                 # 跨运行的经验（共享）
  threads/<thread_id>/
    workflow_state.json               # 多智能体状态快照（供人看）
    pipeline_state.json               # PR 管线状态快照
    planner_output.txt / ...          # 各角色最新原始输出
  runs/<thread_id>/
    round_001_planner.txt / ...       # 每轮/每次修复的产物
    events.jsonl                      # 结构化逐步事件日志
    run.log                           # --detach 时的 stdout/stderr
    run.pid                           # --detach 进程的 pid
```

### Python API

```python
import asyncio
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from zeperion.graphs.multi_agent import create_multi_agent_graph
from zeperion.models import WorkflowConfig, create_initial_state


async def main():
    config = WorkflowConfig(
        requirement_file="./requirement.txt",
        planner_model="claude-opus-4-7",
        developer_model="claude-sonnet-4-6",
        tester_model="claude-opus-4-7",
        max_rounds=10,
        max_fix_attempts=3,
    )
    async with AsyncSqliteSaver.from_conn_string(".zeperion/state/checkpoints.db") as saver:
        graph = create_multi_agent_graph(config, checkpointer=saver, thread_id="my-run-001")
        await graph.ainvoke(create_initial_state(config),
                            {"configurable": {"thread_id": "my-run-001"}})


asyncio.run(main())
```

---

## 开发与测试

```bash
pip install -e ".[dev]"     # 装开发依赖（含 pytest / ruff / black / mypy）

pytest                      # 跑全部测试
pytest tests/test_agents.py
pytest --cov=zeperion       # 带覆盖率

ruff check zeperion         # lint（CI 中 import 排序 I 规则为阻塞）
black zeperion tests        # 格式化
mypy zeperion               # 类型检查（CI 中非阻塞）
```

CI（`.github/workflows/ci.yml`）会跑 `pytest`、`ruff check`、`python -m compileall`。

---

## 故障排查

### Agent 调用超时

调大对应后端的单次超时（**扁平字段，单位秒**）：

```yaml
claude_cli_timeout: 1800     # claude_code 后端
pi_cli_timeout: 1800         # pi 后端
```

### 工作流被 BLOCKED

先看最后一个 agent 的产物，确认是真被卡住还是输出格式问题：

```bash
zeperion status -t <thread_id>
cat .zeperion/state/runs/<thread_id>/round_*_<role>.txt
```

### 启用调试日志

```bash
export ZEPERION_LOG_LEVEL=DEBUG
export ZEPERION_LOG_FORMAT=json     # 或在 run 时加 --log-format json
zeperion run -t <thread_id>
```

### 状态损坏 / 想从头来

```bash
rm -rf .zeperion/state/
zeperion run -t <thread_id>
```

---

## 与 Bash 旧版本的区别

历史上 ZEPERION 曾有一份 bash 实现，现已迁出主线，仅保留在 `legacy/bash-harness`
分支供查阅。新功能不再向 bash 版同步。

| 特性 | Bash 旧版（legacy 分支） | LangGraph Python 版（main） |
|------|-----------------------|-----------------------------|
| 类型安全 | 无 | 有，基于 Pydantic |
| 输出解析 | awk/grep 严格匹配 | `SectionParser` 宽松匹配 |
| 状态持久化 | 手写 JSON 文件 | LangGraph SQLite 检查点 |
| 多任务并行 | 共享文件易冲突 | 按 `thread_id` 分目录隔离 |
| 可测试性 | 无 | `pytest` 覆盖核心路径 |
| 错误恢复 | 人工干预 | `zeperion run --resume` 续跑 |

```bash
git show legacy/bash-harness:.ai_longrun_harness/run_multi_agent_loop.sh
```

---

## 许可证

MIT License - 详见 [LICENSE](LICENSE)。

## 相关项目

- [LangGraph](https://github.com/langchain-ai/langgraph) - 状态机框架
- [LangChain](https://github.com/langchain-ai/langchain) - LLM 应用框架
- [Claude](https://www.anthropic.com/claude) - AI 助手
