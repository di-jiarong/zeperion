# Thread ID 管理指南

## 什么是 Thread ID？

Thread ID 是 ZEPERION 用来标识和管理不同工作流运行的唯一标识符。每个 thread_id 对应一个独立的检查点，允许你：

- 并行运行多个任务
- 随时中断和恢复任务
- 隔离不同项目的状态

## Thread ID 的确定方式

### 1. 默认 Thread ID

如果不指定，使用默认值 `"main"`：

```bash
zeperion run
# 等同于
zeperion run --thread-id main
```

### 2. 自定义 Thread ID

启动时指定：

```bash
zeperion run --thread-id my-feature
```

**命名建议**：
- 使用描述性名称：`auth-system`, `payment-api`, `bug-fix-123`
- 避免特殊字符，使用 `-` 或 `_` 分隔
- 保持简短（20 字符以内）

## 查看所有运行

使用 `list` 命令查看所有 thread_id 及其状态：

```bash
$ zeperion list

                           Workflow Runs
┌─────────────────┬─────────────┬───────┬─────────────┬───────────────┬──────────────────┐
│ Thread ID       │ Phase       │ Round │ Test Status │ Global Status │ Updated          │
├─────────────────┼─────────────┼───────┼─────────────┼───────────────┼──────────────────┤
│ payment-module  │ TESTING     │     2 │ PASS        │ CONTINUE      │ 2026-05-13 14:30 │
│ auth-system     │ DEVELOPMENT │     1 │ PENDING     │ CONTINUE      │ 2026-05-13 14:25 │
│ main            │ COMPLETED   │     5 │ PASS        │ DONE          │ 2026-05-13 10:15 │
└─────────────────┴─────────────┴───────┴─────────────┴───────────────┴──────────────────┘

Total runs: 3

Resume a run:
  zeperion run --resume --thread-id <THREAD_ID>

Check detailed status:
  zeperion status --thread-id <THREAD_ID>
```

## 查看特定任务状态

```bash
$ zeperion status --thread-id auth-system

╭─────────────────────── ZEPERION ───────────────────────╮
│ Workflow Status                                         │
│                                                         │
│ Phase: DEVELOPMENT                                      │
│ Round: 1                                                │
│ Fix Attempt: 0                                          │
│ Test Status: PENDING                                    │
│ Global Status: CONTINUE                                 │
│ Task ID: task_001                                       │
│ Updated: 2026-05-13T14:25:30                           │
╰─────────────────────────────────────────────────────────╯

Agent Outputs:
...
```

## 恢复任务

### 恢复默认任务

```bash
zeperion run --resume
# 恢复 thread_id="main"
```

### 恢复指定任务

```bash
zeperion run --resume --thread-id auth-system
```

## 使用场景

### 场景 1：单个项目开发

```bash
# 启动（使用默认 main）
zeperion run

# 中断后恢复
zeperion run --resume
```

### 场景 2：多功能并行开发

```bash
# 功能 A
zeperion run --thread-id feature-a

# 功能 B（另一个终端）
zeperion run --thread-id feature-b

# 查看所有任务
zeperion list

# 恢复功能 A
zeperion run --resume --thread-id feature-a
```

### 场景 3：实验性尝试

```bash
# 主线开发
zeperion run --thread-id main

# 实验性方案（不影响主线）
zeperion run --thread-id experiment-v2

# 如果实验失败，直接放弃，继续主线
zeperion run --resume --thread-id main
```

### 场景 4：Bug 修复

```bash
# 正在开发新功能
zeperion run --thread-id feature-payment

# 紧急 bug 需要修复
zeperion run --thread-id hotfix-login-bug

# Bug 修复完成后，继续新功能开发
zeperion run --resume --thread-id feature-payment
```

## 检查点存储位置

所有 thread_id 的检查点存储在：

```
.ai_longrun_harness/state/checkpoints.db
```

这是一个 SQLite 数据库，包含：
- 所有 thread_id 的状态快照
- 每个节点执行后的完整状态
- 时间戳和版本信息

## 清理旧任务

### 手动清理

删除整个检查点数据库（会丢失所有任务）：

```bash
rm .ai_longrun_harness/state/checkpoints.db
```

### 选择性清理

目前需要手动操作 SQLite：

```bash
sqlite3 .ai_longrun_harness/state/checkpoints.db

# 查看所有 thread_id
SELECT DISTINCT thread_id FROM checkpoints;

# 删除特定 thread_id
DELETE FROM checkpoints WHERE thread_id = 'old-task';

# 退出
.quit
```

**未来功能**：计划添加 `zeperion clean` 命令自动清理。

## 最佳实践

### ✅ 推荐做法

1. **使用描述性名称**
   ```bash
   zeperion run --thread-id user-auth-jwt
   ```

2. **定期查看任务列表**
   ```bash
   zeperion list
   ```

3. **完成后记录 thread_id**
   ```bash
   # 完成后记录到文档
   echo "user-auth-jwt: 已完成，2026-05-13" >> tasks.log
   ```

4. **长期项目使用固定 thread_id**
   ```bash
   # 始终使用同一个 thread_id
   zeperion run --thread-id project-alpha
   zeperion run --resume --thread-id project-alpha
   ```

### ❌ 避免做法

1. **不要使用随机字符串**
   ```bash
   # 不好：无法记住
   zeperion run --thread-id a8f3d9e2
   ```

2. **不要重复使用已完成任务的 thread_id**
   ```bash
   # 不好：会覆盖之前的检查点
   zeperion run --thread-id main  # 第一个任务
   zeperion run --thread-id main  # 第二个任务（会混淆）
   ```

3. **不要在不同项目使用相同 thread_id**
   ```bash
   # 不好：不同项目的状态会混在一起
   cd project-a && zeperion run --thread-id dev
   cd project-b && zeperion run --thread-id dev
   ```

## 常见问题

### Q: 忘记了 thread_id 怎么办？

A: 使用 `zeperion list` 查看所有运行：

```bash
zeperion list
```

### Q: 可以删除单个 thread_id 吗？

A: 目前需要手动操作 SQLite，未来会添加 `zeperion clean --thread-id <id>` 命令。

### Q: thread_id 有长度限制吗？

A: 没有硬性限制，但建议保持在 20 字符以内，便于查看和输入。

### Q: 可以重命名 thread_id 吗？

A: 目前不支持，需要在 SQLite 中手动修改。建议一开始就使用合适的名称。

### Q: 多个用户可以共享 thread_id 吗？

A: 不推荐。检查点数据库是本地文件，多用户同时访问可能导致冲突。每个用户应使用独立的 thread_id。

### Q: thread_id 会过期吗？

A: 不会自动过期，除非手动删除检查点数据库。

## 技术细节

### 检查点结构

每个 thread_id 在数据库中存储：

```sql
CREATE TABLE checkpoints (
    thread_id TEXT,
    checkpoint_id INTEGER,
    checkpoint_ns TEXT,
    channel_values BLOB,  -- 序列化的 WorkflowState
    ...
);
```

### 状态恢复机制

```python
# 新任务：传入 initial_state
graph.astream(initial_state, {"configurable": {"thread_id": "new-task"}})

# 恢复任务：不传 initial_state，LangGraph 自动从检查点加载
graph.astream(None, {"configurable": {"thread_id": "existing-task"}})
```

### 并发安全

SQLite 提供文件级锁，确保多个进程不会同时写入同一个 thread_id。但建议避免并发操作同一个 thread_id。

## 参考

- [LangGraph Checkpointing 文档](https://langchain-ai.github.io/langgraph/concepts/persistence/)
- [SQLite 文档](https://www.sqlite.org/docs.html)
