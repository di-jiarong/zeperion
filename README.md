# ZEPERION

**多智能体开发与 PR 交付管线框架**

ZEPERION 是一个基于 LangGraph 的多智能体协作框架，用于自动化软件开发工作流。它通过 Planner、Developer、Tester 三个智能体的协作，实现从需求到代码交付的完整闭环，并支持自动化 GitHub PR 创建、审查和合并。

> **⚠️ 重要：默认 `anthropic` agent 不会修改你的项目文件**
>
> `AnthropicAgent` 只发起一次 `messages.create` 调用并解析返回文本，
> **不带任何工具能力**（没有 file IO，没有 shell）。在 `multi_agent`
> 模式下使用默认配置时，Planner/Developer/Tester 三个 agent 只会产出
> 文本到 `.zeperion/state/threads/<thread_id>/*_output.txt`，**不会写入
> 任何源代码**。
>
> 如果想让 agent 真正改文件，必须把对应 role（通常是 Developer）切到
> `claude_code` agent 类型 —— 它通过 `claude --print` CLI 调起 Claude Code，
> 由 CLI 自身完成文件读写：
>
> ```yaml
> developer_agent_type: claude_code
> ```
>
> 这件事 README 之前没有讲清楚，请先看完这一段再决定怎么用。

## 特性

- **类型安全**：基于 Pydantic 的状态模型，编译时类型检查
- **容错解析**：宽松的 LLM 输出解析，支持大小写不敏感、空格容忍
- **检查点恢复**：LangGraph 自动持久化状态，支持中断恢复
- **并发安全**：StateGraph 原子状态更新，无文件轮询
- **可测试性**：模块化设计，易于单元测试和 mock
- **可扩展性**：插件化 Agent 架构，支持自定义智能体
- **PR 自动化**：自动创建 PR、等待 Codex 审查、启用 auto-merge

## 工作模式

ZEPERION 支持两种工作模式：

### 1. Multi-Agent 模式（本地开发）

```
┌─────────────┐
│  Planner    │  分析需求，制定计划
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Developer  │  实现代码，修复 bug
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Tester    │  测试验收，反馈问题
└──────┬──────┘
       │
       ▼
    循环或完成
```

**状态机**：

```
PLANNING ──→ DEVELOPMENT ──→ TESTING
    ↑            │              │
    │            ▼              ▼
    └────── (fix) ←────── (pass/fail)
                              │
                              ▼
                          COMPLETED
```

### 2. PR Pipeline 模式（GitHub 交付）

```
┌──────────────┐
│ Validate Git │  检查 Git/GitHub 环境
└──────┬───────┘
       │
       ▼
┌──────────────┐
│    Commit    │  提交代码变更
└──────┬───────┘
       │
       ▼
┌──────────────┐
│     Push     │  推送到 GitHub
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Create PR   │  创建或更新 PR
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Check Review │  检查 Codex 审查状态
└──────┬───────┘
       │
       ├─→ Approved ──→ Auto-merge ──→ END
       │
       ├─→ Needs Fixes ──→ END
       │
       └─→ Waiting ──→ Wait for Review ──→ END
```

**PR 状态流转**：

```
INIT ──→ COMMIT ──→ PUSH ──→ CREATE_PR ──→ CHECK_REVIEW
                                               │
                                               ├─→ AUTO_MERGE ──→ COMPLETED
                                               │
                                               ├─→ WAIT_REVIEW (暂停)
                                               │
                                               └─→ FAILED
```

## 安装

```bash
# 从源码安装
git clone https://github.com/yourusername/zeperion.git
cd zeperion
pip install -e .

# 或使用 pip（发布后）
pip install zeperion
```

## 快速开始

### Multi-Agent 模式（本地开发）

#### 1. 初始化项目

```bash
# 在你的项目目录中
zeperion init

# 这会创建以下文件：
# - requirement.txt (需求描述)
# - .zeperion/config.yaml (配置文件)
# - .zeperion/state/ (状态目录)
```

#### 2. 编写需求

编辑 `requirement.txt`：

```
实现一个用户认证系统，包括：
1. 用户注册（邮箱 + 密码）
2. 用户登录（JWT token）
3. 密码加密存储（bcrypt）
4. 登录失败限流（5次/分钟）
```

#### 3. 运行工作流

```bash
# 启动多智能体工作流（自动生成 thread_id: "main"）
zeperion run --mode multi_agent

# 使用自定义 thread_id（方便管理多个任务）
zeperion run --mode multi_agent --thread-id feature-auth

# 查看所有运行中的任务
zeperion list

# 查看特定任务的详细状态
zeperion status --thread-id feature-auth

# 恢复中断的工作流
zeperion run --mode multi_agent --resume --thread-id feature-auth
```

### PR Pipeline 模式（GitHub 交付）

#### 前置条件

1. **Git 仓库**：项目必须是 Git 仓库
2. **GitHub CLI**：安装 `gh` CLI 工具（https://cli.github.com/）
3. **GitHub Token**：设置 `GITHUB_TOKEN` 环境变量或在配置文件中配置

```bash
# 安装 GitHub CLI
# macOS
brew install gh

# Linux
sudo apt install gh

# 认证
gh auth login
```

#### 使用流程

```bash
# 1. 先运行 multi_agent 模式完成开发
zeperion run --mode multi_agent --thread-id feature-auth

# 2. 开发完成后，运行 PR pipeline 创建 PR
zeperion run --mode pr_pipeline --thread-id feature-auth-pr

# 3. 查看 PR 状态
zeperion status --thread-id feature-auth-pr

# 4. 如果 Codex 还未审查，可以稍后恢复检查
zeperion run --mode pr_pipeline --resume --thread-id feature-auth-pr
```

#### PR Pipeline 工作流程

1. **Validate Git**：检查 Git 仓库、GitHub CLI、Token
2. **Commit Changes**：提交所有代码变更
3. **Push Branch**：推送到 GitHub
4. **Create/Update PR**：创建新 PR 或更新已有 PR
5. **Check Codex Review**：检查 Codex 审查状态
   - **👍 >= 1**：Codex 批准 → 启用 auto-merge
   - **评论 > 5**：需要修复 → 结束流程
   - **已审查但等待**：触发 `@codex review` → 暂停
   - **未审查**：暂停，等待外部触发
6. **Auto-merge**：启用 squash + delete branch

#### 配置 PR Pipeline

编辑 `.zeperion/config.yaml`：

```yaml
# GitHub 配置
github:
  token: ${GITHUB_TOKEN}  # 或直接填写
  repo: owner/repo-name   # 可选，自动从 git remote 检测
  target_branch: main     # PR 目标分支
  
  # Codex 审查配置
  codex:
    approval_threshold: 1      # 👍 数量阈值
    comments_threshold: 5      # 评论数量阈值（超过视为需要修复）
    wait_interval: 60          # 轮询间隔（秒）
```

### 4. 管理多个任务

```bash
# 任务 A：用户认证（开发模式）
zeperion run --mode multi_agent --thread-id auth-system

# 任务 B：支付模块（并行运行）
zeperion run --mode multi_agent --thread-id payment-module

# 任务 C：auth-system 的 PR（交付模式）
zeperion run --mode pr_pipeline --thread-id auth-system-pr

# 查看所有任务
zeperion list
# 输出：
# ┌──────────────────┬─────────────┬───────┬─────────────┬───────────────┬──────────────────┐
# │ Thread ID        │ Phase       │ Round │ Test Status │ Global Status │ Updated          │
# ├──────────────────┼─────────────┼───────┼─────────────┼───────────────┼──────────────────┤
# │ payment-module   │ TESTING     │     2 │ PASS        │ CONTINUE      │ 2026-05-13 14:30 │
# │ auth-system      │ DEVELOPMENT │     1 │ PENDING     │ CONTINUE      │ 2026-05-13 14:25 │
# │ auth-system-pr   │ CHECK_REVIEW│     - │ -           │ WAITING       │ 2026-05-13 14:35 │
# └──────────────────┴─────────────┴───────┴─────────────┴───────────────┴──────────────────┘

# 恢复任务 A
zeperion run --mode multi_agent --resume --thread-id auth-system
```

## 配置

编辑 `.zeperion/config.yaml`。配置是一份**扁平**的 YAML（直接对应
`WorkflowConfig` 的 Pydantic 字段），不要写嵌套的 `anthropic:` /
`github:` block —— 加载器只会把顶层 key 透传给 `WorkflowConfig(**)`：

```yaml
# 入口需求
requirement_file: ./requirement.txt

# 三个 role 各自的 agent 后端：anthropic（API）或 claude_code（CLI）
planner_agent_type: anthropic
developer_agent_type: anthropic    # 想真正改文件请改成 claude_code
tester_agent_type: anthropic

# 模型
planner_model: claude-opus-4-7
developer_model: claude-sonnet-4-6
tester_model: claude-opus-4-7

# 工作流
max_rounds: 10                     # 默认 10；从 50 降下来防止解析失败时烧 token
max_fix_attempts: 3

# Claude Code CLI 调谐（只有 developer/tester_agent_type=claude_code 时才用得上）
# 实际 CLI flag 由 ClaudeCodeAgent 内部拼装（--print --model --add-dir
# --permission-mode），无法通过配置覆盖。
claude_cli_tool: claude
claude_cli_timeout: 600
claude_cli_use_worktree: false
claude_cli_keep_worktree: true

# Anthropic API 凭据从环境变量读：
#   export ANTHROPIC_API_KEY=sk-ant-...
# 不要写在配置里。

# GitHub PR Pipeline（顶层字段，不是嵌套 block）
github_repo: owner/repo-name       # 可选，未填则从 git remote 自动识别
# github_token 从环境变量读：export GITHUB_TOKEN=ghp_...
pr_target_branch: main             # 默认 main
pr_auto_merge: true                # APPROVED 后是否启用 auto-merge
codex_poll_minutes: 30
max_pr_fixer_rounds: 5             # pr_fixer 最多自动改几轮，防 ping-pong
  
  # Codex 审查配置
  codex:
    approval_threshold: 1      # 👍 数量阈值（>= 1 视为批准）
    comments_threshold: 5      # 评论数量阈值（> 5 视为需要修复）
    wait_interval: 60          # 轮询间隔（秒）
```

### Agent 类型说明

**AnthropicAgent（推荐）**：
- 直接调用 Anthropic API
- 独立运行，不依赖 Claude Code
- 需要设置 `ANTHROPIC_API_KEY` 环境变量
- 适合生产环境和 CI/CD

**ClaudeCodeAgent**：
- 通过 subprocess 调用 Claude Code CLI
- 适合在 Claude Code 环境内运行
- 需要安装 `claude` CLI 工具
- 适合开发和调试

## 高级用法

### 自定义 Agent

仓库自带的 Agent 只有两种：

- `AnthropicAgent` — 直接调 Anthropic Messages API。**不带工具能力，
  只产出文本**（见顶部黄牌）。适合 Planner / Tester。
- `ClaudeCodeAgent` — 通过 subprocess 调用 `claude --print` CLI。
  CLI 自身可读写文件，是 Developer 真正能"改代码"的唯一路径。

要自己接其他后端（OpenAI / Gemini / Ollama / Azure 等），继承
`BaseAgent` 并实现 `invoke()`，复用 `self.parse_output(raw)` 即可
得到与内置 agent 一致的字段解析（含本仓库对 Planner/Tester 必填字段
的 BLOCKED 行为）。具体写法见 [`AGENT_GUIDE.md`](AGENT_GUIDE.md)。

> 历史版本的 README 在这里贴过 `OpenAIAgent` / `OllamaAgent` 的"示例"，
> 但仓库里**并没有**这些实现，会误导用户以为安装即用。本节已收回，
> 等真有人实现并提交时再恢复。

### 自定义工作流

```python
from langgraph.graph import StateGraph
from zeperion.models import WorkflowState
from zeperion.agents import ClaudeCodeAgent

# 创建自定义图
workflow = StateGraph(WorkflowState)

# 添加节点
workflow.add_node("custom_node", custom_node_func)

# 添加边
workflow.add_edge("custom_node", "developer")

# 编译
app = workflow.compile()
```

### 使用 Python API

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
        max_rounds=50,
        max_fix_attempts=3,
    )

    async with AsyncSqliteSaver.from_conn_string(".zeperion/state/checkpoints.db") as saver:
        graph = create_multi_agent_graph(
            config,
            checkpointer=saver,
            thread_id="my-run-001",
        )
        initial_state = create_initial_state(config)
        await graph.ainvoke(
            initial_state,
            {"configurable": {"thread_id": "my-run-001"}},
        )


asyncio.run(main())
```

## 状态管理

ZEPERION 使用 LangGraph 的 SQLite 检查点机制持久化状态，每个 `thread_id`
一份历史。**没显式传 `--thread-id` 时，默认取当前 git 分支名**（经过文件系统
安全字符过滤），不在 git 仓库或处于 detached HEAD 时回退到 `"main"`。
这意味着两条 PR 在不同分支上跑会自动隔离、互不覆盖。

```bash
# 列出所有 thread_id 及当前阶段
zeperion list

# 查看单个 thread 的详细状态（含 PR Pipeline）
zeperion status --thread-id feature-auth

# 从检查点恢复
zeperion run --resume --thread-id feature-auth
```

**命名建议**：

- 用描述性的短名（`auth-system` / `payment-api` / `bug-fix-123`）。
- 不要用随机串、不要在不同项目复用同一 `thread_id`、不要让两个并发
  `zeperion run` 共享同一个 `thread_id`（会让 events.jsonl 和 checkpoint
  同时被两个进程写入，是最难调试的一类损坏）。
- 想直接读 checkpoint，用 LangGraph 官方 API（不要 `pickle.loads` 走
  内部表）：

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async with AsyncSqliteSaver.from_conn_string(".zeperion/state/checkpoints.db") as saver:
    async for snapshot in saver.alist(None):
        print(snapshot.config["configurable"]["thread_id"], snapshot.checkpoint["channel_values"])
```

## 测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_agents.py

# 带覆盖率
pytest --cov=zeperion
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 代码格式化
black zeperion tests

# 类型检查
mypy zeperion

# Linting
ruff check zeperion
```

## 故障排查

### Agent 调用超时

增加 `config.yaml` 中的 `cli.timeout` 值：

```yaml
cli:
  timeout: 1200  # 20 分钟
```

### 解析失败

检查 Agent 输出是否符合格式要求。启用调试日志：

```bash
export ZEPERION_LOG_LEVEL=DEBUG
zeperion run
```

### 状态损坏

重置状态并重新开始：

```bash
rm -rf .zeperion/state/
zeperion run
```

## 与 Bash 旧版本的区别

历史上 ZEPERION 曾有一份 bash 实现（`.ai_longrun_harness/`），现已迁出主线，仅保留在 `legacy/bash-harness` 分支供查阅。新功能不再向 bash 版同步。

| 特性 | Bash 旧版（legacy 分支） | LangGraph Python 版（main） |
|------|-----------------------|-----------------------------|
| 类型安全 | 无 | 有，基于 Pydantic |
| 输出解析 | awk/grep 严格匹配 | `SectionParser` 宽松匹配 |
| 状态持久化 | 手写 JSON 文件 | LangGraph SQLite 检查点 |
| 多任务并行 | 共享文件易冲突 | 按 `thread_id` 分目录隔离 |
| 可测试性 | 无 | `pytest` 覆盖核心路径 |
| 错误恢复 | 人工干预 | `zeperion run --resume` 续跑 |

如需查看旧实现：

```bash
git show legacy/bash-harness:.ai_longrun_harness/run_multi_agent_loop.sh
```

## 贡献

欢迎贡献！请查看 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT License - 详见 [LICENSE](LICENSE)。

## 相关项目

- [LangGraph](https://github.com/langchain-ai/langgraph) - 状态机框架
- [LangChain](https://github.com/langchain-ai/langchain) - LLM 应用框架
- [Claude](https://www.anthropic.com/claude) - AI 助手

## 联系

- Issues: https://github.com/yourusername/zeperion/issues
- Discussions: https://github.com/yourusername/zeperion/discussions
