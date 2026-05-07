from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatHistoryMessage(BaseModel):
    """同一会话内已裁剪的历史消息。"""

    model_config = ConfigDict(populate_by_name=True)

    id: int | None = None
    role: str
    content: str
    intent: str | None = None
    # 这些字段都有 default，Pylance 不会报缺参；保留 alias 兼容上游驼峰输入
    risk_level: str | None = Field(default=None, alias="riskLevel")
    create_time: str | None = Field(default=None, alias="createTime")


class ChatRequest(BaseModel):
    """Spring Boot 转发过来的聊天请求。"""

    user_id: int | None = None
    conversation_id: int | None = None
    message: str
    channel: str = "WEB"
    baby_age_month: int | None = None
    metadata: dict[str, Any] | None = None
    history: list[ChatHistoryMessage] = Field(default_factory=list)
    max_context_chars: int = 256 * 1024


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
