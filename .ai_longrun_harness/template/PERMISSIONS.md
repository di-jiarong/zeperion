# ZEPERION 权限配置说明

## 概述

`.settings.local.json` 包含了 ZEPERION 多智能体开发流程所需的所有权限预配置。

初始化新项目时，这个文件会自动复制到 `.claude/settings.local.json`，让你在使用全流程编程时无需手动同意权限。

## 包含的权限类别

### 1. 基础 Shell 命令
- 文件操作：`cat`, `grep`, `rg`, `awk`, `sed`, `find`, `ls`, `cp`, `mv`
- 目录操作：`mkdir`, `cd`, `pwd`
- 文本处理：`jq`, `wc`, `head`, `tail`, `echo`, `printf`
- 其他：`date`, `sleep`, `chmod`, `touch`

### 2. Shell 控制结构
- 条件判断：`test`, `[ ]`, `[[ ]]`
- 环境变量：`export`, `source`
- 脚本执行：`bash`, `sh`, `eval`, `exit`

### 3. Git 命令
- 查看状态：`git status`, `git diff`, `git log`, `git show`
- 分支操作：`git branch`, `git checkout`, `git fetch`
- 提交推送：`git add`, `git commit`, `git push`
- 其他：`git reset`, `git restore`, `git lfs`, `git ls-remote`, `git rev-parse`

### 4. GitHub CLI
- PR 管理：`gh pr *`
- API 调用：`gh api *`
- 工作流：`gh run *`, `gh workflow *`
- 其他：`gh auth *`, `gh repo *`, `gh issue *`

### 5. 网络请求
- `curl`, `wget`
- WebFetch 域名：`chatgpt.com`, `github.com`, `api.github.com`

### 6. Python 生态
- `python`, `python3`, `pip`, `pip3`, `uv`

### 7. Node.js 生态
- `npm`, `node`, `npx`, `yarn`, `pnpm`

### 8. Claude Code
- `claude *` (所有 claude 命令)

### 9. ai_longrun_harness 目录权限
- 读取：`Read(/home/*/project/*/.ai_longrun_harness/**)`
- 写入状态：`Write(/home/*/project/*/.ai_longrun_harness/state/**)`
- 编辑：`Edit(/home/*/project/*/.ai_longrun_harness/**)`

## 使用方式

### 初始化新项目
```bash
cd /path/to/new/project
bash /path/to/.ai_longrun_harness/template/zeperion-init.sh
```

初始化脚本会自动复制 `.settings.local.json` 到新项目的 `.claude/settings.local.json`。

### 手动添加权限
如果需要添加项目特定的权限，编辑 `.claude/settings.local.json`：

```json
{
  "permissions": {
    "allow": [
      "现有权限...",
      "Bash(你的新命令 *)"
    ]
  }
}
```

## 安全说明

- `.settings.local.json` 模板文件不会提交到 git（已在 `.gitignore` 中）
- 生成的 `.claude/settings.local.json` 也不会提交（`.claude` 目录被忽略）
- 这些权限仅在本地生效，不影响团队其他成员
- 权限使用通配符 `*`，覆盖常见参数组合
- 路径权限使用 `/home/*/project/*/` 模式，适配不同用户和项目

## 权限规则语法

- `Bash(命令 *)` - 允许该命令及所有参数
- `Read(路径)` - 允许读取指定路径
- `Write(路径)` - 允许写入指定路径
- `Edit(路径)` - 允许编辑指定路径
- `WebFetch(domain:域名)` - 允许访问指定域名

## 故障排查

如果某个命令仍然需要权限确认：

1. 检查命令是否在 allowlist 中
2. 检查通配符是否匹配
3. 手动添加该命令到 `.claude/settings.local.json`
4. 重启 Claude Code 会话

## 更新模板

如果需要更新模板权限配置：

1. 编辑 `.ai_longrun_harness/template/.settings.local.json`
2. 新项目初始化时会使用最新版本
3. 已有项目需要手动更新 `.claude/settings.local.json`
