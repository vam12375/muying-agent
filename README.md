# Muying AI Agent

母婴电商 AI Agent 服务，基于 FastAPI 构建，用于承接 `muying-mall` Spring Boot 电商平台转发过来的智能导购、订单查询、售后判断、育儿知识问答和高风险问题转人工流程。

本服务定位为“业务流程自动化 Agent”，不是单纯聊天机器人。它只负责意图识别、流程编排、工具调用和可选大模型润色；商品、订单、售后、工单、日志等核心业务数据仍由 `muying-mall` 统一管理，避免 Python 服务绕过电商主系统直接改业务数据。

## 核心能力

- **AI 导购**：根据用户消息、宝宝月龄、关键词等信息检索商品和育儿知识，返回推荐理由。
- **订单查询**：识别订单 ID 或订单号，调用 Spring Boot 工具接口查询订单、物流和发货状态。
- **售后判断**：根据订单状态和售后规则评估是否可退款、是否需要人工审核。
- **知识问答**：检索母婴育儿知识库，回答护理、月龄、喂养等低风险问题。
- **投诉转人工**：识别过敏、质量、变质、医疗相关等高风险问题，自动创建客服工单。
- **调用追踪**：每次工具调用都会写回 Spring Boot，便于后台查看 trace、成功率、耗时和失败原因。

## 架构关系

推荐调用链路如下：

```text
muying-web / muying-admin
        |
        v
muying-mall: POST /api/ai/chat
        |
        v
muying-agent: POST /api/v1/chat
        |
        v
muying-mall: /api/ai/tools/*
```

职责边界：

- `muying-web`：用户侧聊天、导购、售后入口。
- `muying-admin`：管理端查看 Agent 日志、工单和人工处理结果。
- `muying-mall`：负责鉴权、业务数据、工具接口、工单和审计日志。
- `muying-agent`：负责 Agent 编排、风险识别、工具调用和回答生成。

## 目录结构

```text
muying-agent/
├── app/
│   ├── main.py      # FastAPI 入口，提供健康检查和聊天接口
│   ├── agent.py     # Agent 主流程编排，按意图调用不同业务工具
│   ├── intent.py    # 轻量规则意图识别和风险等级判断
│   ├── tools.py     # Spring Boot 工具接口客户端，并写回工具调用日志
│   ├── llm.py       # 可选大模型润色客户端，没有密钥时自动跳过
│   ├── schemas.py   # 请求、响应和工具调用日志模型
│   └── config.py    # 环境变量配置
├── .env.example     # 本地环境变量示例
├── requirements.txt # Python 依赖
└── README.md
```

## 环境要求

- Python 3.10+
- 已启动 `muying-mall`，默认地址为 `http://localhost:8080/api`
- `muying-mall` 已配置 AI Agent 代理地址，默认 `ai.agent.base-url=http://localhost:8001`

## 快速启动

在 Windows PowerShell 中执行：

```powershell
cd G:\muying\muying-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8001
```

启动成功后访问健康检查：

```powershell
Invoke-RestMethod -Uri http://localhost:8001/health
```

预期返回：

```json
{
  "status": "ok"
}
```

## 环境变量

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `MUYING_AGENT_SPRING_BASE_URL` | `http://localhost:8080/api` | Spring Boot 电商平台 API 根地址 |
| `MUYING_AGENT_REQUEST_TIMEOUT_SECONDS` | `20` | 调用 Spring Boot 和大模型接口的超时时间 |
| `MUYING_AGENT_ENABLE_LLM` | `false` | 是否启用大模型润色 |
| `MUYING_AGENT_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `MUYING_AGENT_OPENAI_API_KEY` | 空 | 大模型接口密钥 |
| `MUYING_AGENT_OPENAI_MODEL` | `gpt-4o-mini` | 大模型名称 |

说明：第一版 Agent 即使不配置大模型也可以运行。关闭 `MUYING_AGENT_ENABLE_LLM` 时，服务会直接使用业务工具返回的结构化结果生成回答，更适合本地演示和调试。

## 接口说明

### 健康检查

```http
GET /health
```

### Agent 聊天

```http
POST /api/v1/chat
```

请求示例：

```json
{
  "user_id": 1,
  "conversation_id": 10001,
  "message": "宝宝8个月，容易红屁屁，推荐纸尿裤和护臀膏",
  "channel": "WEB",
  "baby_age_month": 8,
  "metadata": {
    "source": "web-chat"
  }
}
```

响应示例：

```json
{
  "conversationId": 10001,
  "traceId": "c4f5b4f0a37d49b98e24f37f2d47e9a1",
  "answer": "根据你的需求，我优先筛选了这些商品...",
  "intent": "SHOPPING_GUIDE",
  "riskLevel": "LOW",
  "humanHandoffRequired": false,
  "ticketId": null,
  "suggestions": ["查看商品详情", "加入购物车", "继续按预算筛选"],
  "toolResults": {
    "products": [],
    "knowledge": []
  }
}
```

本接口会透传 `Authorization` 请求头给 `muying-mall`，用于查询用户订单、售后和工单等需要登录态的数据。实际业务联调时，推荐从 `muying-mall` 的 `POST /api/ai/chat` 入口调用，不建议前端绕过 Java 后端直接请求 Python 服务。

## Spring Boot 工具接口

`muying-agent` 当前会调用以下 `muying-mall` 工具接口：

| 工具名 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| `searchProducts` | `POST` | `/ai/tools/products/search` | 搜索商品 |
| `searchKnowledgeBase` | `GET` | `/ai/tools/knowledge/search` | 检索育儿知识库 |
| `getOrderStatus` | `POST` | `/ai/tools/orders/status` | 查询订单状态 |
| `evaluateRefund` | `POST` | `/ai/tools/refunds/evaluate` | 判断售后和退款规则 |
| `createSupportTicket` | `POST` | `/ai/tools/tickets` | 创建客服工单 |
| `recordToolCall` | `POST` | `/ai/tools/trace/tool-call` | 写入工具调用日志 |

工具调用失败时，Agent 会返回可降级的回答，并建议用户稍后重试或联系人工客服。工具日志写入失败不会阻断主流程，避免监控链路影响用户体验。

## 意图与风险规则

当前版本采用轻量规则分类，便于 MVP 演示和问题排查。

| 意图 | 触发场景 | 默认风险 |
| --- | --- | --- |
| `SHOPPING_GUIDE` | 推荐、购买、纸尿裤、奶瓶、护臀膏等 | `LOW` |
| `ORDER_QUERY` | 订单、物流、发货、快递、单号等 | `LOW` |
| `REFUND_CHECK` | 退款、退货、换货、售后、取消订单等 | `MEDIUM` |
| `KNOWLEDGE_QA` | 月龄、护理、喂养、红屁屁、湿疹等 | `LOW` |
| `COMPLAINT_HANDOFF` | 投诉、质量、过敏、变质、假货、医生、医院等 | `HIGH` |

安全边界：

- Agent 不直接执行退款、发券、取消订单等高风险动作。
- 高风险投诉、质量、过敏、医疗相关问题必须创建工单并进入人工处理。
- 医疗和用药相关内容只做风险提示，不给诊断结论。
- 所有业务工具调用都写入 trace，方便审计和复盘。

## 本地联调建议

1. 先启动 MySQL、Redis 和 `muying-mall`。
2. 确认 `muying-mall` 的 AI 数据表已初始化。
3. 启动 `muying-agent`，确认 `/health` 返回 `ok`。
4. 登录前端或管理端，通过 `muying-mall` 的 `POST /api/ai/chat` 发起对话。
5. 在管理端查看 Agent 会话、工具调用日志和工单流转。

## 常见问题

### 1. 返回 401 或没有订单数据

通常是没有从前端或 `muying-mall` 透传登录态。订单、售后、工单等接口需要用户身份，建议从 `POST /api/ai/chat` 统一入口联调。

### 2. 商品推荐为空

优先检查 `muying-mall` 商品数据、商品上下架状态、库存和 `/ai/tools/products/search` 接口是否正常。

### 3. Agent 回答没有大模型效果

默认没有启用 LLM。需要在 `.env` 中设置：

```env
MUYING_AGENT_ENABLE_LLM=true
MUYING_AGENT_OPENAI_API_KEY=你的密钥
```

### 4. 提示 Spring Boot 工具调用失败

检查 `MUYING_AGENT_SPRING_BASE_URL` 是否与 `muying-mall` 实际启动地址一致。默认要求 Spring Boot API 根路径为 `http://localhost:8080/api`。

## 后续可迭代方向

- 将规则意图识别替换为模型分类或结构化输出。
- 增加向量检索，提升育儿知识库和售后规则命中率。
- 在管理端增加 Agent 评测集、满意度、人工接管率和单次成本统计。
- 为高风险动作加入更完整的人工审批流和权限校验。
