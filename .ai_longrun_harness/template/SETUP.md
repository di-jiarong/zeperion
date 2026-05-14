# ZEPERION 快速设置指南

## 🚀 一键初始化新项目

```bash
cd /path/to/your/new/project
bash /path/to/.ai_longrun_harness/template/zeperion-init.sh
```

## ✅ 初始化后会自动创建

1. **`.claude/setting.json`** - Claude Code 基础配置
2. **`.claude/settings.local.json`** - 权限预配置（60+ 条规则，无需手动同意）
3. **`.claude/commands/zeperion.md`** - `/zeperion` 命令
4. **`.claude/commands/zeperion-pr.md`** - `/zeperion-pr` 命令
5. **`CLAUDE.md`** - 多智能体开发工作流文档
6. **`AGENTS.md`** - Agent 角色定义
7. **`.gitignore`** - 自动配置忽略 `.ai_longrun_harness/` 目录

## 📦 手动复制工作流脚本

初始化脚本只创建配置文件，工作流脚本需要手动复制：

```bash
cp -r /path/to/.ai_longrun_harness /path/to/your/new/project/
```

## 🎯 核心特性

### 1. 零权限提示
初始化后，所有常用命令已预配置权限：
- ✅ Shell 基础命令（cat, grep, sed, awk, jq...）
- ✅ Git 全套命令
- ✅ GitHub CLI (gh)
- ✅ Python/Node.js 生态
- ✅ Claude Code 命令
- ✅ `.ai_longrun_harness` 目录读写

### 2. Git 友好
- `.ai_longrun_harness/` 整个目录不会被提交（自动配置 `.gitignore`）
- `.claude/settings.local.json` 不会被提交
- 团队成员各自维护自己的权限配置
- 初始化脚本会自动检查并更新 `.gitignore`

### 3. 多智能体工作流
- **Master Scheduler** - 全局编排
- **Planner** - 调研代码库，制定计划
- **Developer** - 按计划实现
- **Tester** - 独立质检验证

## 📁 目录结构

```
your-project/
├── .ai_longrun_harness/          # 工作流脚本（不提交）
│   ├── state/                    # 状态文件
│   ├── prompts/                  # Prompt 模板
│   ├── run_multi_agent_loop.sh   # 多智能体循环
│   ├── run_pr_pipeline.sh        # PR 交付管线
│   └── template/                 # 初始化模板
├── .claude/
│   ├── setting.json              # 基础配置（提交）
│   ├── settings.local.json       # 权限配置（不提交）
│   └── commands/                 # 自定义命令
├── CLAUDE.md                     # 工作流文档
└── AGENTS.md                     # Agent 定义
```

## 🔧 环境变量配置

在 `.ai_longrun_harness/config.env` 中配置：

```bash
# GitHub 配置
export GITHUB_TOKEN="ghp_xxx"
export GITHUB_REPO="owner/repo"

# 模型配置
MASTER_MODEL="balanced-model"
PLANNER_MODEL="quality-model"
DEVELOPER_MODEL="fast-model"
TESTER_MODEL="quality-model"

# 循环控制
MAX_ROUNDS=50
MAX_FIX_ATTEMPTS=3
```

## 📝 使用示例

### 启动多智能体开发
```bash
# 在 Claude Code 中
/zeperion
```

### 提交 PR
```bash
# 开发完成后
/zeperion-pr
```

### 后台运行
```bash
# 无人值守开发
bash .ai_longrun_harness/run_multi_agent_loop.sh
```

## 🛠️ 自定义权限

如果需要添加项目特定的权限，编辑 `.claude/settings.local.json`：

```json
{
  "permissions": {
    "allow": [
      "现有权限...",
      "Bash(你的新命令 *)",
      "Read(/your/custom/path/**)"
    ]
  }
}
```

## 📚 更多文档

- **PERMISSIONS.md** - 权限配置详细说明
- **CLAUDE.md** - 多智能体工作流完整文档
- **README.md** - 工作流脚本使用说明

## ⚠️ 注意事项

1. **首次使用**：确保已安装 `gh` CLI 并完成 `gh auth login`
2. **环境变量**：配置 `GITHUB_TOKEN` 和 `GITHUB_REPO`
3. **Git 仓库**：必须在 git 仓库中使用
4. **权限模式**：建议使用 `bypassPermissions` 或 `auto` 模式

## 🐛 故障排查

### 权限仍然需要确认
1. 检查 `.claude/settings.local.json` 是否存在
2. 检查命令是否在 allowlist 中
3. 重启 Claude Code 会话

### 初始化脚本失败
1. 确保有写入权限
2. 检查模板文件是否完整
3. 查看错误日志

### Git 提交了不该提交的文件
1. 检查 `.gitignore` 是否包含 `.ai_longrun_harness/`（初始化脚本会自动添加）
2. 运行 `git rm -r --cached .ai_longrun_harness`
3. 重新提交

## 🎉 开始使用

现在你已经准备好使用 ZEPERION 多智能体开发工作流了！

```bash
cd your-project
/zeperion
```

享受无障碍的全流程编程体验！
