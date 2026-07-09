# Evenly Backend Service

Evenly 是一款多人协作记账与分账应用的后端服务，提供账本（ledger）管理、成员协作、费用拆分、结算建议与语音记账等能力。

后端基于 **FastAPI** 构建，使用 **SQLAlchemy 2.0** + **Alembic** 管理 PostgreSQL 数据模型，以 **Redis** 存储验证码与限流，并集成了 Apple 登录、腾讯云 COS、邮件与 OpenAI 语音等服务。

## 技术栈

| 类别 | 选型 |
| --- | --- |
| 语言 / 运行时 | Python 3.12+ |
| Web 框架 | FastAPI + Uvicorn |
| ORM / 迁移 | SQLAlchemy 2.0、Alembic |
| 数据库 | PostgreSQL 16 |
| 缓存 | Redis 7（验证码、限流；未配置时回退到内存） |
| 认证 | JWT（HttpOnly Cookie），邮箱验证码、Apple Sign In |
| 文件存储 | 腾讯云 COS（头像等） |
| 邮件 | SMTP（兼容阿里云 DirectMail） |
| 语音记账 | OpenAI（语音转写 + 结构化草稿） |
| 依赖管理 | uv |
| 容器化 | Docker（多架构镜像，推送至阿里云 ACR） |

## 项目结构

```
.
├── main.py                # FastAPI 入口，中间件、异常处理、健康检查
├── app/
│   ├── config.py          # 基于 YAML + 环境变量的分层配置
│   ├── database.py        # SQLAlchemy engine / session
│   ├── models/            # ORM 模型：user, ledger, expense, settlement
│   ├── schemas/           # Pydantic 请求 / 响应模型
│   ├── routers/           # API 路由：auth, users, ledgers, expenses, settlements
│   ├── services/          # 业务服务：auth, apple_auth, cos, email,
│   │                      #   verification, settlement, voice_expense
│   └── utils/deps.py      # 依赖注入（当前用户、DB 会话等）
├── alembic/               # 数据库迁移
├── tests/                 # pytest 测试
├── .github/workflows/     # CI（测试 + 迁移校验 + 密钥扫描）与部署
├── Dockerfile             # 生产镜像
├── docker-compose.yml     # 本地 PostgreSQL + Redis
└── Makefile               # 常用命令封装
```

## 核心功能

- **账本与成员**：创建账本、邀请成员（支持临时成员 / 邀请确认）、移交与解散。
- **费用拆分**：记录费用、自定义分摊比例，支持 `pending / confirmed / rejected` 确认流程。
- **结算建议**：基于贪心算法计算最少转账次数的结算方案，并记录实际还款历史。
- **多方式认证**：邮箱 + 验证码注册 / 密码登录、Apple Sign In，JWT 通过 Cookie 下发。
- **语音记账**：上传语音，由 OpenAI 转写并生成费用草稿，待用户确认入库。
- **文件上传**：头像等资源经腾讯云 COS 托管。

## 本地开发

需要先安装 [uv](https://docs.astral.sh/uv/) 并启动 Docker Desktop。

```bash
# 1. 安装依赖
uv sync --group dev

# 2. 启动 PostgreSQL + Redis
make dev-db-up

# 3. 准备本地配置（Git 忽略，主运行时配置）
cp config/config.yaml.example config/config.yaml   # 如有示例；否则手动创建
#    最小配置示例见下方“配置”一节

# 4. 执行数据库迁移
make db-upgrade

# 5. 启动 API（热重载）
make dev-api
```

API 监听 `http://localhost:8000`，交互式文档见 `http://localhost:8000/docs`。

如业务接口出现 `500`，可先用 `make doctor` 检查数据库连接；端口被占用时用
`lsof -nP -iTCP:8000 -sTCP:LISTEN` 排查。

## 配置

服务优先读取 `config/config.yaml`（被 `.gitignore` 忽略），同时支持通过环境变量覆盖，
嵌套字段使用 `__` 分隔（如 `DB__HOST`），顶层字段使用别名（如 `DATABASE_URL`、
`REDIS_URL`、`OPENAI_API_KEY`）。

最小本地配置示例：

```yaml
db:
  host: localhost
  port: 5432
  database: evenly
  user: postgres
  password: postgres

redis_url: redis://localhost:6379/0

cors:
  allow_origins:
    - http://localhost:5173
```

可选服务按需启用：

- **Redis**：未配置 `redis_url` 时，验证码回退到内存存储（仅本地可用）。
- **语音记账**：设置 `OPENAI_API_KEY`，可用 `OPENAI_TRANSCRIPTION_MODEL` /
  `OPENAI_TEXT_MODEL` 覆盖默认模型。
- **COS / SMTP**：在 YAML 中补充 `cos` / `smtp` 段即可启用头像上传与邮件发送。
- **生产部署**：务必覆盖 `jwt_secret_key`，并将 `auth_cookie_secure` 设为 `true`。

## API 概览

| 路由前缀 | 说明 |
| --- | --- |
| `/auth` | 验证码发送 / 校验、注册、登录、Apple 登录、登出、密码重置 |
| `/users` | 当前用户信息、认证方式、用户名 / 头像 / 邮箱 / 密码管理、用户搜索、注销 |
| `/ledgers` | 账本 CRUD、成员管理、邀请接受 / 拒绝、账本概览 |
| `/expenses` | 语音草稿、费用创建 / 查询 / 确认 / 拒绝 / 删除 |
| `/ledgers/{id}/settlements` | 结算建议、历史记录、登记还款 |

健康检查：

- `GET /` — 欢迎信息
- `GET /health` — 存活检查
- `GET /ready` — 就绪检查（含数据库连通性）

## 数据库迁移

表结构变更统一通过 Alembic 管理，应用启动时不再自动建表。

```bash
make db-upgrade                 # 应用到最新
make db-downgrade               # 回退一个版本
make db-revision m="描述变更"    # 生成新迁移
```

对于由旧的 `create_all` 创建的既有库，先核对 schema 后用
`uv run python -m alembic stamp head` 标记为已管理，再进行后续迁移。
生产环境执行迁移前务必备份数据。详见 [`DATABASE.md`](DATABASE.md)。

## 测试

```bash
uv run --group dev python -m pytest -q
```

CI（`.github/workflows/ci.yml`）会运行 pytest、校验 Alembic 迁移可生成 SQL，
并通过 gitleaks 进行密钥扫描。

## 容器化与部署

```bash
make image-build-local     # 本地构建
make image-build-push      # 多架构构建并推送至阿里云 ACR
make info                  # 查看镜像名 / 标签
```

镜像标签取自 `.version` 文件。推送到 `main` 分支会触发
`.github/workflows/deploy.yml` 自动部署。

## 相关文档

- [开发指南](DEVELOPMENT.md)
- [数据库迁移](DATABASE.md)
