import uuid
from typing import Any

from app.intent import AgentIntent, IntentClassifier, RiskLevel
from app.llm import OptionalLlmClient
from app.schemas import ChatRequest, ChatResponse
from app.tools import SpringToolClient


class MuyingAgent:
    """母婴电商业务流程 Agent。"""

    def __init__(self) -> None:
        self.classifier = IntentClassifier()
        self.llm = OptionalLlmClient()

    async def chat(self, request: ChatRequest, authorization: str | None) -> ChatResponse:
        trace_id = uuid.uuid4().hex
        intent = self.classifier.classify(request.message)
        risk_level = self.classifier.assess_risk(request.message, intent)
        tools = SpringToolClient(authorization)

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
            )

        polished = await self.llm.polish(user_message=request.message, draft_answer=response.answer)
        if polished:
            response.answer = polished
        return response

    async def _handle_shopping(
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
                limit=3,
            ),
            default=[],
        )
        products_page = await self._safe_call(
            tools.search_products(
                trace_id=trace_id,
                conversation_id=request.conversation_id,
                intent=intent,
                risk_level=risk_level,
                keyword=keyword,
                baby_age_month=request.baby_age_month,
                limit=6,
            ),
            default={"records": []},
        )
        products = products_page.get("records", []) if isinstance(products_page, dict) else []

        answer = self._format_product_answer(keyword, products, knowledge)
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            suggestions=["查看商品详情", "加入购物车", "继续按预算筛选"],
            tool_results={"products": products, "knowledge": knowledge},
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
        )
        if not order:
            return self._tool_error_response(request, trace_id, intent, risk_level, "没有查到该订单，或当前账号无权访问。")

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
        )
        if not decision:
            return self._tool_error_response(request, trace_id, intent, risk_level, "售后规则判断失败，请稍后重试或联系人工客服。")

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
            )
            ticket_id = ticket.get("id") if isinstance(ticket, dict) else None

        answer = f"{decision.get('decision')} 最大可参考退款金额：{decision.get('maxRefundAmount') or '待核验'}。"
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
        )
        answer = self._format_knowledge_answer(keyword, knowledge)
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id=trace_id,
            answer=answer,
            intent=intent,
            risk_level=risk_level,
            suggestions=["查看育儿知识详情", "继续提问", "按月龄推荐商品"],
            tool_results={"knowledge": knowledge},
        )

    async def _safe_call(self, awaitable: Any, default: Any) -> Any:
        try:
            return await awaitable
        except Exception:
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
        )

    def _format_product_answer(self, keyword: str, products: list[dict[str, Any]], knowledge: list[dict[str, Any]]) -> str:
        if not products:
            return f"我没有找到和“{keyword}”直接匹配的商品。可以换一个关键词，或补充宝宝月龄、预算、品牌偏好。"

        lines = [f"根据你的需求，我优先筛选了这些商品："]
        for index, product in enumerate(products[:3], start=1):
            name = product.get("productName") or product.get("name") or "未命名商品"
            price = product.get("priceNew") or product.get("price") or "暂无价格"
            stock = product.get("stock") if product.get("stock") is not None else "库存待确认"
            lines.append(f"{index}. {name}，价格 {price}，库存 {stock}。")

        if knowledge:
            tip = knowledge[0]
            lines.append(f"相关育儿知识：{tip.get('title') or tip.get('summary') or '建议结合宝宝实际情况选择。'}")
        lines.append("涉及湿疹、过敏、用药等问题时，请优先咨询医生，AI只做购物和护理信息辅助。")
        return "\n".join(lines)

    def _format_knowledge_answer(self, keyword: str, knowledge: list[dict[str, Any]]) -> str:
        if not knowledge:
            return f"暂时没有检索到和“{keyword}”直接相关的育儿知识。你可以换个说法，或补充宝宝月龄和具体症状。"

        lines = ["我从育儿知识库里找到了这些参考信息："]
        for index, tip in enumerate(knowledge[:3], start=1):
            title = tip.get("title") or "育儿知识"
            summary = tip.get("summary") or ""
            lines.append(f"{index}. {title}：{summary}")
        lines.append("这些内容只作为日常护理参考，医疗判断和用药建议需要咨询专业医生。")
        return "\n".join(lines)
