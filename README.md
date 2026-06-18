# Zeperion — 一键式自动开发

给它一个需求，它自动写代码、跑测试、修 bug，直到全绿。

```bash
zeperion "实现一个 GET /health 端点"   # 开始干活
zeperion status                         # 看进度
zeperion accept                         # 满意就合入
```

## 安装

```bash
pip install -e ".[dev]"
```

需要 [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) 可用（`claude --version`）。

## 快速开始

```bash
# 初始化项目（一次性）
zeperion init

# 直接开始（内联需求）
zeperion "给项目加上用户认证功能"

# 或者写到文件再跑
echo "实现 REST API 的 CRUD 接口" > requirement.txt
zeperion run
```

## 工作原理

```
需求 → Planner（拆任务）→ Developer（写代码）→ Tester（跑测试）
                ↑                                        ↓
                └──── 失败时自动修复，修不好换思路重来 ────┘
```

- **Developer** 用 Claude Code CLI 直接读写你的项目文件
- **Tester** 运行真实测试命令（自动检测 pytest/go test/npm test）
- 失败时 Developer 看到**真实测试报错**来修复（不是二手转述）
- 同一个错误重复出现 → 自动升级给 Planner 换思路
- 修复耗尽 → 重新规划，不放弃（直到 rounds 用完）

## 观测运行

```bash
# 精简事件流
zeperion logs --follow

# 详细步骤（像前台一样看到所有 tool call）
zeperion logs --follow --verbose

# 状态面板
zeperion status --watch

# 只看错误
zeperion logs --follow --errors-only
```

## 后台运行

```bash
zeperion run --detach "实现 X"
# → pid=12345, thread=feature-x
# → zeperion logs -t feature-x -f -v   查看详细
# → zeperion stop -t feature-x         停止
```

## 常用命令

| 命令 | 用途 |
|------|------|
| `zeperion init` | 初始化项目（生成 .zeperion/config.yaml） |
| `zeperion run "需求"` | 运行开发循环 |
| `zeperion status` | 查看当前状态 |
| `zeperion logs -f -v` | 实时详细日志 |
| `zeperion changes` | 查看本次改动 |
| `zeperion accept` | 合入改动到工作树 |
| `zeperion discard` | 丢弃本次运行 |
| `zeperion verify` | 手动跑验证命令 |
| `zeperion doctor` | 检查环境是否就绪 |
| `zeperion stop` | 停止后台运行 |

## 配置

`zeperion init` 生成的 `.zeperion/config.yaml` 只有核心字段：

```yaml
requirement_file: ../requirement.txt
project_dir: ..
state_dir: state
planner_agent_type: claude_code
developer_agent_type: claude_code
tester_agent_type: claude_code
enable_reviewer: false
tester_verify_commands: []
```

所有高级选项（模型、超时、fallback、PR pipeline、workspace 等）都有合理默认值，不写就用默认。详见 [高级配置](docs/advanced.md)。

## 高级功能

- **PR Pipeline**：自动 commit → push → 开 PR → 等审查 → 合并
- **Run Workspace**：在隔离的 git worktree 里运行，不污染工作树
- **Reviewer**：`enable_reviewer: true` 开启代码审查环节
- **Fallback Models**：主模型挂了自动切备用
- **Token Budget**：`max_total_tokens` 控制单次运行消耗上限

详细文档：[docs/](docs/)

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
