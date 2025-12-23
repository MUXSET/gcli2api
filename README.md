# GCLI2API

<div align="center">

**将 Google Gemini CLI 凭证转换为 OpenAI 兼容 API 的代理服务**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC](https://img.shields.io/badge/License-CNC%201.0-green.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)

</div>

---

## ✨ 功能特性

- 🔄 **OpenAI 兼容 API** - 完全兼容 OpenAI Chat Completions API 格式
- 🌐 **Gemini 原生 API** - 同时支持 Google Gemini 原生 API 格式
- 🔐 **多用户系统** - 完整的用户注册、登录和权限管理 (RBAC)
- 📦 **凭证管理** - 支持多凭证轮换、自动刷新和状态监控
- 🛡️ **凭证隔离** - 可选的用户凭证隔离模式，确保资源独立
- 📊 **使用统计** - 详细的 API 调用统计和用量追踪
- 🔁 **自动重试** - 智能处理 429 速率限制错误
- 🌊 **流式传输** - 支持真流式和假流式传输模式
- 🧠 **思考模式** - 支持 Gemini 2.5 系列的思考 (Thinking) 功能
- 🔍 **Google 搜索** - 内置 Google 搜索集成
- 🛠️ **Function Calling** - 完整支持工具调用功能
- 💾 **多存储后端** - 支持 Redis、PostgreSQL、MySQL、MongoDB 和本地文件存储

---

## 🚀 快速开始

### 环境要求

- Python 3.12+
- Google Cloud 项目凭证 (OAuth 2.0 或 Service Account)

### 安装步骤

#### 1. 克隆仓库

```bash
git clone https://github.com/MUXSET/gcli2api.git
cd gcli2api
```

#### 2. 安装依赖

**Windows (PowerShell):**
```powershell
.\install.ps1
```

**Linux/macOS:**
```bash
chmod +x install.sh
./install.sh
```

**macOS (Darwin):**
```bash
chmod +x darwin-install.sh
./darwin-install.sh
```

**Termux (Android):**
```bash
chmod +x termux-install.sh
./termux-install.sh
```

#### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，配置必要的参数
```

#### 4. 添加凭证

将 Google OAuth 凭证 JSON 文件放入 `creds/` 目录，或通过控制面板上传。

#### 5. 启动服务

**Windows:**
```powershell
.\start.bat
```

**Linux/macOS:**
```bash
./start.sh
```

**或直接运行:**
```bash
python web.py
```

服务启动后访问: `http://127.0.0.1:7861`

---

## 📖 使用方法

### API 端点

| 端点 | 描述 |
|------|------|
| `/v1/chat/completions` | OpenAI 兼容的聊天补全 API |
| `/v1/models` | 获取可用模型列表 |
| `/v1beta/models/{model}:generateContent` | Gemini 原生 API |
| `/v1beta/models/{model}:streamGenerateContent` | Gemini 流式 API |

### 认证方式

使用个人中心生成的 API Key 进行认证：

```bash
curl -X POST http://127.0.0.1:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gcli-your-api-key" \
  -d '{
    "model": "gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 支持的模型

| 模型名称 | 描述 |
|----------|------|
| `gemini-2.5-pro` | Gemini 2.5 Pro 基础版 |
| `gemini-2.5-flash` | Gemini 2.5 Flash 快速版 |
| `gemini-3-pro-preview` | Gemini 3 Pro 预览版 |
| `gemini-3-flash-preview` | Gemini 3 Flash 预览版 |
| `gemini-2.5-pro-maxthinking` | 最大思考预算 |
| `gemini-2.5-pro-nothinking` | 禁用思考模式 |
| `gemini-2.5-pro-search` | 启用 Google 搜索 |

### 模型前缀

| 前缀 | 功能 |
|------|------|
| `假流式/` | 使用假流式传输 (非流式请求转流式输出) |
| `流式抗截断/` | 启用流式抗截断功能 |

**示例:** `假流式/gemini-2.5-pro-maxthinking`

---

## ⚙️ 配置说明

### 环境变量

主要配置项：

| 变量名 | 默认值 | 描述 |
|--------|--------|------|
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `7861` | 服务监听端口 |
| `PROXY` | - | 代理服务器地址 |
| `LOG_LEVEL` | `info` | 日志级别 |
| `GOOGLE_OAUTH_CLIENT_ID` | (内置) | 自定义 OAuth Client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | (内置) | 自定义 OAuth Client Secret |

> **提示**: 想要使用自己的 Google Cloud 项目进行认证？请查看 [OAUTH_SETUP.md](OAUTH_SETUP.md) 获取详细指南。

### 💾 存储配置与异步支持

系统采用 **全异步 (Full Async)** 架构设计，支持高并发处理。

#### 1. 业务数据 (凭证/配置/状态)
由 `StorageAdapter` 管理，支持以下后端 (按优先级自动检测):

1. 🔴 **Redis** (推荐, Async) - 设置 `REDIS_URI` (高性能缓存)
2. 🐬 **MySQL** (Async) - 设置 `MYSQL_URI`
3. 🐘 **PostgreSQL** (Async) - 设置 `POSTGRES_DSN`
4. 🍃 **MongoDB** (Async) - 设置 `MONGODB_URI`
5. 📁 **本地文件** (默认, Zero-Config) - 无需任何配置，开箱即用

#### 2. 用户数据 (账号/Token/权限)
由 `UserManager` 管理，目前支持：

- 🐬 **MySQL** (推荐, Async) - 设置 `MYSQL_URI` (与业务数据共用)
- 📁 **SQLite** (默认, ThreadPool) - 自动降级，使用线程池模拟异步，不阻塞主线程

详细配置请参考 [.env.example](.env.example)

---

## 👥 多用户系统

GCLI2API 支持完整的多用户管理和权限控制。

### 角色说明

| 角色 | 权限 |
|------|------|
| **管理员 (Admin)** | 完全控制权：用户管理、全局配置、查看所有凭证 |
| **普通用户 (User)** | 受限访问：个人凭证管理、API 调用 |

### 默认管理员账户

- 用户名: `admin`
- 初始密码: `admin`

> ⚠️ **安全提示**: 首次登录后请立即修改默认密码！

### 凭证隔离模式

- **开启隔离**: 用户只能使用自己上传的凭证
- **关闭隔离** (默认): 所有用户共享凭证池

详细说明请参考 [MULTI_USER_README.md](MULTI_USER_README.md)

---

## 🐳 Docker 部署

### 使用 Docker Compose

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

### 单独构建

```bash
docker build -t gcli2api .
docker run -d -p 7861:7861 -v ./creds:/app/creds gcli2api
```

---

## 📁 项目结构

```
gcli2api/
├── web.py                 # 主入口文件
├── config.py              # 配置管理
├── log.py                 # 日志系统
├── src/
│   ├── routers/           # API 路由模块 (Auth, Admin, Gemini, OpenAI等)
│   ├── services/          # 业务逻辑服务 (AuthService, GeminiService等)
│   ├── schemas/           # Pydantic 数据模型
│   ├── credential_manager.py  # 凭证管理
│   ├── user_manager.py    # 用户管理
│   ├── dependencies.py    # FastAPI 依赖
│   ├── utils.py           # 通用工具
│   ├── usage_stats.py     # 使用统计
│   └── storage/           # 存储适配器
├── front/
│   ├── control_panel.html # 控制面板
│   └── admin_panel.html   # 管理后台
├── creds/                 # 凭证存储目录
├── docs/                  # 文档资源
└── tests/                 # 测试文件
```

---

## 🔧 开发

### 开发环境设置

```bash
# 安装开发依赖
pip install -r requirements-dev.txt

# 运行测试
pytest tests/

# 代码格式检查
flake8 .
```

### 贡献指南

请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 📋 常见问题

### Q: 如何获取 Google 凭证？

1. 访问 [Google Cloud Console](https://console.cloud.google.com/)
2. 创建项目并启用 Gemini API
3. 创建 OAuth 2.0 凭证或服务账号
4. 下载 JSON 凭证文件

### Q: 出现 429 错误怎么办？

429 表示请求过于频繁。系统会自动重试，你也可以：
- 增加凭证数量进行轮换
- 调整 `RETRY_429_MAX_RETRIES` 参数

### Q: 如何启用思考模式？

在模型名称后添加 `-maxthinking` 后缀，例如：
```
gemini-2.5-pro-maxthinking
```

---

## 📄 许可证

本项目采用 [Cooperative Non-Commercial License (CNC-1.0)](LICENSE) 许可证。

**主要限制：**
- ✅ 允许个人和教育用途
- ✅ 允许非营利组织使用
- ❌ 禁止商业用途
- ❌ 禁止年收入超过 100 万美元的公司使用

---

## 🙏 致谢

感谢所有贡献者和社区成员的支持！

---

<div align="center">

**⭐ 如果这个项目对您有帮助，请点个 Star！**

</div>
