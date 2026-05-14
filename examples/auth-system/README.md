# 示例：用户认证系统

这是一个使用 ZEPERION 开发用户认证系统的完整示例。

## 需求

实现一个用户认证系统，包括：
1. 用户注册（邮箱 + 密码）
2. 用户登录（JWT token）
3. 密码加密存储（bcrypt）
4. 登录失败限流（5次/分钟）

## 步骤

### 1. 初始化项目

```bash
mkdir auth-system
cd auth-system
zeperion init
```

### 2. 编写需求文件

编辑 `requirement.txt`：

```
实现一个用户认证系统，包括：

功能需求：
1. 用户注册
   - 接受邮箱和密码
   - 邮箱格式验证
   - 密码强度检查（至少8位，包含大小写字母和数字）
   - 密码使用 bcrypt 加密存储
   - 返回用户 ID

2. 用户登录
   - 接受邮箱和密码
   - 验证凭据
   - 生成 JWT token（有效期 24 小时）
   - 返回 token 和用户信息

3. 登录限流
   - 同一 IP 最多 5 次失败尝试/分钟
   - 超过限制返回 429 错误
   - 成功登录后重置计数

技术栈：
- Python 3.11+
- FastAPI
- SQLAlchemy
- bcrypt
- PyJWT
- Redis（限流）

验收标准：
- 所有 API 有单元测试
- 密码不以明文存储
- JWT token 可验证
- 限流机制生效
```

### 3. 配置工作流

编辑 `.zeperion/config.yaml`：

```yaml
models:
  planner: claude-opus-4-7
  developer: claude-sonnet-4-6
  tester: claude-opus-4-7

workflow:
  max_rounds: 30
  max_fix_attempts: 3

cli:
  command: claude
  model_flag: --model
  resume_flag: --resume
  timeout: 600
```

### 4. 运行工作流

```bash
zeperion run
```

## 预期输出

### Round 1: Planning

Planner 会输出：

```
TASK_ID: auth_system_v1
GLOBAL_STATUS: CONTINUE

PLAN:
- [P1] 搭建项目结构（FastAPI + SQLAlchemy）
- [P2] 实现用户模型和数据库表
- [P3] 实现密码加密工具（bcrypt）
- [P4] 实现 JWT token 生成和验证
- [P5] 实现注册 API
- [P6] 实现登录 API
- [P7] 实现 Redis 限流中间件
- [P8] 编写单元测试

RISKS:
- bcrypt 性能可能影响响应时间
- Redis 连接失败需要降级方案

HANDOFF_TO_DEVELOPER:
- 本轮先实现 P1-P4（基础设施）
- 创建 src/auth/ 目录结构
- 配置数据库连接
```

### Round 2: Development

Developer 会创建：

```
src/
├── auth/
│   ├── __init__.py
│   ├── models.py       # User 模型
│   ├── schemas.py      # Pydantic schemas
│   ├── security.py     # 密码加密、JWT
│   ├── database.py     # 数据库配置
│   └── main.py         # FastAPI app
├── tests/
│   ├── test_security.py
│   └── test_models.py
└── requirements.txt
```

### Round 3: Testing

Tester 会验证：

```
TEST_STATUS: PASS

TEST_CASES:
- 密码加密：PASS（bcrypt 正确使用）
- JWT 生成：PASS（token 可验证）
- 数据库模型：PASS（表结构正确）

BUGS: NONE
```

### Round 4-6: API 实现

继续实现注册、登录 API 和限流功能。

### Final Round: 完成

```
GLOBAL_STATUS: DONE

所有功能已实现并通过测试：
✅ 用户注册 API
✅ 用户登录 API
✅ 密码 bcrypt 加密
✅ JWT token 认证
✅ Redis 限流
✅ 单元测试覆盖率 > 90%
```

## 查看结果

```bash
# 查看生成的代码
ls -la src/auth/

# 运行测试
pytest tests/

# 启动服务
uvicorn src.auth.main:app --reload

# 测试 API
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "SecurePass123"}'
```

## 中断恢复

如果工作流中断：

```bash
# 查看运行历史
zeperion status

# 恢复特定运行
zeperion run --resume <run_id>
```

## 自定义调整

### 修改需求

编辑 `requirement.txt` 添加新需求：

```
4. 添加邮箱验证
   - 注册后发送验证邮件
   - 验证链接 24 小时有效
   - 未验证用户不能登录
```

然后重新运行：

```bash
zeperion run
```

### 调整模型

如果 Developer 太慢，可以降级：

```yaml
models:
  developer: claude-haiku-4-5  # 更快但能力稍弱
```

## 经验总结

从这个示例中学到的经验（会自动记录到 `lessons_learned.txt`）：

```
- bcrypt 的 cost factor 设为 12 平衡安全和性能
- JWT secret 必须从环境变量读取，不能硬编码
- Redis 连接失败时应降级到内存限流
- 单元测试应 mock 外部依赖（数据库、Redis）
- FastAPI 的依赖注入适合实现认证中间件
```

## 下一步

- 添加 OAuth2 登录（Google、GitHub）
- 实现双因素认证（2FA）
- 添加用户权限系统（RBAC）
- 部署到生产环境

## 完整代码

完整的示例代码可在 `examples/auth-system/` 目录中找到。
