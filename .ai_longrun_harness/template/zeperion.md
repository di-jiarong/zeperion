# ZEPERION 多智能体开发交付流程

当你收到 `/zeperion` 命令时，启动完整的 ZEPERION 开发交付管线。

## 使用方法

```bash
/zeperion --branch feat/new-feature
```

## 参数

- `--branch` 或 `-b`: 新功能分支名（可选）
  - 如果指定，会自动从当前分支创建新分支并切换
  - 如果不指定，在当前分支上开发

## 执行逻辑

1. **解析参数**：从用户输入中提取 `--branch` 或 `-b` 参数
2. **⚠️ 如果未指定分支，先询问用户**：在当前分支开发还是切新分支？
3. **设置环境变量**：`export FEATURE_BRANCH="分支名"`
4. **调用脚本**：`bash .ai_longrun_harness/run_multi_agent_loop.sh`
5. **脚本会自动**：
   - 如果 FEATURE_BRANCH 已设置且不在该分支上，自动创建/切换分支
   - 如果分支已存在，直接切换
   - 如果未设置，在当前分支上开发

## 整体流程

```
确认分支 → Planner → 用户确认方案 → Developer → Tester → PASS → ⚠️ 自动进入 PR Pipeline（不等待）
```

---

## Phase 1: 多智能体开发循环

严格按照 CLAUDE.md 中定义的多智能体工作流执行：

### Master Scheduler（我）
负责编排全局，拆解决策，追踪状态。

### Planner Agent
- 用 Agent(Explore) 调研代码库
- 输出结构化 plan 到 `ai_longrun_harness/state/current_plan.txt`
- 格式：TASK_ID, PLAN 项带验收标准, RISKS, HANDOFF_TO_DEVELOPER

### Developer Agent
- 用 Agent(general-purpose) 实现代码
- 输出到 `ai_longrun_harness/state/task_result.txt`
- 格式：DEV_STATUS, CHANGES, LESSONS

### Tester Agent
- 用 Agent(general-purpose) 独立质检
- 输出到 `ai_longrun_harness/state/test_report.txt`
- TEST_STATUS: PASS/FAIL → FAIL 则回 Developer 修复（最多 3 次）

### Lessons Learned
- 每轮提取 LESSONS 追加到 `ai_longrun_harness/state/lessons_learned.txt`

---

## Phase 2: PR 交付管线

Tester PASS 后，进入交付阶段：

### 步骤
1. **Commit + Push** — 添加所有改动文件，commit，push 到远程分支
2. **创建 PR** — 用 `gh pr create` 创建 PR，打上 `automerge` 标签
3. **等待 Codex 审查** — 不手动干预，让 GitHub Actions 自动处理
   - 不要在审查中途 push 或 `@codex review`
   - 不要启动多个轮询进程
   - 等 Codex 出完整结果（最多 30 分钟）

### Codex 审查结果处理（批量修复原则）

**⚠️ 一次只推一次，修完所有再推**

```
① Codex 返回全部 comments
② 收集到 ai_longrun_harness/state/codex_comments.txt
③ 按 severity 分级处理：
   P0/P1 = 必修，阻塞 merge
   P2   = 影响正确性/安全/稳定性才必修，否则可延后
   P3/nit = 忽略（不阻塞 merge）
④ 一次性修复所有 P0/P1/P2(必修)
⑤ 运行测试确认全部通过
⑥ 一次性 git commit + push（不每修一个就推一次）
⑦ 在 PR 评论 @codex review 触发重审
⑧ 回到 ①，直到 Codex 说 LGTM 或给 👍
⑨ Codex 通过 → auto-merge 自动处理
```

### 禁止行为
- ❌ 修一个 comment 就 push 一次（浪费 CI 资源）
- ❌ 在 auto-merge workflow 运行时手动 @codex review（造成 workflow 互相 cancel）
- ❌ 启动多个轮询任务同时监听同一个 PR
- ❌ 中途手动 merge

---

## 状态追踪

更新 `ai_longrun_harness/state/workflow_state.json`：
```json
{
  "status": "running|paused|completed",
  "phase": "planner|developer|tester|codex_review|fix_batch",
  "round": 1,
  "fix_attempt": 0,
  "last_error": ""
}
```

---

## 关键规则

1. Tester 和 Developer 保持 session 一致（修复时回到原 session）
2. 测试失败最多修复 3 次
3. 每轮经验写入 lessons_learned.txt
4. Codex 审查结果保存到 codex_comments.txt，一次性批量修复
5. PR 创建后自动打 `automerge` 标签以触发 CI/CD
6. AGENTS.md 中定义了 Codex 审查规则（P0/P1/P2 分级）
7. 权限已设为 accept 模式，不再提示确认
