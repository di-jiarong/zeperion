# ZEPERION 使用指南

## 快速开始

### 0. 重置状态（推荐）

每次开始新功能前，重置状态文件：

```bash
cd .ai_longrun_harness
bash reset_state.sh
```

这会备份旧状态并重置所有状态文件到初始状态。

### 1. 初始化新项目

```bash
cd /path/to/your/project
bash /path/to/.ai_longrun_harness/template/zeperion-init.sh
```

初始化后会自动：
- 创建 `.claude/settings.json` 和 `.claude/settings.local.json`（权限预配置）
- 创建自定义命令：`/zeperion` 和 `/zeperion-pr`
- 创建 `CLAUDE.md` 和 `AGENTS.md` 文档
- 配置 `.gitignore` 忽略 `.ai_longrun_harness/` 和 `.claude`

### 2. 复制管线脚本

```bash
cp -r /path/to/source/.ai_longrun_harness ./
```

### 3. 配置环境变量

编辑 `.ai_longrun_harness/config.env`：

```bash
# GitHub 配置
export GITHUB_TOKEN="ghp_xxx"
export GITHUB_REPO="owner/repo"

# 模型配置
export MASTER_MODEL="claude-opus-4-7"
export PLANNER_MODEL="claude-opus-4-7"
export DEVELOPER_MODEL="claude-sonnet-4-6"
export TESTER_MODEL="claude-opus-4-7"

# 多智能体调用模板
export MULTI_AGENT_CALL_TEMPLATE="claude --model {model} --prompt-file {prompt_file} > {output_file}"
```

---

## 使用场景

### 场景 1: 在当前分支开发新功能

```bash
# 在 Claude Code 中执行
/zeperion

# 然后输入需求描述
```

工作流会：
1. 在当前分支上开发
2. Planner → Developer → Tester 循环
3. 测试通过后提示你运行 `/zeperion-pr`

### 场景 2: 自动创建新分支开发

```bash
/zeperion --branch feat/add-user-auth
```

工作流会：
1. 自动创建 `feat/add-user-auth` 分支（如果不存在）
2. 切换到该分支
3. 开始 Planner → Developer → Tester 循环

### 场景 3: 提交 PR 到默认分支（dev）

```bash
/zeperion-pr
```

会自动：
1. Commit 所有改动
2. Push 到远程
3. 创建 PR 到 `dev` 分支
4. 等待 Codex 审查

### 场景 4: 提交 PR 到指定分支

```bash
/zeperion-pr --target main
/zeperion-pr --target feat/parent-feature
```

适用于：
- 提交到主分支
- 提交到父功能分支（多层级开发）

### 场景 5: 自定义 PR 标题

```bash
/zeperion-pr --target dev --title "feat: implement user authentication system"
```

---

## 参数说明

### `/zeperion` 参数

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--branch` | `-b` | 新功能分支名 | 当前分支 |

### `/zeperion-pr` 参数

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--target` | `-t` | PR 目标分支 | `dev` |
| `--title` | - | PR 标题 | 自动生成 |
| `--poll` | - | Codex 等待分钟数 | 30 |

---

## 工作流状态文件

所有状态文件位于 `.ai_longrun_harness/state/`：

```
state/
├── workflow_state.json      # 当前工作流状态
├── pipeline_state.json      # PR 管线状态
├── current_plan.txt         # Planner 输出的计划
├── task_result.txt          # Developer 输出的结果
├── test_report.txt          # Tester 输出的测试报告
├── lessons_learned.txt      # 经验库
├── codex_comments.txt       # Codex 审查意见
└── logs/                    # 各角色的日志文件
```

---

## 常见问题

### Q: 如何暂停工作流？

按 `Ctrl+C` 中断脚本，状态会保存在 `workflow_state.json` 中。

### Q: 如何恢复工作流？

重新运行 `/zeperion`，脚本会自动从上次状态恢复。

### Q: Codex 审查失败怎么办？

工作流会自动：
1. 收集所有 Codex comments
2. 按优先级分类（P0/P1/P2/P3）
3. 一次性修复所有 blocking issues
4. 重新测试
5. Push 并触发 Codex 重审

### Q: 如何跳过 Codex 审查？

不建议跳过。如果必须，可以手动合并 PR：

```bash
gh pr merge <PR-NUMBER> --squash
```

### Q: 权限提示太多怎么办？

初始化脚本已经预配置了 60+ 条权限规则。如果还有提示：

1. 检查 `.claude/settings.local.json` 是否存在
2. 重启 Claude Code 会话
3. 查看 `.ai_longrun_harness/template/PERMISSIONS.md` 了解如何添加自定义权限

---

## 最佳实践

### 1. 分支命名规范

```
feat/功能名        # 新功能
fix/问题描述       # Bug 修复
refactor/重构内容  # 重构
docs/文档更新      # 文档
test/测试内容      # 测试
```

### 2. 需求描述规范

在 `.ai_longrun_harness/requirement_template.txt` 中清晰描述：

```
## 功能目标
实现用户认证系统

## 验收标准
1. 用户可以注册账号
2. 用户可以登录/登出
3. 密码加密存储
4. Session 管理

## 技术约束
- 使用 JWT token
- 密码使用 bcrypt 加密
- Session 过期时间 24 小时
```

### 3. 多层级开发

对于复杂功能，可以分层开发：

```bash
# 第一层：主功能分支
/zeperion --branch feat/user-system

# 开发完成后提交到 dev
/zeperion-pr --target dev

# 第二层：子功能分支
git checkout feat/user-system
/zeperion --branch feat/user-system-oauth

# 提交到父分支
/zeperion-pr --target feat/user-system
```

---

## 故障排查

### 脚本执行失败

检查日志文件：

```bash
tail -f .ai_longrun_harness/state/logs/planner.log
tail -f .ai_longrun_harness/state/logs/developer.log
tail -f .ai_longrun_harness/state/logs/tester.log
```

### Git 操作失败

确保：
1. `GITHUB_TOKEN` 已设置且有效
2. 远程仓库存在
3. 有 push 权限

### Codex 审查超时

默认等待 30 分钟，可以调整：

```bash
/zeperion-pr --poll 60  # 等待 60 分钟
```

---

## 相关文档

- [STATE_MANAGEMENT.md](./STATE_MANAGEMENT.md) - 状态文件管理详解
- [SETUP.md](./template/SETUP.md) - 快速设置
- [PERMISSIONS.md](./template/PERMISSIONS.md) - 权限配置

### 自定义模型

编辑 `config.env`：

```bash
# 使用更快的模型加速开发
export DEVELOPER_MODEL="claude-sonnet-4-6"

# 使用更强的模型提升质量
export TESTER_MODEL="claude-opus-4-7"
```

### 调整重试次数

```bash
export MAX_FIX_ATTEMPTS=5  # 测试失败最多修复 5 次
export ROLE_CALL_MAX_RETRIES=3  # 角色调用失败最多重试 3 次
```

### 自定义 Prompt

编辑 `.ai_longrun_harness/prompts/` 下的模板文件：

- `planner_prompt.txt` - Planner 角色提示词
- `developer_prompt.txt` - Developer 角色提示词
- `tester_prompt.txt` - Tester 角色提示词
