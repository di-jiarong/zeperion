# 示例：用户认证系统

这个目录是 ZEPERION 的一个**可复现端到端示例**：跑一次 multi-agent
流程，把它真实写到磁盘上的所有产物（每轮的 planner / developer /
tester 输出、`events.jsonl`、`lessons_learned.txt`）原样保存到
`transcript/` 子目录里。

> 之前这个示例 README 列了一份"理想化"的预期产出（包括 `src/auth/main.py`
> 等源码文件）—— 那是误导。默认的 `anthropic` agent 没有任何工具能力，
> 不会写任何源代码。本次更新把它改成一份**真实跑出来的样子**，包括
> 测试失败 → 修复 → 通过 → 进入下一轮 → 完成的完整循环。
> 详见仓库根目录 `README.md` 顶部的黄牌警告。

## 目录结构

```
examples/auth-system/
├── README.md                    # 本文件
├── config.yaml                  # 扁平 ZEPERION 配置（注意：旧版的嵌套写法已废弃）
├── requirement.txt              # 输入需求
├── run_demo.py                  # 复现脚本：用 FakeAgent 跑一次完整流程
└── transcript/                  # run_demo.py 的产出，commit 进仓库
    ├── lessons_learned.txt      # 跨轮经验沉淀
    ├── threads/demo/            # "最新一份"产出（latest snapshot）
    │   ├── planner_output.txt
    │   ├── developer_output.txt
    │   └── tester_output.txt
    └── runs/demo/               # 每轮 / 每次 fix 的归档产物
        ├── events.jsonl                       # 结构化事件流
        ├── round_001_planner.txt
        ├── round_001_developer.txt
        ├── round_001_tester.txt               # FAIL
        ├── round_001_developer_fix_1.txt      # 修复
        ├── round_001_tester_fix_1.txt         # PASS
        ├── round_002_planner.txt
        ├── round_002_developer.txt
        └── round_002_tester.txt               # PASS + GLOBAL_STATUS=DONE
```

## 这次"跑"的故事

`run_demo.py` 用一个 `FakeAgent` 喂 8 段预先写好的 LLM 输出（覆盖 2 轮
+ 1 次 round 1 的 fix attempt），跑通了 ZEPERION 真实的状态机：

| 步骤 | 角色 | 关键产出 |
|------|------|---------|
| Round 1 | Planner | 拆出 P1/P2/P3，emit `TASK_ID=auth_v1_bootstrap`、`PR_TITLE`、`GLOBAL_STATUS=CONTINUE` |
| Round 1 | Developer | 报告写了 `app/security.py` 等文件 |
| Round 1 | Tester | `TEST_STATUS=FAIL`，报告 `created_at` 默认值缺失 + `verify_password` 错误返回 |
| Round 1 fix | Developer | 修两处 bug |
| Round 1 fix | Tester | `TEST_STATUS=PASS` |
| Round 2 | Planner | 进入下一组任务（注册 API + 限流 stub） |
| Round 2 | Developer | 报告实现完成 |
| Round 2 | Tester | `TEST_STATUS=PASS`, `GLOBAL_STATUS=DONE` —— 流程结束 |

最终 state：

```
phase: completed
test_status: PASS
global_status: DONE
```

注意 Developer 的产出**只是文本**——没有 `app/security.py` 真正生成。
要让 Developer 能改你项目里的文件，必须在 `config.yaml` 里把
`developer_agent_type` 改成 `claude_code`，由 `claude` CLI 自己完成 IO。

## 如何复现

```bash
cd examples/auth-system
python3 run_demo.py
```

脚本会清空并重写 `transcript/` 目录。本次提交里的 `transcript/` 内容
就是这条命令的产物，跨机器跑结果应该是 byte-identical（FakeAgent 是
确定性的，时间戳除外）。

## 想跑一次"真的"

如果你有 Anthropic API key 并且想看真实的 LLM 跑：

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# 在你自己的项目目录里（不是 examples/auth-system）
cd my-real-project
zeperion init                       # 生成 .zeperion/config.yaml + requirement.txt
$EDITOR requirement.txt             # 写需求
$EDITOR .zeperion/config.yaml       # 把 developer_agent_type 改成 claude_code
zeperion run --mode multi_agent --thread-id auth-system
zeperion status --thread-id auth-system
```

## 想自动开 PR

跑完 multi_agent 之后：

```bash
zeperion run --mode pr_pipeline --thread-id auth-system-pr
```

注意：PR Pipeline 默认 `pr_target_branch=main`、`pr_auto_merge=true`，
按需在 `config.yaml` 里调。

## 旧版本的 README 怎么了

历史 README 在这里贴过 8 步幻想式的预期输出，包括 ZEPERION 自动生成
`src/auth/main.py` 等文件、跑 alembic、`uvicorn ... --reload` 起服务
的整套示意。**那些都不会发生**：默认的 anthropic 后端只产生文本。
保留那种 README 是在骗自己。本次替换为真实可复现的产物。
