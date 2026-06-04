# Agent 实现指南

ZEPERION 支持多种 LLM 提供商。本文档说明如何选择和实现自定义 Agent。

## 内置 Agent

### AnthropicAgent

**特点**：
- 直接调用 Anthropic API
- 独立运行，不依赖外部工具
- 生产环境推荐

**使用方式**：

```python
from zeperion.agents import AnthropicAgent
from zeperion.models import AgentRole

agent = AnthropicAgent(
    role=AgentRole.PLANNER,
    model="claude-opus-4-7",
    api_key="sk-ant-...",  # 或使用环境变量 ANTHROPIC_API_KEY
    max_tokens=4096,
    timeout=600,
)
```

**环境变量**：
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### ClaudeCodeAgent

**特点**：
- 通过 subprocess 调用 Claude Code CLI
- 适合在 Claude Code 环境内运行
- 开发调试推荐

**使用方式**：

```python
from zeperion.agents import ClaudeCodeAgent
from zeperion.models import AgentRole

agent = ClaudeCodeAgent(
    role=AgentRole.DEVELOPER,
    model="claude-sonnet-4-6",
    cli_tool="claude",
    timeout=600,
    permission_mode="bypassPermissions",  # 也可设成 "acceptEdits" 走人工确认
)
```

**要求**：
- 安装 `claude` CLI 工具
- 配置 Claude Code 认证

### PiAgent（推荐用于 Pi Coding Agent 工作流）

**特点**：
- 通过 subprocess 调用 Pi Coding Agent RPC 模式
- 支持 `planner_agent_type` / `developer_agent_type` / `reviewer_agent_type` / `tester_agent_type: pi`
- 适合配合 `.pi/APPEND_SYSTEM.md` 和 `.pi/skills/*` 使用

**使用方式**：

```python
from zeperion.agents import PiAgent
from zeperion.models import AgentRole

agent = PiAgent(
    role=AgentRole.DEVELOPER,
    model="gpt-5",
    cli_tool="pi",
    timeout=600,
    project_dir=".",
)
```

**要求**：
- 安装 `pi` CLI 工具
- 配置 Pi Coding Agent 认证
- 在配置中为需要真实改文件的角色设置 `*_agent_type: pi`

## 实现自定义 Agent

### 1. 继承 BaseAgent

```python
from zeperion.agents.base import BaseAgent, AgentInvocationError
from zeperion.models import AgentOutput, AgentRole
from zeperion.parsers import SectionParser

class MyCustomAgent(BaseAgent):
    def __init__(self, role: AgentRole, model: str, **kwargs):
        super().__init__(role, model)
        # 初始化你的客户端
        self.client = MyLLMClient(**kwargs)
    
    async def invoke(self, prompt: str, session_id=None) -> AgentOutput:
        """调用 LLM 并返回解析后的输出"""
        try:
            # 1. 调用你的 LLM API
            response = await self.client.generate(
                prompt=prompt,
                model=self.model,
            )
            
            # 2. 提取文本输出
            raw_output = response.text
            
            # 3. 解析输出
            return self.parse_output(raw_output)
            
        except Exception as e:
            raise AgentInvocationError(f"Failed to invoke {self.role.value}: {e}")
    
    def parse_output(self, raw_output: str) -> AgentOutput:
        """解析 LLM 输出为结构化格式"""
        return SectionParser.parse(raw_output, self.role)
```

### 2. 输出格式要求

你的 LLM 必须输出以下格式（SectionParser 会解析）：

**Planner 输出**：
```
TASK_ID: task_001
GLOBAL_STATUS: CONTINUE
PLAN:
- [P1] 子任务1
- [P2] 子任务2
RISKS:
- 风险1
LESSONS:
- 经验1
```

**Developer 输出**：
```
GLOBAL_STATUS: CONTINUE
CHANGES:
- 变更1
- 变更2
VERIFY_HINTS:
- 测试点1
BLOCKERS:
- NONE
LESSONS:
- 经验1
```

**Reviewer 输出**：
```
REVIEW_STATUS: PASS
GLOBAL_STATUS: CONTINUE
FINDINGS:
- NONE
FIX_REQUEST:
- NONE
VERIFY_HINTS:
- 测试点1
LESSONS:
- 经验1
```

**Tester 输出**：
```
TEST_STATUS: PASS
GLOBAL_STATUS: CONTINUE
TEST_CASES:
- 用例1: 通过
BUGS:
- NONE
LESSONS:
- 经验1
```

### 3. 示例实现

#### OpenAI Agent

```python
from openai import AsyncOpenAI
from zeperion.agents.base import BaseAgent, AgentInvocationError
from zeperion.models import AgentOutput, AgentRole
from zeperion.parsers import SectionParser

class OpenAIAgent(BaseAgent):
    def __init__(
        self,
        role: AgentRole,
        model: str,
        api_key: str = None,
        max_tokens: int = 4096,
        timeout: int = 600,
    ):
        super().__init__(role, model)
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.max_tokens = max_tokens
    
    async def invoke(self, prompt: str, session_id=None) -> AgentOutput:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.max_tokens,
            )
            raw_output = response.choices[0].message.content
            return self.parse_output(raw_output)
        except Exception as e:
            raise AgentInvocationError(f"OpenAI API failed: {e}")
    
    def parse_output(self, raw_output: str) -> AgentOutput:
        return SectionParser.parse(raw_output, self.role)
```

#### Ollama Agent（本地模型）

```python
import aiohttp
from zeperion.agents.base import BaseAgent, AgentInvocationError
from zeperion.models import AgentOutput, AgentRole
from zeperion.parsers import SectionParser

class OllamaAgent(BaseAgent):
    def __init__(
        self,
        role: AgentRole,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: int = 600,
    ):
        super().__init__(role, model)
        self.base_url = base_url
        self.timeout = timeout
    
    async def invoke(self, prompt: str, session_id=None) -> AgentOutput:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": prompt},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    data = await resp.json()
                    raw_output = data["response"]
                    return self.parse_output(raw_output)
        except Exception as e:
            raise AgentInvocationError(f"Ollama API failed: {e}")
    
    def parse_output(self, raw_output: str) -> AgentOutput:
        return SectionParser.parse(raw_output, self.role)
```

#### Google Gemini Agent

```python
import google.generativeai as genai
from zeperion.agents.base import BaseAgent, AgentInvocationError
from zeperion.models import AgentOutput, AgentRole
from zeperion.parsers import SectionParser

class GeminiAgent(BaseAgent):
    def __init__(
        self,
        role: AgentRole,
        model: str,
        api_key: str = None,
        timeout: int = 600,
    ):
        super().__init__(role, model)
        genai.configure(api_key=api_key)
        self.client = genai.GenerativeModel(model)
    
    async def invoke(self, prompt: str, session_id=None) -> AgentOutput:
        try:
            response = await self.client.generate_content_async(prompt)
            raw_output = response.text
            return self.parse_output(raw_output)
        except Exception as e:
            raise AgentInvocationError(f"Gemini API failed: {e}")
    
    def parse_output(self, raw_output: str) -> AgentOutput:
        return SectionParser.parse(raw_output, self.role)
```

## 在工作流中使用自定义 Agent

### 方式 1：修改 graphs/multi_agent.py

```python
from zeperion.agents.openai import OpenAIAgent  # 你的自定义 Agent

def create_multi_agent_graph(config: WorkflowConfig):
    # 使用自定义 Agent
    planner = OpenAIAgent(
        role=AgentRole.PLANNER,
        model="gpt-4",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    developer = OpenAIAgent(
        role=AgentRole.DEVELOPER,
        model="gpt-4",
    )
    tester = OpenAIAgent(
        role=AgentRole.TESTER,
        model="gpt-4",
    )
    # ... 其余代码
```

### 方式 2：通过配置文件

扩展配置加载逻辑：

```python
# zeperion/config.py
def load_agents(config: dict):
    agent_type = config.get("agent_type", "anthropic")
    
    if agent_type == "anthropic":
        from zeperion.agents import AnthropicAgent
        agent_class = AnthropicAgent
    elif agent_type == "openai":
        from zeperion.agents.openai import OpenAIAgent
        agent_class = OpenAIAgent
    elif agent_type == "ollama":
        from zeperion.agents.ollama import OllamaAgent
        agent_class = OllamaAgent
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")
    
    return agent_class
```

配置文件：
```yaml
agent_type: openai
planner_model: gpt-4
developer_model: gpt-4
tester_model: gpt-4
```

## 测试自定义 Agent

```python
import pytest
from zeperion.models import AgentRole

@pytest.mark.asyncio
async def test_custom_agent():
    agent = MyCustomAgent(
        role=AgentRole.PLANNER,
        model="my-model",
    )
    
    prompt = "测试 prompt"
    output = await agent.invoke(prompt)
    
    assert output.global_status is not None
    assert isinstance(output.lessons, list)
```

## 最佳实践

1. **错误处理**：捕获 API 异常并转换为 `AgentInvocationError`
2. **超时控制**：设置合理的超时时间
3. **日志记录**：使用 `logging` 记录调用详情
4. **重试机制**：对临时错误实现重试
5. **成本控制**：监控 token 使用量

## 常见问题

### Q: 如何混用不同的 Agent？

A: 为不同角色使用不同的 Agent 类：

```python
planner = AnthropicAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")
developer = OpenAIAgent(role=AgentRole.DEVELOPER, model="gpt-4")
tester = OllamaAgent(role=AgentRole.TESTER, model="llama3")
```

### Q: 如何处理不同的输出格式？

A: 重写 `parse_output` 方法，或在 prompt 中明确要求输出格式。

### Q: 如何优化成本？

A: 
- Planner/Tester 使用强模型（Opus, GPT-4）
- Developer 使用快速模型（Sonnet, GPT-3.5）
- 本地模型用于开发测试

## 参考

- [BaseAgent API](zeperion/agents/base.py)
- [SectionParser](zeperion/parsers/section_parser.py)
- [Prompt 模板](zeperion/prompts/templates/)
