# 状态文件管理

## 概述

ZEPERION 工作流使用 JSON 文件追踪运行状态。这些文件位于 `.ai_longrun_harness/state/` 目录。

## 状态文件说明

### 核心状态文件

| 文件 | 用途 | 何时更新 |
|------|------|----------|
| `workflow_state.json` | 多智能体循环状态 | 每个 phase 切换时 |
| `pipeline_state.json` | PR 管线状态 | PR 创建、Codex 审查时 |
| `progress.json` | 整体进度追踪 | 每轮开始/结束时 |

### 工作文件

| 文件 | 用途 |
|------|------|
| `current_plan.txt` | Planner 输出的计划 |
| `task_result.txt` | Developer 输出的结果 |
| `test_report.txt` | Tester 输出的测试报告 |
| `lessons_learned.txt` | 累积的经验教训 |
| `codex_comments.txt` | Codex 审查意见 |

### Resume 文件

| 文件 | 用途 |
|------|------|
| `planner.resume_id` | Planner session ID |
| `developer.resume_id` | Developer session ID |
| `tester.resume_id` | Tester session ID |

## 状态文件初始化

### 自动初始化

脚本启动时会自动检查状态文件：

```bash
# run_multi_agent_loop.sh 和 run_pr_pipeline.sh 都会：
1. 检查状态文件是否存在
2. 验证 JSON 格式是否正确
3. 如果缺失或损坏，自动创建初始状态
```

### 手动重置

使用 `reset_state.sh` 脚本重置所有状态：

```bash
cd .ai_longrun_harness
bash reset_state.sh
```

这会：
1. ✅ 备份当前状态到 `state/backups/YYYYMMDD_HHMMSS/`
2. ✅ 重置所有 JSON 状态文件
3. ✅ 清空日志文件
4. ✅ 保留 `lessons_learned.txt`（经验库）

## 状态文件模板

### workflow_state.json

```json
{
  "status": "idle|running|paused|completed|failed",
  "phase": "init|planner|developer|tester|task_pass|fix_limit",
  "round": 0,
  "task_id": "",
  "fix_attempt": 0,
  "owner_dev_session": "",
  "owner_test_session": "",
  "last_error": "",
  "updated_at": "2026-05-12T10:30:00+08:00"
}
```

### pipeline_state.json

```json
{
  "status": "idle|running|waiting_codex|fixing|completed|failed",
  "phase": "init|commit|push|create_pr|codex_review|fix_batch|merge",
  "pr_branch": "feat/my-feature",
  "pr_target": "dev",
  "pr_number": "123",
  "pr_url": "https://github.com/owner/repo/pull/123",
  "codex_status": "pending|approved|changes_requested",
  "updated_at": "2026-05-12T10:30:00+08:00"
}
```

### progress.json

```json
{
  "mode": "multi_agent",
  "status": "idle|running|completed|failed",
  "round": 1,
  "fix_attempt": 0,
  "updated_at": "2026-05-12T10:30:00+08:00"
}
```

## Git 忽略规则

状态文件**不应该**提交到 Git，因为它们是运行时数据。

`.gitignore` 配置：

```gitignore
# ZEPERION state files (runtime data)
.ai_longrun_harness/state/
.ai_longrun_harness/config.env
```

## 已知陷阱

### GitHub API 分页截断（严重）

**症状**：`gh api ...comments` 总是返回 30 条，Codex 明明有新评论但轮询看不到。

**根因**：GitHub API 默认每页 30 条。评论超过 30 条后新评论被截断到下一页。

**修复**：**永远**使用以下之一：
- `gh api "...?per_page=100"` — 单页上限 100
- `gh api --paginate "..."` — 自动翻页获取全量

**影响**：PR #4 曾因此漏掉 46/76 条评论，持续 9 轮审查未被发现。

### Cron 重复触发

**症状**：一次修完 push 后立刻手动再 `@codex review`，导致 Codex 多次审查中间提交。

**修复**：push + 触发一次后，Cron 静默等待至少 1 小时。不要手动重新触发。

### 状态文件过时

**症状**：`pipeline_state.json` 还记录 PR #2 但实际工作在 PR #4。

**修复**：每次 Phase 转换必须同步更新 pipeline_state.json / workflow_state.json / progress.json。

---

## 故障排查

### 状态文件损坏

**症状**：脚本启动时报 JSON 解析错误

**解决**：
```bash
# 方法 1: 自动修复（脚本会重新初始化）
bash .ai_longrun_harness/run_multi_agent_loop.sh

# 方法 2: 手动重置
bash .ai_longrun_harness/reset_state.sh
```

### 状态不一致

**症状**：工作流行为异常，phase 不匹配

**解决**：
```bash
# 重置状态并重新开始
bash .ai_longrun_harness/reset_state.sh
/zeperion --branch feat/new-start
```

### 恢复备份

```bash
# 查看备份
ls -la .ai_longrun_harness/state/backups/

# 恢复特定备份
cp .ai_longrun_harness/state/backups/20260512_103000/*.json \
   .ai_longrun_harness/state/
```

## 最佳实践

### 1. 定期清理

每次开始新功能前重置状态：

```bash
bash .ai_longrun_harness/reset_state.sh
/zeperion --branch feat/new-feature
```

### 2. 保留经验库

`lessons_learned.txt` 包含宝贵的经验，不要删除：

```bash
# reset_state.sh 会自动保留它
# 如果需要手动清理，先备份
cp .ai_longrun_harness/state/lessons_learned.txt ~/backup/
```

### 3. 检查状态

开发过程中随时检查状态：

```bash
# 查看当前状态
cat .ai_longrun_harness/state/workflow_state.json | jq

# 查看 PR 状态
cat .ai_longrun_harness/state/pipeline_state.json | jq
```

### 4. 备份重要状态

在关键节点手动备份：

```bash
# 创建快照
SNAPSHOT=".ai_longrun_harness/state/snapshots/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SNAPSHOT"
cp .ai_longrun_harness/state/*.json "$SNAPSHOT/"
cp .ai_longrun_harness/state/*.txt "$SNAPSHOT/"
```

## 状态文件生命周期

```
初始化
  ↓
[idle] → 脚本启动 → [running]
  ↓
Planner → Developer → Tester
  ↓
[task_pass] → PR Pipeline → [completed]
  ↓
重置状态 → [idle]
```

## 相关文档

- [USAGE.md](./USAGE.md) - 使用指南
- [SETUP.md](./template/SETUP.md) - 快速设置
- [PERMISSIONS.md](./template/PERMISSIONS.md) - 权限配置
