from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 允许的渠道集合：muying-mall 目前会传 WEB / MINI_PROGRAM / ADMIN，
# 用 Literal 约束防止上游误传未识别的来源穿透到下游
AllowedChannel = Literal["WEB", "MINI_PROGRAM", "ADMIN", "API"]


class ChatHistoryMessage(BaseModel):
    """同一会话内已裁剪的历史消息。"""

    model_config = ConfigDict(populate_by_name=True)

    id: int | None = None
    role: str
    # 历史消息的 content 由上游 mall 已校验过；此处仅做最大上限保护，
    # 防止单条历史超长把整个 prompt 吃满
    content: str = Field(max_length=8000)
    intent: str | None = None
    # 这些字段都有 default，Pylance 不会报缺参；保留 alias 兼容上游驼峰输入
    risk_level: str | None = Field(default=None, alias="riskLevel")
    create_time: str | None = Field(default=None, alias="createTime")


class ChatRequest(BaseModel):
    """Spring Boot 转发过来的聊天请求。

    所有用户可控字段在此层做边界校验，避免在 agent / tools / llm 各层重复防御。
    """

    user_id: int | None = Field(default=None, ge=1)
    conversation_id: int | None = Field(default=None, ge=1)
    # message 是用户原始输入，最容易被滥用：
    # - min_length=1：禁止空字符串触发空 LLM 调用
    # - max_length=4000：约 1k tokens 的安全上限，防止打爆 prompt 与超时
    message: str = Field(min_length=1, max_length=4000)
    channel: AllowedChannel = "WEB"
    # 月龄上限按中国母婴主流业务到 6 岁（72 月）
    baby_age_month: int | None = Field(default=None, ge=0, le=72)
    metadata: dict[str, Any] | None = None
    # 历史最多 100 条；超过则上游 mall 应该已经做过裁剪
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=100)
    # 字符上限默认 256k，下限 1k 防止误传 0 把 LLM 上下文清空
    max_context_chars: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)


class ChatResponse(BaseModel):
    """返回给 Spring Boot 的聊天响应。

    说明：trace_id / risk_level 是必填字段。
    使用 serialization_alias（而非 alias），让构造参数名保持 Python 风格 `trace_id/risk_level`，
    避免 Pylance reportCallIssue（alias= 会让静态类型把参数名暴露为 traceId/riskLevel）。
    序列化（model_dump(by_alias=True)）仍输出 camelCase。
    """

    model_config = ConfigDict(populate_by_name=True)

    conversation_id: int | None = Field(default=None, serialization_alias="conversationId")
    trace_id: str = Field(serialization_alias="traceId")
    answer: str
    intent: str
    risk_level: str = Field(serialization_alias="riskLevel")
    human_handoff_required: bool = Field(default=False, serialization_alias="humanHandoffRequired")
    ticket_id: int | None = Field(default=None, serialization_alias="ticketId")
    suggestions: list[str] = Field(default_factory=list)
    tool_results: dict[str, Any] = Field(default_factory=dict, serialization_alias="toolResults")
    # 输出轻量工作流状态，方便后台统计节点耗时/失败点；字段保持向后兼容，旧前端可忽略。
    workflow: dict[str, Any] = Field(default_factory=dict)


class ToolCallLog(BaseModel):
    """写回 Spring Boot 的工具调用日志。"""

    traceId: str
    conversationId: int | None = None
    intent: str | None = None
    riskLevel: str = "LOW"
    toolName: str
    toolType: str = "BUSINESS_API"
    requestPayload: Any | None = None
    responsePayload: Any | None = None
    success: bool = True
    errorMessage: str | None = None
    durationMs: int | None = None


class BaseToolParams(BaseModel):
    """业务工具参数基类。

    工具入口先走 Pydantic 强校验，避免非法参数穿透到 Spring Boot。
    """

    trace_id: str = Field(min_length=1, max_length=64)
    conversation_id: int | None = Field(default=None, ge=1)
    intent: str = Field(min_length=1, max_length=64)
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"


class ProductSearchToolParams(BaseToolParams):
    keyword: str = Field(min_length=1, max_length=120)
    baby_age_month: int | None = Field(default=None, ge=0, le=72)
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, ge=0)
    limit: int = Field(default=6, ge=1, le=20)


class KnowledgeSearchToolParams(BaseToolParams):
    keyword: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=5, ge=1, le=10)


class OrderStatusToolParams(BaseToolParams):
    order_id: int | None = Field(default=None, ge=1)
    order_no: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def require_order_identifier(self) -> "OrderStatusToolParams":
        if self.order_id is None and not self.order_no:
            raise ValueError("订单查询工具必须提供 order_id 或 order_no")
        return self


class RefundEvaluateToolParams(OrderStatusToolParams):
    reason: str = Field(min_length=1, max_length=4000)


class CreateTicketToolParams(BaseToolParams):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    order_id: int | None = Field(default=None, ge=1)
    product_id: int | None = Field(default=None, ge=1)
