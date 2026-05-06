from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """Spring Boot 转发过来的聊天请求。"""

    user_id: int | None = None
    conversation_id: int | None = None
    message: str
    channel: str = "WEB"
    baby_age_month: int | None = None
    metadata: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    """返回给 Spring Boot 的聊天响应。"""

    model_config = ConfigDict(populate_by_name=True)

    conversation_id: int | None = Field(default=None, alias="conversationId")
    trace_id: str = Field(alias="traceId")
    answer: str
    intent: str
    risk_level: str = Field(alias="riskLevel")
    human_handoff_required: bool = Field(default=False, alias="humanHandoffRequired")
    ticket_id: int | None = Field(default=None, alias="ticketId")
    suggestions: list[str] = Field(default_factory=list)
    tool_results: dict[str, Any] = Field(default_factory=dict, alias="toolResults")


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
