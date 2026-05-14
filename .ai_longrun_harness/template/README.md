# ZEPERION 项目模板

## 文件清单

```
你的项目/
├── .claude/
│   ├── setting.json            # Claude Code 配置（API、模型、权限、命令）
│   └── commands/
│       ├── zeperion.md         # /zeperion — 完整开发交付管线
│       └── zeperion-pr.md      # /zeperion-pr — PR 交付管线
├── AGENTS.md                   # Codex Cloud 审查规则（P0/P1/P2/P3）
├── CLAUDE.md                   # 多智能体工作流协议
└── .ai_longrun_harness/
    ├── run_pr_pipeline.sh      # PR 交付脚本
    ├── prompts/                # 智能体角色提示词
    ├── state/                  # 运行状态文件
    └── usage.txt               # 使用说明
```

## 使用方式

```bash
# 初始化新项目
cd /path/to/new/project
bash /path/to/zeperion-init.sh

# 开始开发
/zeperion 实现 XXX 功能

# 提交 PR
/zeperion-pr --target dev
```

## 依赖

- Claude Code CLI
- GitHub CLI (`gh`) 已登录
- 项目已关联 GitHub 远程仓库

## 自定义

| 文件 | 按需修改 |
|------|---------|
| `.claude/setting.json` | API 端点、模型、权限模式 |
| `AGENTS.md` | Codex 审查严格程度 |
| `CLAUDE.md` | 工作流协议细节 |
| `.ai_longrun_harness/config.env` | PR 目标分支等 |
