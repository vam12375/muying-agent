import time
from typing import Any

import httpx

from app.config import settings
from app.schemas import ToolCallLog


class SpringToolClient:
    """调用 Spring Boot 电商业务工具的客户端。"""

    def __init__(self, authorization: str | None):
        self.authorization = authorization

    async def search_products(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        keyword: str,
        baby_age_month: int | None,
        limit: int = 6,
    ) -> dict[str, Any]:
        payload = {
            "keyword": keyword,
            "babyAgeMonth": baby_age_month,
            "limit": limit,
        }
        return await self._call_tool(
            "searchProducts",
            "POST",
            "/ai/tools/products/search",
            trace_id,
            conversation_id,
            intent,
            risk_level,
            json=payload,
        )

    async def search_knowledge(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        keyword: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        data = await self._call_tool(
            "searchKnowledgeBase",
            "GET",
            "/ai/tools/knowledge/search",
            trace_id,
            conversation_id,
            intent,
            risk_level,
            params={"keyword": keyword, "limit": limit},
        )
        return data or []

    async def get_order_status(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        order_id: int | None,
        order_no: str | None,
    ) -> dict[str, Any]:
        payload = {"orderId": order_id, "orderNo": order_no}
        return await self._call_tool(
            "getOrderStatus",
            "POST",
            "/ai/tools/orders/status",
            trace_id,
            conversation_id,
            intent,
            risk_level,
            json=payload,
        )

    async def evaluate_refund(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        order_id: int | None,
        order_no: str | None,
        reason: str,
    ) -> dict[str, Any]:
        payload = {"orderId": order_id, "orderNo": order_no, "reason": reason}
        return await self._call_tool(
            "evaluateRefund",
            "POST",
            "/ai/tools/refunds/evaluate",
            trace_id,
            conversation_id,
            intent,
            risk_level,
            json=payload,
        )

    async def create_ticket(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        title: str,
        content: str,
        order_id: int | None = None,
        product_id: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "conversationId": conversation_id,
            "orderId": order_id,
            "productId": product_id,
            "title": title,
            "content": content,
            "intent": intent,
            "riskLevel": risk_level,
        }
        return await self._call_tool(
            "createSupportTicket",
            "POST",
            "/ai/tools/tickets",
            trace_id,
            conversation_id,
            intent,
            risk_level,
            json=payload,
        )

    async def _call_tool(
        self,
        tool_name: str,
        method: str,
        path: str,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        **kwargs: Any,
    ) -> Any:
        started = time.perf_counter()
        request_payload = kwargs.get("json") or kwargs.get("params")
        success = True
        error_message = None
        response_payload: Any = None

        try:
            async with httpx.AsyncClient(
                base_url=settings.spring_base_url,
                timeout=settings.request_timeout_seconds,
                headers=self._headers(),
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                body = response.json()
                if not body.get("success", False):
                    raise ValueError(body.get("message", "Spring Boot 工具调用失败"))
                response_payload = body.get("data")
                return response_payload
        except Exception as exc:
            success = False
            error_message = str(exc)
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await self._record_tool_log(
                ToolCallLog(
                    traceId=trace_id,
                    conversationId=conversation_id,
                    intent=intent,
                    riskLevel=risk_level,
                    toolName=tool_name,
                    requestPayload=request_payload,
                    responsePayload=response_payload,
                    success=success,
                    errorMessage=error_message,
                    durationMs=duration_ms,
                )
            )

    async def _record_tool_log(self, log: ToolCallLog) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=settings.spring_base_url,
                timeout=settings.request_timeout_seconds,
                headers=self._headers(),
            ) as client:
                await client.post("/ai/tools/trace/tool-call", json=log.model_dump(by_alias=False))
        except Exception:
            # 日志失败不阻断主流程，避免监控链路影响用户回答。
            return

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        return headers
