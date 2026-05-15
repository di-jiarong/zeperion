# PR Pipeline 使用指南

本文档详细说明 ZEPERION 的 PR Pipeline 模式，用于自动化 GitHub PR 的创建、审查和合并流程。

## 概述

PR Pipeline 是 ZEPERION 的第二种工作模式，专注于将本地开发的代码自动交付到 GitHub。它通过以下步骤完成完整的 PR 生命周期：

1. **验证环境**：检查 Git 仓库、GitHub CLI、Token
2. **提交代码**：自动 commit 所有变更
3. **推送分支**：push 到 GitHub
4. **创建 PR**：创建或更新 Pull Request
5. **等待审查**：监控 Codex 审查状态
6. **自动合并**：审查通过后启用 auto-merge

## 前置条件

### 1. Git 仓库

项目必须是一个 Git 仓库：

```bash
# 初始化 Git 仓库（如果还没有）
git init

# 添加远程仓库
git remote add origin https://github.com/owner/repo.git
```

### 2. GitHub CLI

安装并认证 GitHub CLI：

```bash
# macOS
brew install gh

# Linux (Debian/Ubuntu)
sudo apt install gh

# Linux (其他发行版)
# 参考：https://github.com/cli/cli/blob/trunk/docs/install_linux.md

# 认证
gh auth login
```

### 3. GitHub Token

设置 GitHub Personal Access Token：

```bash
# 方式 1：环境变量（推荐）
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# 方式 2：配置文件
# 编辑 .zeperion/config.yaml
github:
  token: ghp_xxxxxxxxxxxx
```

**Token 权限要求**：
- `repo`（完整仓库访问）
- `workflow`（如果需要触发 GitHub Actions）

## 基本用法

### 完整流程示例

```bash
# 1. 初始化项目
zeperion init

# 2. 编写需求
echo "实现用户认证功能" > requirement.txt

# 3. 运行 Multi-Agent 模式完成开发
zeperion run --mode multi_agent --thread-id auth-feature

# 4. 开发完成后，运行 PR Pipeline
zeperion run --mode pr_pipeline --thread-id auth-feature-pr

# 5. 查看 PR 状态
zeperion status --thread-id auth-feature-pr

# 6. 如果需要，恢复检查
zeperion run --mode pr_pipeline --resume --thread-id auth-feature-pr
```

### 自定义 PR 信息

通过配置文件或状态文件自定义 PR 标题和描述：

```yaml
# .zeperion/config.yaml
github:
  pr_title: "feat: Add user authentication system"
  pr_body: |
    ## Changes
    - Implemented user registration
    - Added JWT token authentication
    - Integrated bcrypt password hashing
    
    ## Testing
    - All unit tests pass
    - Integration tests added
```

## 工作流详解

### 阶段 1：Validate Git

**目的**：验证环境是否满足 PR Pipeline 的要求

**检查项**：
- 是否在 Git 仓库中
- `gh` CLI 是否安装
- `GITHUB_TOKEN` 是否设置
- 自动检测 GitHub 仓库名称

**失败处理**：
- 如果检查失败，工作流立即终止并提示错误信息

### 阶段 2：Commit Changes

**目的**：提交所有本地代码变更

**行为**：
- 检查是否有未提交的变更（`git status`）
- 如果有变更，执行 `git add -A` 和 `git commit`
- 如果没有变更，使用当前分支状态

**Commit Message 格式**：
```
<pr_title or task_id>

Changed files:
- file1.py
- file2.py
- ... (最多列出 20 个文件)
```

> 历史版本会在 commit body 末尾追加一行
> `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`，已删除：
> 实际跑代码的 backend 不一定是 Claude（live test 跑过 DeepSeek，
> 用户可以接 GPT/Llama），且这个邮箱也不是真实可映射地址。
> Git 自己的 author 字段已经记录了真正运行 zeperion 的人，那才是
> 诚实的归因。

### 阶段 3：Push Branch

**目的**：将本地分支推送到 GitHub

**行为**：
- 执行 `git push origin <branch>`
- 如果分支不存在，自动创建远程分支

### 阶段 4：Create or Update PR

**目的**：创建新 PR 或更新已有 PR

**行为**：
- 检查是否已存在 PR（`gh pr list`）
- 如果存在，更新 PR 标题（如果提供）
- 如果不存在，创建新 PR

**PR Body 自动生成**：
```markdown
## Changes

<git log 摘要>

## Commits

- commit1 message
- commit2 message

## Files Changed

- file1.py
- file2.py
```

### 阶段 5：Check Codex Review

**目的**：检查 Codex 审查状态

**Codex 状态判断**：

| 条件 | Codex 状态 | 下一步 |
|------|-----------|--------|
| 👍 >= 1 | `APPROVED` | 启用 auto-merge |
| 已审查 && 评论 > 5 | `NEEDS_FIXES` | 结束流程（需要手动修复）|
| 已审查 && 评论 <= 5 | `WAITING` | 触发 `@codex review` 并暂停 |
| 未审查 | `PENDING` | 暂停，等待外部触发 |

**审查数据来源**：
- 使用 `gh api` 获取 PR 评论
- 过滤 Codex 用户的评论
- 统计 👍 反应和评论数量

### 阶段 6：Auto-merge

**目的**：启用 GitHub auto-merge 功能

**行为**：
- 执行 `gh pr merge --auto --squash --delete-branch <pr_url>`
- 合并策略：squash（压缩所有 commit）
- 合并后自动删除分支

**触发条件**：
- Codex 状态为 `APPROVED`
- 所有 CI 检查通过（由 GitHub 自动判断）

### 阶段 7：Wait for Review

**目的**：暂停工作流，等待审查完成

**行为**：
- 如果 Codex 已审查但未批准，添加 `@codex review` 评论
- 工作流进入 `END` 状态（可通过 `--resume` 恢复）

**恢复检查**：
```bash
# 等待一段时间后，恢复检查审查状态
zeperion run --mode pr_pipeline --resume --thread-id <thread_id>
```

## 配置选项

### GitHub 配置

```yaml
github:
  # 必需配置
  token: ${GITHUB_TOKEN}           # GitHub Personal Access Token
  
  # 可选配置
  repo: owner/repo-name            # GitHub 仓库（默认自动检测）
  target_branch: main              # PR 目标分支（默认 main）
  pr_title: "feat: ..."            # PR 标题（默认使用 task_id）
  pr_body: "..."                   # PR 描述（默认自动生成）
  
  # Codex 审查配置
  codex:
    approval_threshold: 1          # 👍 数量阈值（默认 1）
    comments_threshold: 5          # 评论数量阈值（默认 5）
    wait_interval: 60              # 轮询间隔秒数（默认 60）
```

### 环境变量

```bash
# GitHub Token（优先级高于配置文件）
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# 自定义 GitHub CLI 路径
export GH_PATH=/usr/local/bin/gh
```

## 常见场景

### 场景 1：首次创建 PR

```bash
# 1. 开发完成
zeperion run --mode multi_agent --thread-id feature-x

# 2. 创建 PR
zeperion run --mode pr_pipeline --thread-id feature-x-pr

# 输出：
# ✅ Validated Git environment
# ✅ Committed changes (3 files)
# ✅ Pushed branch: feature-x
# ✅ Created PR #123: https://github.com/owner/repo/pull/123
# ⏳ Codex review pending, workflow paused
```

### 场景 2：更新已有 PR

```bash
# 1. 修改代码
# ... 编辑文件 ...

# 2. 重新运行 PR Pipeline（使用相同 thread_id）
zeperion run --mode pr_pipeline --thread-id feature-x-pr

# 输出：
# ✅ Found existing PR #123
# ✅ Committed new changes (2 files)
# ✅ Pushed branch: feature-x
# ✅ Updated PR #123
# ⏳ Codex review pending
```

### 场景 3：Codex 批准后自动合并

```bash
# 1. 等待 Codex 审查
# ... Codex 在 GitHub 上添加 👍 ...

# 2. 恢复检查
zeperion run --mode pr_pipeline --resume --thread-id feature-x-pr

# 输出：
# ✅ Codex approved (👍 = 1)
# ✅ Enabled auto-merge (squash + delete branch)
# ✅ PR will merge automatically when CI passes
```

### 场景 4：Codex 要求修复

```bash
# 1. 恢复检查
zeperion run --mode pr_pipeline --resume --thread-id feature-x-pr

# 输出：
# ⚠️ Codex needs fixes (8 comments)
# ❌ Workflow ended, please address feedback manually

# 2. 查看反馈
gh pr view 123

# 3. 修复问题后重新运行
# ... 修复代码 ...
zeperion run --mode pr_pipeline --thread-id feature-x-pr
```

## 故障排查

### 问题 1：`gh: command not found`

**原因**：GitHub CLI 未安装

**解决**：
```bash
# macOS
brew install gh

# Linux
sudo apt install gh
```

### 问题 2：`GITHUB_TOKEN not set`

**原因**：未配置 GitHub Token

**解决**：
```bash
# 方式 1：环境变量
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# 方式 2：配置文件
# 编辑 .zeperion/config.yaml
github:
  token: ghp_xxxxxxxxxxxx
```

### 问题 3：`Not in a git repository`

**原因**：当前目录不是 Git 仓库

**解决**：
```bash
git init
git remote add origin https://github.com/owner/repo.git
```

### 问题 4：`gh auth status` 失败

**原因**：GitHub CLI 未认证

**解决**：
```bash
gh auth login
# 按提示完成认证流程
```

### 问题 5：PR 创建失败（权限不足）

**原因**：Token 权限不足

**解决**：
1. 访问 https://github.com/settings/tokens
2. 创建新 Token，勾选 `repo` 权限
3. 更新 `GITHUB_TOKEN`

## 最佳实践

### 1. 分离开发和交付

```bash
# 开发阶段：使用 multi_agent 模式
zeperion run --mode multi_agent --thread-id dev-feature-x

# 交付阶段：使用 pr_pipeline 模式
zeperion run --mode pr_pipeline --thread-id pr-feature-x
```

### 2. 使用有意义的 Thread ID

```bash
# ❌ 不好
zeperion run --mode pr_pipeline --thread-id pr1

# ✅ 好
zeperion run --mode pr_pipeline --thread-id auth-system-pr
```

### 3. 定期检查 PR 状态

```bash
# 查看所有 PR Pipeline 任务
zeperion list | grep pr_pipeline

# 查看特定 PR 的详细状态
zeperion status --thread-id auth-system-pr
```

### 4. 自动化 CI/CD 集成

```yaml
# .github/workflows/zeperion.yml
name: ZEPERION PR Pipeline

on:
  workflow_dispatch:
    inputs:
      thread_id:
        description: 'Thread ID'
        required: true

jobs:
  pr-pipeline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install ZEPERION
        run: pip install zeperion
      - name: Run PR Pipeline
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          zeperion run --mode pr_pipeline --thread-id ${{ github.event.inputs.thread_id }}
```

## 限制和注意事项

1. **GitHub CLI 依赖**：PR Pipeline 依赖 `gh` CLI，无法使用纯 API 实现
2. **Codex 特定**：审查逻辑针对 Codex 设计，其他审查者需要自定义
3. **单分支限制**：一个 thread_id 对应一个分支，不支持多分支并行
4. **网络依赖**：需要稳定的网络连接到 GitHub
5. **权限要求**：需要仓库的 write 权限

## 扩展阅读

- [GitHub CLI 文档](https://cli.github.com/manual/)
- [GitHub Auto-merge 文档](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request)
- [LangGraph Checkpointing](https://langchain-ai.github.io/langgraph/concepts/persistence/)
