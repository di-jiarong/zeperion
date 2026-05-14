# ZEPERION-PR 交付管线

开发完成后的自动提交流程。

## 使用方法

```bash
/zeperion-pr --target dev
/zeperion-pr --target feat/dijia-test-pr --title "feat: add new feature"
```

## 参数

- `--target` 或 `-t`: 目标分支（默认 dev）
- `--title`: PR 标题（可选，默认自动生成）
- `--poll`: Codex 等待分钟数（默认 30）

## 执行逻辑

1. **解析参数**：从用户输入中提取参数
2. **设置环境变量**：
   - `export PR_TARGET_BRANCH="目标分支"`
   - `export PR_TITLE="标题"` (如果指定)
   - `export CODEX_POLL_MINUTES="分钟数"` (如果指定)
3. **调用脚本**：`bash .ai_longrun_harness/run_pr_pipeline.sh`

## 流程

```
git commit + push → 创建 PR → 等 Codex 审查
    │
    ├── Codex LGTM/👍 → auto-merge → CI/CD → Merge ✅
    │
    └── Codex P0/P1/P2(blocking) → 收集全部 comments
         │
         ▼
    一次性修完所有 blocking issues → 测试通过
         │
         ▼
    git push（同一個 PR）→ @codex review → 回到审查
```

## 执行步骤

### Step 1: Commit & Push
- 将所有改动文件 stage + commit
- commit message 按 Conventional Commits 格式
- push 到当前分支的 remote

### Step 2: Create PR
- 用 `gh pr create` 创建 PR
- target = 指定的目标分支
- 打 `automerge` 标签触发 CI

### Step 3: 等 Codex 出结果
- **不要手动干预**，让 GitHub Actions 自动处理
- 不要启动多个轮询进程
- 如果 Codex 给 👍 → auto-merge 自动接手
- 如果 Codex 有 comments → 进入 Step 4

### Step 4: 批量修复（一次性修完再推）
```
① 收集 ALL Codex comments 到 state/codex_comments.txt
② 按 severity 分级：
   P0/P1 = 必修，阻塞 merge
   P2   = 影响正确性/安全才修，否则忽略
   P3/nit = 忽略
③ 一次性修复所有 blocking issues
④ 跑测试
⑤ 一次性 git push（不每修一个就推一次）
⑥ PR 评论 @codex review 触发重审
⑦ 回到 Step 3
```

### 禁止行为
- ❌ 修一个 comment 就 push 一次
- ❌ 手动合并 PR（让 auto-merge 处理）
- ❌ 在 auto-merge workflow 运行时多次 @codex review

### Step 5: Auto-merge
- Codex 通过后，GitHub Actions 的 Auto Merge 工作流自动处理
- CI/CD 通过后自动 squash-merge
- 源分支自动删除
