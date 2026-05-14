# ZEPERION 多智能体开发交付流程 — 完整流程图

## 总览

```
                     用户提交任务
                          │
                    ┌─────┴─────┐
                    │ /zeperion  │  --branch feat/xxx（可选）
                    │ /zeperion  │  需求描述
                    └─────┬─────┘
                          │
                    Master Scheduler
                    评估任务复杂度
                          │
              ┌───────────┴───────────┐
              │ 简单任务               │ 复杂任务（3+文件/新模块/需测试）
              │ 直接做                 │ 进入多智能体模式
              └───────────────────────┘
                          │
                    ┌─────┴─────┐
                    │ 确认分支？  │  ⚠️ 未指定 --branch 时询问
                    │ 当前/新分支 │
                    └─────┬─────┘
                          │
                          ▼
              ┌─────────────────────────┐
              │      Phase 1: Planner   │
              │  Agent(Explore) 调研     │
              │  输出 current_plan.txt   │
              └────────────┬────────────┘
                           │
                      ┌────┴────┐
                      │ 用户确认 │  确认方案
                      └────┬────┘
                           │ 确认
                           ▼
              ┌─────────────────────────┐
              │    Phase 2: Developer   │
              │  Agent(general-purpose)  │
              │  按计划实现 + 写测试      │
              │  输出 task_result.txt    │
              └────────────┬────────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │     Phase 3: Tester     │
              │  Agent(general-purpose)  │
              │  独立验证 + 跑测试        │
              │  输出 test_report.txt    │
              └────────────┬────────────┘
                           │
                    ┌──────┴──────┐
                    │ PASS        │ FAIL
                    ▼             ▼
              提取 Lessons    ┌──────────┐
                ↓             │ Fix Loop │ (最多3次)
           全部完成?           │ Developer │→ Tester
                │             └──────────┘
           ┌────┴────┐
           │ 是       │ 否
           ▼          ▼
      ⚠️ 自动进入    回到 Planner
      交付管线
      （不等待用户确认）
```

## Codex 审查循环（详细）

```
  Tester PASS + 全部完成
           │
           ▼
  ┌─────────────────┐
  │  git commit      │  Conventional Commits 格式
  │  git push        │  推到 feature 分支
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  gh pr create    │  目标: dev/main
  │  打 automerge 标签 │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────────────┐
  │  CronCreate 启动轮询     │  ⚠️ 每10分钟，per_page=100
  │  记录 baseline 评论数    │  ⚠️ 一个PR只一个Cron
  │  写入 pipeline_state    │
  └────────┬────────────────┘
           │
           ▼
  ┌─────────────────────────┐
  │  等 2 分钟判断 Codex      │
  │  是否已自动开始审查        │
  │  已自动 → 跳过手动触发    │
  │  未自动 → gh pr comment   │  ⚠️ 创建时最多手动一次
  │           "@codex review" │
  └────────┬────────────────┘
           │
           ▼
  ┌─────────────────────────────────┐
  │         Cron 每 N 分钟轮询       │
  │                                 │
  │  gh api "...?per_page=100"      │ ← 必须加 per_page！
  │  对比评论总数 vs baseline        │
  │  对比 reviews 数 vs 上次          │
  │                                 │
  │  ┌──────┬──────┬──────────┐     │
  │  │ 增加  │ 不变  │ 有 👍    │     │
  │  └──┬───┴──┬───┴────┬─────┘     │
  └─────┼──────┼────────┼───────────┘
        │      │        │
        ▼      ▼        ▼
    新审查结果 静默继续  审查通过
        │               │
        │               ▼
        │    ┌──────────────────┐
        │    │   CronDelete     │
        │    │   更新 state      │
        │    │   通知用户        │
        │    │   → Auto-merge   │
        │    └──────────────────┘
        │
        ▼
  ┌──────────────────────────┐
  │  CronDelete 当前任务       │
  │  收集全部 Codex comments   │  ⚠️ per_page=100 防截断
  │  写入 codex_comments.txt  │
  └──────────┬───────────────┘
             │
             ▼
  ┌──────────────────────────┐
  │  按优先级分级处理          │
  │  P0/P1 = 必修，阻塞        │
  │  P2   = 影响安全才行       │
  │  P3/nit = 忽略            │
  └──────────┬───────────────┘
             │
             ▼
  ┌──────────────────────────┐
  │  一次性修复所有 blocking    │  ← 不是修一个推一个！
  │  go fmt + go test        │
  └──────────┬───────────────┘
             │
             ▼
  ┌──────────────────────────┐
  │  git commit + push        │
  │  更新 state 文件           │  ← pipeline_state / workflow_state / progress
  └──────────┬───────────────┘
             │
             ▼
  ┌──────────────────────────┐
  │  清理旧 Cron（如有）        │
  │  启动新 Cron + @codex      │
  └──────────┬───────────────┘
             │
             ▼
      回到「Cron 轮询」继续循环
```

## 关键规则清单

| 规则 | 说明 |
|------|------|
| **批量修复** | 收集全部 comments → 一次性修完 → 一次性 push |
| **per_page=100** | 所有 gh api comments 调用必须加，默认30条会截断 |
| **1 Cron / PR** | 创建新 Cron 前 CronList + CronDelete 旧的 |
| **不重复触发** | @codex review 触发一次后，至少等1小时 |
| **状态实时同步** | 每次 Phase 转换更新 pipeline_state / workflow_state / progress |
| **修复上限** | Developer → Tester 循环最多3次 |
| **AGENTS.md 格式** | 通过时必须 👍 + LGTM + Safe to merge |
| **禁止事项** | 修一个推一个 / 中途手动merge / 多个Cron同时跑 |

## 状态文件

```
.ai_longrun_harness/state/
├── workflow_state.json    ← 当前阶段/轮次/fix_attempt
├── pipeline_state.json    ← PR编号/CronID/审查轮次
├── progress.json          ← 整体进度/完成轮次
├── current_plan.txt       ← Planner 输出
├── task_result.txt        ← Developer 输出
├── test_report.txt        ← Tester 输出
├── lessons_learned.txt    ← 累积经验（不reset）
├── codex_comments.txt     ← Codex 审查意见
└── errors.log             ← 异常日志
```

## Cron 管理

```
创建前:
  CronList → 检查是否已有 → 有则 CronDelete

创建时:
  CronCreate(durable=true, cron="*/10 * * * *")
  prompt 内必须包含：
    - per_page=100 参数
    - 评论数/审查数对比逻辑
    - 检测到 👍 时 CronDelete 自己

结束时:
  审查通过 → CronDelete
  不再需要 → CronDelete
```
