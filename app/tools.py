import time
from typing import Any

import httpx

from app.config import settings
from app.logging_setup import get_logger
from app.schemas import ToolCallLog

logger = get_logger(__name__)


class SpringToolClient:
    """调用 Spring Boot 电商业务工具的客户端。

    依赖外部传入的 httpx.AsyncClient（由 FastAPI lifespan 管理），
    避免每次请求新建连接池，提高并发性能。
    """

    def __init__(self, authorization: str | None, client: httpx.AsyncClient):
        self.authorization = authorization
        self._client = client

    async def search_products(
        self,
        *,
        trace_id: str,
        conversation_id: int | None,
        intent: str,
        risk_level: str,
        keyword: str,
        baby_age_month: int | None,
        min_price: Any | None = None,
        max_price: Any | None = None,
        limit: int = 6,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "keyword": keyword,
            "babyAgeMonth": baby_age_month,
            "limit": limit,
        }
        if min_price is not None:
            payload["minPrice"] = str(min_price)
        if max_price is not None:
            payload["maxPrice"] = str(max_price)
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
        error_message: str | None = None
        response_payload: Any = None

        try:
            response = await self._client.request(
                method,
                path,
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("success", False):
                # Spring Boot 业务失败：成功 HTTP 但 body.success=false
                raise ValueError(body.get("message", "Spring Boot 工具调用失败"))
            response_payload = body.get("data")
            return response_payload
        except httpx.TimeoutException as exc:
            success = False
            error_message = f"timeout: {exc}"
            logger.warning(
                "工具调用超时 tool=%s trace_id=%s path=%s err=%s",
                tool_name, trace_id, path, exc,
            )
            raise
        except httpx.HTTPStatusError as exc:
            success = False
            error_message = f"http_{exc.response.status_code}: {exc}"
            logger.warning(
                "工具返回HTTP错误 tool=%s trace_id=%s status=%s",
                tool_name, trace_id, exc.response.status_code,
            )
            raise
        except httpx.HTTPError as exc:
            success = False
            error_message = f"network: {exc}"
            logger.warning(
                "工具网络错误 tool=%s trace_id=%s err=%s",
                tool_name, trace_id, exc,
            )
            raise
        except ValueError as exc:
            # 业务约定的失败（success=false），属预期路径，info 级别
            success = False
            error_message = str(exc)
            logger.info(
                "工具业务失败 tool=%s trace_id=%s msg=%s",
                tool_name, trace_id, exc,
            )
            raise
        except Exception as exc:
            success = False
            error_message = f"unexpected: {exc}"
            logger.exception(
                "工具调用未知异常 tool=%s trace_id=%s",
                tool_name, trace_id,
            )
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
        """异步写回工具调用日志。日志写入失败不阻断主流程。"""
        try:
            await self._client.post(
                "/ai/tools/trace/tool-call",
                json=log.model_dump(by_alias=False),
                headers=self._headers(),
            )
        except Exception as exc:
            # 链路日志失败不影响用户回答，但要在本地服务侧留痕
            logger.warning(
                "写回工具调用日志失败 trace_id=%s tool=%s err=%s",
                log.traceId, log.toolName, exc,
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        return headers


# 静态访问 settings 以便单测覆盖；实际超时由 lifespan 创建 client 时设置。
_ = settings
