# Contributing to ZEPERION

感谢你对 ZEPERION 的贡献兴趣！

## 开发环境设置

```bash
# 克隆仓库
git clone https://github.com/yourusername/zeperion.git
cd zeperion

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装开发依赖
pip install -e ".[dev]"

# 安装 pre-commit hooks
pre-commit install
```

## 代码规范

### Python 风格

- 遵循 PEP 8
- 使用 Black 格式化（行长 88）
- 使用 Ruff 进行 linting
- 使用 mypy 进行类型检查

```bash
# 格式化代码
black zeperion tests

# 检查类型
mypy zeperion

# Linting
ruff check zeperion
```

### 提交信息

使用 Conventional Commits 格式：

```
feat: add custom agent support
fix: resolve parsing error for uppercase status
docs: update README with examples
test: add tests for prompt templates
refactor: extract common parsing logic
```

### 分支策略

- `main` - 稳定版本
- `develop` - 开发分支
- `feature/*` - 新功能
- `fix/*` - Bug 修复
- `docs/*` - 文档更新

## 测试

### 运行测试

```bash
# 所有测试
pytest

# 特定文件
pytest tests/test_agents.py

# 带覆盖率
pytest --cov=zeperion --cov-report=html

# 查看覆盖率报告
open htmlcov/index.html
```

### 编写测试

- 每个新功能必须有测试
- 测试覆盖率目标：> 80%
- 使用 pytest fixtures
- Mock 外部依赖（LLM 调用、文件 I/O）

示例：

```python
import pytest
from unittest.mock import patch

from zeperion.agents import ClaudeCodeAgent
from zeperion.models import AgentRole, GlobalStatus


@pytest.mark.asyncio
async def test_agent_invoke(mock_cli_output):
    agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

    with patch("asyncio.create_subprocess_exec") as mock_proc:
        mock_proc.return_value.communicate.return_value = (mock_cli_output, b"")
        mock_proc.return_value.returncode = 0
        result = await agent.invoke("Plan something")

    assert result.global_status == GlobalStatus.CONTINUE
```

## 添加新功能

### 1. 创建 Issue

描述：
- 功能目标
- 使用场景
- 预期行为
- 可能的实现方案

### 2. 实现功能

```bash
git checkout -b feature/your-feature-name
# 实现代码
# 编写测试
# 更新文档
```

### 3. 提交 PR

PR 描述应包含：
- 功能说明
- 变更列表
- 测试结果
- 相关 Issue

模板：

```markdown
## 功能说明
简要描述这个 PR 做了什么。

## 变更列表
- [ ] 添加了 X 功能
- [ ] 修复了 Y 问题
- [ ] 更新了 Z 文档

## 测试
- [ ] 单元测试通过
- [ ] 集成测试通过
- [ ] 手动测试通过

## 相关 Issue
Closes #123
```

## 项目结构

```
zeperion/
├── zeperion/           # 主包
│   ├── agents/         # Agent 基类与 Anthropic / Claude Code 实现
│   ├── graphs/         # LangGraph 工作流（multi_agent, pr_pipeline）
│   ├── models/         # 状态模型与 WorkflowConfig
│   ├── parsers/        # SectionParser
│   ├── prompts/        # Jinja2 模板与渲染器
│   ├── storage/        # 文件级状态持久化（按 thread_id 隔离）
│   ├── utils/          # GitHub 封装 / 时间工具
│   └── cli.py          # CLI 入口
├── tests/              # pytest 测试
├── examples/           # 示例项目
├── docs/               # 文档
├── .ai_longrun_harness/  # 旧 bash 版本（参考用，按计划逐步退役）
└── pyproject.toml      # 项目配置
```

## 添加新 Agent

1. 继承 `BaseAgent`：

```python
from typing import Optional

from zeperion.agents.base import BaseAgent
from zeperion.models import AgentOutput, AgentRole


class MyAgent(BaseAgent):
    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        raw_output = await self._call_my_llm(prompt)
        return self.parse_output(raw_output)
```

`BaseAgent.parse_output` 由所有子类共用，会按规则提取 `TASK_ID` / `TEST_STATUS` / `GLOBAL_STATUS` / `LESSONS`，并阻止 Developer 角色单方面设置 `GLOBAL_STATUS=DONE`。

2. 添加测试：

```python
@pytest.mark.asyncio
async def test_my_agent():
    agent = MyAgent()
    result = await agent.invoke(test_state)
    assert result.status == expected_status
```

3. 更新文档：

```markdown
## MyAgent

描述、用法、示例
```

## 添加新 Prompt 模板

1. 创建模板文件 `zeperion/prompts/templates/my_template.txt`：

```jinja2
你是 {{ role }} 智能体。

任务：{{ task }}

输出格式：
STATUS: {{ status }}
```

2. 添加渲染方法：

```python
def render_my_template(self, **context) -> str:
    return self.render("my_template.txt", **context)
```

3. 添加测试：

```python
def test_render_my_template():
    manager = PromptTemplate()
    prompt = manager.render_my_template(role="Test", task="Do something")
    assert "Test" in prompt
```

## 文档

### 更新文档

- README.md - 主文档
- examples/ - 示例代码
- docstrings - 代码文档

### 构建文档

```bash
# 安装文档依赖
pip install -e ".[docs]"

# 构建
cd docs
make html

# 查看
open _build/html/index.html
```

## 发布流程

1. 更新版本号（`pyproject.toml`）
2. 更新 CHANGELOG.md
3. 创建 release tag
4. 构建并发布到 PyPI

```bash
# 构建
python -m build

# 发布到 TestPyPI
python -m twine upload --repository testpypi dist/*

# 发布到 PyPI
python -m twine upload dist/*
```

## 获取帮助

- GitHub Issues: 报告 bug 或请求功能
- GitHub Discussions: 提问和讨论
- Discord: [链接]（如果有）

## 行为准则

- 尊重所有贡献者
- 建设性反馈
- 包容不同观点
- 专注于项目目标

感谢你的贡献！🎉
