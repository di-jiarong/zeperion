# DimOS 多智能体开发工作流

## 概述

复杂任务（多文件改动、需测试验证、多步骤实现）时，自动进入多智能体工作流。
我（Claude）作为 **Master Scheduler** 编排全局，用 `Agent` 工具 spawn 子 agent 承担专业角色。

简单任务（修 typo、单函数改动、回答问题）直接做，不走此流程。

## 四种角色

| 角色 | 谁承担 | 职责 | Agent 类型 |
|------|--------|------|-----------|
| **Master Scheduler** | Claude | 拆解决策、编排调度、状态追踪、经验积累 | — |
| **Planner** | 子 agent | 调研代码库、拆子任务、输出可验收计划 | Explore |
| **Developer** | 子 agent | 按计划实现、写测试 | general-purpose |
| **Tester** | 子 agent | 独立质检、跑测试、输出 PASS/FAIL | general-purpose |

## 工作流状态机

```
用户提交复杂任务
    │
    ▼
┌─────────────────────┐
│  Master Scheduler    │  我评估复杂度，决定进入多智能体模式
│  解析需求            │
│  ⚠️ 确认是否切新分支   │  询问用户：在当前分支开发还是切新分支？
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Planner Agent       │  调研代码结构，输出 plan 到
│  (Explore)           │  .ai_longrun_harness/state/current_plan.txt
└────────┬────────────┘
         │
    ┌────┴────┐
    │ 用户确认 │  展示计划给用户，确认后再执行
    └────┬────┘
         │
         ▼
    ┌────────┐
    │ Fix    │←────────────┐
    │ Loop   │              │
    ├────────┤              │
    │ ▼      │              │
    │Developer│  实现，写入   │  FAIL
    │Agent   │  task_result  │
    ├────────┤              │
    │ ▼      │              │
    │Tester  │  验证，写入   │
    │Agent   │  test_report ─┘
    └────────┘
         │ PASS
         ▼
    经验写入 lessons_learned.txt
         │
         ▼
    检查是否全部完成
         │
    ┌────┴────┐
    │ DONE    │  还有下一个子任务 → 回到 Planner
    │ 完成报告│
    └────┬────┘
         │ 全部完成
         ▼
    ⚠️ 自动进入 PR 交付管线（不用等确认）
```

## 状态文件

全部位于 `.ai_longrun_harness/state/`：

| 文件 | 用途 |
|------|------|
| `workflow_state.json` | 当前阶段、轮次、fix_attempt、owner |
| `current_plan.txt` | Planner 输出的当前轮计划 |
| `task_result.txt` | Developer 的实现结果 |
| `test_report.txt` | Tester 的验证报告 |
| `lessons_learned.txt` | 跨轮次经验积累 |
| `progress.json` | 进度摘要 |
| `errors.log` | 异常记录 |
| `task_queue.txt` | 任务队列（单 agent 模式用） |

## 调用规则

### 何时进入多智能体模式

满足任一条件：
- 任务涉及 3+ 个文件改动
- 需要新增模块/功能
- 需要测试验证
- 涉及调试排障
- 用户明确要求"用多智能体流程"

### Planner 调用

```python
Agent(
    description="Plan implementation for: <task>",
    subagent_type="Explore",
    prompt=f"""你是 Planner Agent。任务是调研并制定实现计划。

需求：{task}

经验库（避免重复踩坑）：
{lessons}

输出格式：
TASK_ID: task_xxx
PLAN:
- [P1] 子任务描述（验收标准）
- [P2] 子任务描述
- [P3] 子任务描述
RISKS:
- 风险描述
"""
)
```

Planner 输出写入 `.ai_longrun_harness/state/current_plan.txt`。
**重要：计划输出后先展示给用户确认，再进入开发阶段。**

### Developer 调用

```python
Agent(
    description="Implement: <plan_summary>",
    subagent_type="general-purpose",
    prompt=f"""你是 Developer Agent。按计划实现，不要扩大范围。

当前计划：{plan}

经验库：{lessons}

测试报告（修复轮次）：{test_report}

输出格式：
DEV_STATUS: DONE | BLOCKED
CHANGES:
- 变更点
BLOCKERS:
- 若无写 NONE
LESSONS:
- 本轮经验
"""
)
```

Developer 输出写入 `.ai_longrun_harness/state/task_result.txt`。

### Tester 调用

```python
Agent(
    description="Test: <plan_summary>",
    subagent_type="general-purpose",
    prompt=f"""你是 Tester Agent。独立验证开发结果。

开发结果：{task_result}

经验库：{lessons}

输出格式：
TEST_STATUS: PASS | FAIL
TEST_CASES:
- 用例：结果
BUGS:
- 若无写 NONE
FIX_REQUEST:
- 若 FAIL，给出最小修复动作
LESSONS:
- 测试经验
"""
)
```

Tester 输出写入 `.ai_longrun_harness/state/test_report.txt`。

### 修复循环规则

1. Tester 输出 FAIL → 更新 fix_attempt → 回到 Developer（传入 test_report 作为上下文）
2. Developer 修复后 → 再次 Tester 验证
3. 同一个 Tester session 复验（保持 owner 一致）
4. 最多 **3 次**修复尝试，超出则停止并报告
5. Tester 输出 PASS → 提取双方 LESSONS 追加到 `lessons_learned.txt`

## Lessons Learned 管理

每轮完成后：
1. 从 Developer 和 Tester 输出中提取 `LESSONS:` 段
2. 追加到 `.ai_longrun_harness/state/lessons_learned.txt`
3. 后续所有子 agent 的 prompt 中加载 lessons 作为上下文

## 状态追踪

每进入一个新阶段，更新 `.ai_longrun_harness/state/workflow_state.json`：

```json
{
  "status": "running",
  "phase": "planner|developer|tester",
  "round": 1,
  "task_id": "task_001",
  "fix_attempt": 0,
  "owner_dev_session": "",
  "owner_test_session": "",
  "last_error": ""
}
```

## 交付管线

**开发完成（Tester PASS + 全部子任务完成）后，自动进入 PR 交付流程，无需等待用户确认。**

```
git commit + push → 创建 PR → Codex Cloud 审查
    │                              │
    │                         ┌────┴────┐
    │                         │ 👍      │ ❌/超时
    │                         └────┬────┘
    │                              │
    │                              ▼
    │                          PR Fixer 修 bug
    │                              │
    │                              ▼
    │                          re-push → re-poll
    │                              │
    ▼                              ▼
    Auto-merge → CI/CD → Merge 完成
```

脚本：`bash .ai_longrun_harness/run_pr_pipeline.sh`

需要设置 `GITHUB_TOKEN` 环境变量，以及 `GITHUB_REPO`（如 `topsun-bot/topsun_dimos`）。

## Codex 审查修复策略

**批量修复原则**（不是每改一个就提一次 PR）：

```
① Codex 返回所有 review comments
② 收集全部 issues 到 state/codex_comments.txt
③ 一次性修复所有 valid 的 issues
④ 运行测试确认全部通过
⑤ 一次性 git commit + push
⑥ **仅在 Codex 未自动重审时才在 PR 评论发 @codex review 手动触发**
⑦ Cron 轮询等结果，**禁止 20 分钟内就重复触发**
⑧ 如果还有新的 comments → 回到 ②
⑨ Codex 👍 → auto-merge
```

**⚠️ @codex review 触发规则（避免重复触发）：**
- **PR 创建时**：如果 Codex 已配置自动审查 → 不需要手动发 @codex review
- **修复 push 后**：Codex 不会自动重审 → 必须手动发 @codex review
- 触发后至少等 1 小时再考虑重新触发，不要每 20 分钟触发一次
- 修一个 comment → push → 修一个 → push 会浪费 CI 和 Codex 资源

审查意见保存在 `.ai_longrun_harness/state/codex_comments.txt`。

### Cron 生命周期管理（铁律）

1. **一个 PR 同时只允许一个 Cron** — 创建新 Cron 前必须 `CronList` 检查 + `CronDelete` 清理旧任务
2. **Codex 审查通过后必须立即 CronDelete** — 不能让残留 Cron 导致重复提交
3. **Cron prompt 内必须包含自己清理自己的逻辑** — 检测到 👍/APPROVED 时 CronDelete
4. **每次创建 Cron 后，将 Cron ID 写入 `pipeline_state.json` 的 `cron_job_id` 字段**

### PR 创建后的 Cron 轮询

**PR 创建后必须用 CronCreate 启动轮询，不能用 sleep 循环傻等。**

```
PR 创建成功后 → 立即启动 Cron 轮询任务（CronCreate）
  │
  ├── Cron 频率：每 10 分钟一次
  ├── durable: true（持久化到 .claude/scheduled_tasks.json）
  ├── ⚠️ 必须用 gh api "...?per_page=100" 获取评论（默认 30 条/页会截断！）
  ├── 轮询逻辑：
  │     - 记录上次评论总数作为基线
  │     - 每次轮询对比评论总数和 reviews 数量
  │     - 有增长 → CronDelete 停任务，获取详情通知用户
  │     - 有 👍 → CronDelete，通知通过
  │     - 无变化 → 静默
  │
  ├── 有新结果 → 通知用户，CronDelete 删除轮询任务
  ├── 无变化 → 静默继续，不上报
  └── **修复 push 后才需要手动 @codex review，PR 创建时如果 Codex 已自动审查则不需要**
```

**Cron 轮询 prompt 模板（含 per_page 和评论计数）：**
```
第一步：记录基线
COMMENT_COUNT=$(gh api "repos/OWNER/REPO/pulls/N/comments?per_page=100" --jq 'length')

轮询：
R=$(gh pr view N --json reviews --jq '.reviews | length')
C=$(gh api "repos/OWNER/REPO/pulls/N/comments?per_page=100" --jq 'length')

如果 R > 上次或 C > 基线 → CronDelete，获取最新评论通知用户
如果 review body 含 👍 → CronDelete，通知通过
否则静默
```

### 每次 Phase 转换必须同步更新状态文件

**状态文件不能过时**，每次 Codex 审查轮次都要更新：
- `pipeline_state.json`：pr_branch, pr_number, review_round, cron_job_id, last_commit, updated_at
- `workflow_state.json`：current_phase, pr_number, last_commit, updated_at
- `progress.json`：codex_review_rounds, updated_at

## 与现有 bash 脚本的关系

| 脚本 | 用途 | 场景 |
|------|------|------|
| `run_multi_agent_loop.sh` | 多智能体开发循环 | 后台无人值守开发 |
| `run_ralph_loop.sh` | 单智能体任务循环 | 简单任务后台执行 |
| `run_pr_pipeline.sh` | PR 交付管线 | 开发完成后的提交流程 |
| CLAUDE.md | 交互式开发协议 | 我在前台编排，用户实时介入 |
