import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from app.intent import AgentIntent, IntentClassifier, RiskLevel
from app.llm import OptionalLlmClient
from app.logging_setup import get_logger
from app.rag import RetrievalBundle, build_retrieval_bundle
from app.schemas import ChatRequest, ChatResponse
from app.tools import SpringToolClient
from app.workflow import WorkflowStep, completed_workflow

logger = get_logger(__name__)

# SSE 流式输出内置心跳间隔（秒）
# 选 25s 是因为：常见反向代理（nginx、cloudflare）默认 60s 空闲超时，
# 25s 给上下游各留一次包袱；OpenAI 兼容代理通常每 ~3s 推一个 token，
# 心跳只在 LLM 出现长间隙（>25s）时才会触发，正常聊天不冗余。
SSE_HEARTBEAT_SECONDS = 25.0


class _DisconnectProbe(Protocol):
    """流式过程中用于检测客户端是否已断开的协议。

    抽象出 Protocol 而非直接绑定 starlette.Request，方便：
    - 单测里传一个永远 False 的 stub；
    - 将来切到 ASGI 原生接口或自研监听。
    """

    async def is_disconnected(self) -> bool: ...


class MuyingAgent:
    """母婴电商业务流程 Agent。"""

    def __init__(
        self,
        spring_client: httpx.AsyncClient | None = None,
        llm_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.classifier = IntentClassifier()
        self.llm = OptionalLlmClient(client=llm_client)
        # spring_client 由 lifespan 注入；单测可不传，由调用方提供 SpringToolClient mock
        self._spring_client = spring_client

    def _build_tools(self, authorization: str | None) -> SpringToolClient:
        """根据请求级 authorization 构建工具客户端，复用全局连接池。"""
        if self._spring_client is None:
            raise RuntimeError(
                "Spring httpx client 未初始化。请通过 FastAPI lifespan 注入，"
                "或在测试中直接 mock SpringToolClient。"
            )
        return SpringToolClient(authorization, self._spring_client)

    async def chat(
        self,
        request: ChatRequest,
        authorization: str | None,
        *,
        polish: bool = True,
    ) -> ChatResponse:
        trace_id = uuid.uuid4().hex
        intent = self.classifier.classify(request.message)
        risk_level = self.classifier.assess_risk(request.message, intent)
        tools = self._build_tools(authorization)

        logger.info(
            "Agent 开始处理 trace_id=%s intent=%s risk=%s conv=%s",
            trace_id, intent, risk_level, request.conversation_id,
        )

        if intent == AgentIntent.SHOPPING_GUIDE:
            response = await self._handle_shopping(request, tools, trace_id, intent, risk_level)
        elif intent == AgentIntent.ORDER_QUERY:
            response = await self._handle_order(request, tools, trace_id, intent, risk_level)
        elif intent == AgentIntent.REFUND_CHECK:
            response = await self._handle_refund(request, tools, trace_id, intent, risk_level)
        elif intent == AgentIntent.COMPLAINT_HANDOFF:
            response = await self._handle_complaint(request, tools, trace_id, intent, risk_level)
        elif intent == AgentIntent.KNOWLEDGE_QA:
            response = await self._handle_knowledge(request, tools, trace_id, intent, risk_level)
        else:
            response = ChatResponse(
                conversation_id=request.conversation_id,
                trace_id=trace_id,
                answer="我可以帮你做商品推荐、订单查询、售后判断和育儿知识问答。你可以告诉我宝宝月龄、预算或订单号。",
                intent=AgentIntent.UNKNOWN,
                risk_level=RiskLevel.LOW,
                suggestions=["推荐8个月宝宝纸尿裤", "查询订单号", "判断订单是否可退款"],
                workflow=self._workflow(
                    trace_id,
                    AgentIntent.UNKNOWN,
                    RiskLevel.LOW,
                    [WorkflowStep.CLASSIFY, WorkflowStep.RISK_GATE, WorkflowStep.RESPOND],
                ),
            )

        if not polish:
            return response

        polished = await self.llm.polish(
            user_message=request.message,
            draft_answer=response.answer,
            history=request.history,
            max_chars=request.max_context_chars,
        )
        if polished:
            response.answer = polished
        return response

    async def chat_stream(
        self,
        request: ChatRequest,
        authorization: str | None,
        *,
        disconnect_probe: _DisconnectProbe | None = None,
    ) -> AsyncIterator[str]:
        """SSE 流式聊天。

        事件协议：
        - meta：业务上下文（trace_id / intent / suggestions 等），answer 留空
        - delta：增量 token 文本片段（content 字段）
        - ping：心跳事件，每 SSE_HEARTBEAT_SECONDS 秒注入一次，
                防止反向代理空闲超时切断；前端可忽略
        - error：流式润色失败时的错误说明
        - done：会话终结，data 含完整 ChatResponse + status 字段
                * status="success"   正常完成
                * status="error"     LLM 流式失败但已回退草稿
                * status="cancelled" 客户端断开导致提前终止
        """
        response = await self.chat(request, authorization, polish=False)
        yield self._sse_event("meta", self._stream_meta(response))

        # 状态机：done 事件最终携带的 status
        final_status = "success"
        answer_parts: list[str] = []

        try:
            stream_iter = self.llm.stream_polish(
                user_message=request.message,
                draft_answer=response.answer,
                history=request.history,
                max_chars=request.max_context_chars,
            ).__aiter__()

            while True:
                # 用 wait_for 控制空闲：超时即心跳；客户端断开即提前退出
                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=SSE_HEARTBEAT_SECONDS,
                    )
                except StopAsyncIteration:
                    # LLM 正常结束
                    break
                except asyncio.TimeoutError:
                    # 空闲超过心跳间隔：先看客户端是否已经走了，再决定是发心跳还是终止
                    if await self._client_disconnected(disconnect_probe):
                        final_status = "cancelled"
                        logger.info("流式期间客户端已断开 trace_id=%s", response.trace_id)
                        break
                    yield self._sse_event("ping", {"ts": int(time.time())})
                    continue

                if not chunk:
                    continue
                answer_parts.append(chunk)
                yield self._sse_event("delta", {"content": chunk})

                # 正常 token 到达后也顺带做一次断开检测；不阻塞主路径
                if await self._client_disconnected(disconnect_probe):
                    final_status = "cancelled"
                    logger.info("流式期间客户端已断开 trace_id=%s", response.trace_id)
                    break

        except asyncio.CancelledError:
            # 协程被取消（FastAPI 在客户端断开时会触发）：标记并上抛
            logger.info("流式润色被取消 trace_id=%s", response.trace_id)
            final_status = "cancelled"
            raise
        except Exception:
            # 流式润色失败时回退到业务草稿，避免外部模型异常中断用户主流程
            logger.exception("流式润色失败 trace_id=%s", response.trace_id)
            final_status = "error"
            if not answer_parts:
                answer_parts.append(response.answer)
                yield self._sse_event("delta", {"content": response.answer})
                yield self._sse_event(
                    "error",
                    {"status": "error", "message": "大模型流式润色失败，已返回业务草稿。"},
                )
            else:
                yield self._sse_event(
                    "error",
                    {"status": "error", "message": "大模型流式润色中断，已返回当前生成内容。"},
                )

        # 客户端取消时不再补 delta（用户已离开），其它情况下保证至少一条文本
        if not answer_parts and final_status != "cancelled":
            answer_parts.append(response.answer)
            yield self._sse_event("delta", {"content": response.answer})

        response.answer = "".join(answer_parts) or response.answer
        yield self._sse_event("done", self._build_done_payload(response, final_status))

    async def _client_disconnected(self, probe: _DisconnectProbe | None) -> bool:
        """探测客户端是否已断开。失败时静默返回 False，避免单次探测错误中断流。"""
        if probe is None:
            return False
        try:
            return await probe.is_disconnected()
        except Exception:
            return False

    def _stream_meta(self, response: ChatResponse) -> dict[str, Any]:
        # meta 先发业务上下文，answer 留给 delta/done，前端可边收边渲染正文
        data = response.model_dump(mode="json", by_alias=True)
        data["answer"] = ""
        return data

    def _build_done_payload(self, response: ChatResponse, status: str) -> dict[str, Any]:
        """done 事件负载：完整响应 + 终态 status。

        前端通过 status 区分：
        - success：可以保存对话；
        - error：仍可保存（已含兜底文本），可加错误提示；
        - cancelled：用户已离开，可不保存或仅本地保存。
        """
        payload = response.model_dump(mode="json", by_alias=True)
        payload["status"] = status
        return payload

    def _sse_event(self, event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def _handle_shopping(
        self,
        request: ChatRequest,
        tools: SpringToolClient,
        trace_id: str,
        intent: str,
        risk_level: str,
    ) -> ChatResponse:
        keyword = self.classifier.extract_keyword(request.message)
        baby_age_month = request.baby_age_month
        if baby_age_month is None:
            baby_age_month = self.classifier.extract_baby_age_month(request.message)
        min_price, max_price = self.classifier.extract_price_range(request.message)

        # 知识检索与商品检索互不依赖，并发执行可省一半延迟
        knowledge_task = self._safe_call(
            tools.search_knowledge(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                keyword=keyword,
                limit=3,
            ),
            default=[],
            tool_name="search_knowledge",
            trace_id=trace_id,
        )
        products_task = self._safe_call(
            tools.search_products(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                keyword=keyword,
                baby_age_month=baby_age_month,
                min_price=min_price,
                max_price=max_price,
                limit=6,
            ),
            default={"records": []},
            tool_name="search_products",
            trace_id=trace_id,
        )
        knowledge, products_page = await asyncio.gather(knowledge_task, products_task)
        products = products_page.get("records", []) if isinstance(products_page, dict) else []

        retrieval = build_retrieval_bundle(query=keyword, knowledge=knowledge, products=products)
        answer = self._append_sources(
            self._format_product_answer(keyword, products, knowledge),
            retrieval,
        )
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            suggestions=["查看商品详情", "加入购物车", "继续按预算筛选"],
            tool_results={
                "products": products,
                "knowledge": knowledge,
                "sources": retrieval.to_tool_result(),
            },
            workflow=self._workflow(
                trace_id,
                intent,
                risk_level,
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.RETRIEVE,
                    WorkflowStep.CALL_TOOL,
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    async def _handle_order(
        self,
        request: ChatRequest,
        tools: SpringToolClient,
        trace_id: str,
        intent: str,
        risk_level: str,
    ) -> ChatResponse:
        order_id = self.classifier.extract_order_id(request.message)
        order_no = self.classifier.extract_order_no(request.message)
        if order_id is None and order_no is None:
            return ChatResponse(
                conversation_id=request.conversation_id,
                trace_id=trace_id,
                answer="请提供订单ID或订单号，我可以帮你查询发货、物流和订单状态。",
                intent=intent,
                risk_level=risk_level,
                suggestions=["去订单列表查看", "输入：订单号 2026xxxx", "联系人工客服"],
                workflow=self._workflow(
                    trace_id,
                    intent,
                    risk_level,
                    [WorkflowStep.CLASSIFY, WorkflowStep.RISK_GATE, WorkflowStep.RESPOND],
                ),
            )

        order = await self._safe_call(
            tools.get_order_status(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                order_id=order_id,
                order_no=order_no,
            ),
            default=None,
            tool_name="get_order_status",
            trace_id=trace_id,
        )
        if not order:
            return self._tool_error_response(
                request, trace_id, intent, risk_level,
                "没有查到该订单，或当前账号无权访问。",
            )

        answer = (
            f"订单 {order.get('orderNo')} 当前状态是 {order.get('status')}。"
            f"物流公司：{order.get('shippingCompany') or '暂无'}，"
            f"物流单号：{order.get('trackingNo') or '暂无'}。"
        )
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            suggestions=["查看物流详情", "判断是否可退款", "联系人工客服"],
            tool_results={"order": order},
            workflow=self._workflow(
                trace_id,
                intent,
                risk_level,
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.CALL_TOOL,
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    async def _handle_refund(
        self,
        request: ChatRequest,
        tools: SpringToolClient,
        trace_id: str,
        intent: str,
        risk_level: str,
    ) -> ChatResponse:
        order_id = self.classifier.extract_order_id(request.message)
        order_no = self.classifier.extract_order_no(request.message)
        if order_id is None and order_no is None:
            return ChatResponse(
                conversation_id=request.conversation_id,
                trace_id=trace_id,
                answer="我可以先判断售后规则。请补充订单ID或订单号，例如：订单号 2026xxxx 可以退款吗？",
                intent=intent,
                risk_level=risk_level,
                suggestions=["去订单列表查看", "输入订单号", "转人工客服"],
                workflow=self._workflow(
                    trace_id,
                    intent,
                    risk_level,
                    [WorkflowStep.CLASSIFY, WorkflowStep.RISK_GATE, WorkflowStep.RESPOND],
                ),
            )

        decision = await self._safe_call(
            tools.evaluate_refund(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                order_id=order_id,
                order_no=order_no,
                reason=request.message,
            ),
            default=None,
            tool_name="evaluate_refund",
            trace_id=trace_id,
        )
        if not decision:
            return self._tool_error_response(
                request, trace_id, intent, risk_level,
                "售后规则判断失败，请稍后重试或联系人工客服。",
            )

        ticket_id = None
        human_required = bool(decision.get("humanApprovalRequired", True))
        if human_required and decision.get("riskLevel") == RiskLevel.HIGH:
            ticket = await self._safe_call(
                tools.create_ticket(
                    trace_id=trace_id,
                    conversation_id=request.conversation_id,
                    intent=intent,
                    risk_level=RiskLevel.HIGH,
                    title="AI识别到高风险售后问题",
                    content=request.message,
                    order_id=decision.get("orderId"),
                ),
                default=None,
                tool_name="create_ticket",
                trace_id=trace_id,
            )
            ticket_id = ticket.get("id") if isinstance(ticket, dict) else None

        answer = (
            f"{decision.get('decision')} 最大可参考退款金额："
            f"{decision.get('maxRefundAmount') or '待核验'}。"
        )
        if human_required:
            answer += " 为保证资金和售后安全，AI不会直接退款，需要人工确认。"

        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=decision.get("riskLevel", risk_level),
            human_handoff_required=human_required,
            ticket_id=ticket_id,
            suggestions=["提交售后申请", "上传凭证", "联系人工客服"],
            tool_results={"refundDecision": decision},
            workflow=self._workflow(
                trace_id,
                intent,
                decision.get("riskLevel", risk_level),
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.CALL_TOOL,
                    *([WorkflowStep.HUMAN_HANDOFF] if human_required else []),
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    async def _handle_complaint(
        self,
        request: ChatRequest,
        tools: SpringToolClient,
        trace_id: str,
        intent: str,
        risk_level: str,
    ) -> ChatResponse:
        ticket = await self._safe_call(
            tools.create_ticket(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=RiskLevel.HIGH,
                title="AI识别到高风险投诉/质量问题",
                content=request.message,
            ),
            default=None,
            tool_name="create_ticket",
            trace_id=trace_id,
        )
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer="这个问题涉及质量、过敏或投诉风险，我已经为你转入人工处理。AI不会给医疗结论，也不会直接执行退款。",
            intent=intent,
            risk_level=RiskLevel.HIGH,
            human_handoff_required=True,
            ticket_id=ticket_id,
            suggestions=["补充订单号", "上传凭证图片", "等待客服处理"],
            tool_results={"ticket": ticket},
            workflow=self._workflow(
                trace_id,
                intent,
                RiskLevel.HIGH,
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.HUMAN_HANDOFF,
                    WorkflowStep.CALL_TOOL,
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    async def _handle_knowledge(
        self,
        request: ChatRequest,
        tools: SpringToolClient,
        trace_id: str,
        intent: str,
        risk_level: str,
    ) -> ChatResponse:
        keyword = self.classifier.extract_keyword(request.message)
        knowledge = await self._safe_call(
            tools.search_knowledge(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                keyword=keyword,
                limit=5,
            ),
            default=[],
            tool_name="search_knowledge",
            trace_id=trace_id,
        )
        retrieval = build_retrieval_bundle(query=keyword, knowledge=knowledge, products=[])
        answer = self._append_sources(self._format_knowledge_answer(keyword, knowledge), retrieval)
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            suggestions=["查看育儿知识详情", "继续提问", "按月龄推荐商品"],
            tool_results={"knowledge": knowledge, "sources": retrieval.to_tool_result()},
            workflow=self._workflow(
                trace_id,
                intent,
                risk_level,
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.RETRIEVE,
                    WorkflowStep.CALL_TOOL,
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    async def _safe_call(
        self,
        awaitable: Any,
        default: Any,
        *,
        tool_name: str = "unknown",
        trace_id: str = "-",
    ) -> Any:
        """统一兜底：保留 CancelledError 上抛，其余异常记录后返回默认值。"""
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "工具调用失败已降级 tool=%s trace_id=%s default=%r",
                tool_name, trace_id, default,
            )
            return default

    def _tool_error_response(
        self,
        request: ChatRequest,
        trace_id: str,
        intent: str,
        risk_level: str,
        answer: str,
    ) -> ChatResponse:
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            human_handoff_required=True,
            suggestions=["稍后重试", "联系人工客服", "返回订单列表"],
            workflow=self._workflow(
                trace_id,
                intent,
                risk_level,
                [
                    WorkflowStep.CLASSIFY,
                    WorkflowStep.RISK_GATE,
                    WorkflowStep.SELECT_TOOL,
                    WorkflowStep.FALLBACK,
                    WorkflowStep.RESPOND,
                ],
            ),
        )

    def _append_sources(self, answer: str, retrieval: RetrievalBundle) -> str:
        """在最终草稿中保留依据来源，降低 RAG/LLM 编造风险。"""
        suffix = retrieval.format_answer_suffix()
        if not suffix:
            return answer
        return f"{answer}{suffix}"

    def _workflow(
        self,
        trace_id: str,
        intent: str,
        risk_level: str,
        steps: list[str],
    ) -> dict[str, Any]:
        return completed_workflow(
            trace_id=trace_id,
            intent=intent,
            risk_level=risk_level,
            steps=steps,
        )

    def _format_product_answer(
        self,
        keyword: str,
        products: list[dict[str, Any]],
        knowledge: list[dict[str, Any]],
    ) -> str:
        if not products:
            return f"我没有找到和\u201c{keyword}\u201d直接匹配的商品。可以换一个关键词，或补充宝宝月龄、预算、品牌偏好。"

        lines = ["根据你的需求，我优先筛选了这些商品："]
        for index, product in enumerate(products[:3], start=1):
            name = product.get("productName") or product.get("name") or "未命名商品"
            price = product.get("priceNew") or product.get("price") or "暂无价格"
            stock = product.get("stock") if product.get("stock") is not None else "库存待确认"
            lines.append(f"{index}. {name}，价格 {price}，库存 {stock}。")

        if knowledge:
            tip = knowledge[0]
            lines.append(
                f"相关育儿知识：{tip.get('title') or tip.get('summary') or '建议结合宝宝实际情况选择。'}"
            )
        lines.append("涉及湿疹、过敏、用药等问题时，请优先咨询医生，AI只做购物和护理信息辅助。")
        return "\n".join(lines)

    def _format_knowledge_answer(
        self,
        keyword: str,
        knowledge: list[dict[str, Any]],
    ) -> str:
        if not knowledge:
            return f"暂时没有检索到和\u201c{keyword}\u201d直接相关的育儿知识。你可以换个说法，或补充宝宝月龄和具体症状。"

        lines = ["我从育儿知识库里找到了这些参考信息："]
        for index, tip in enumerate(knowledge[:3], start=1):
            title = tip.get("title") or "育儿知识"
            summary = tip.get("summary") or ""
            lines.append(f"{index}. {title}：{summary}")
        lines.append("这些内容只作为日常护理参考，医疗判断和用药建议需要咨询专业医生。")
        return "\n".join(lines)
