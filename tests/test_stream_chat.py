import unittest

import httpx

import app.main as main_module
from app.agent import MuyingAgent
from app.intent import AgentIntent, RiskLevel
from app.schemas import ChatRequest, ChatResponse


class FakeRouteAgent:
    async def chat(self, request: ChatRequest, authorization: str | None):
        return ChatResponse(
            conversation_id=request.conversation_id,
            trace_id="test-trace",
            answer="你好",
            intent=AgentIntent.UNKNOWN,
            risk_level=RiskLevel.LOW,
        )

    async def chat_stream(self, request: ChatRequest, authorization: str | None):
        yield 'event: delta\ndata: {"content": "你好"}\n\n'
        yield 'event: done\ndata: {"answer": "你好"}\n\n'


class FakeStreamingLlm:
    async def stream_polish(self, *, user_message: str, draft_answer: str, history=None, max_chars: int | None = None):
        yield "流"
        yield "式"


def _swap_agent(fake_agent: object):
    """将 lifespan 注入的 app.state.agent 临时替换为 fake，返回 (恢复函数)。"""
    state = main_module.app.state
    original = getattr(state, "agent", None)
    state.agent = fake_agent

    def restore() -> None:
        if original is not None:
            state.agent = original
        else:
            try:
                delattr(state, "agent")
            except AttributeError:
                pass

    return restore


class StreamChatTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_chat_route_returns_sse_chunks(self) -> None:
        restore = _swap_agent(FakeRouteAgent())
        try:
            transport = httpx.ASGITransport(app=main_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/chat", json={"message": "你好"})
        finally:
            restore()

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("event: delta", response.text)
        self.assertIn('"content": "你好"', response.text)
        self.assertIn("event: done", response.text)

    async def test_stream_route_returns_sse_chunks(self) -> None:
        restore = _swap_agent(FakeRouteAgent())
        try:
            transport = httpx.ASGITransport(app=main_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/chat/stream", json={"message": "你好"})
        finally:
            restore()

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("event: delta", response.text)
        self.assertIn('"content": "你好"', response.text)
        self.assertIn("event: done", response.text)

    async def test_json_chat_route_returns_original_shape(self) -> None:
        restore = _swap_agent(FakeRouteAgent())
        try:
            transport = httpx.ASGITransport(app=main_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/v1/chat/json", json={"message": "你好"})
        finally:
            restore()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["answer"], "你好")

    async def test_agent_streams_polished_answer_and_done_event(self) -> None:
        # 该单测不走 HTTP 路径，但 MuyingAgent 现在要求 spring_client；
        # 这里只测 chat_stream + LLM 流式，不会触达 spring，传一个 dummy client 即可。
        dummy_spring = httpx.AsyncClient(base_url="http://example.invalid")
        try:
            agent = MuyingAgent(spring_client=dummy_spring)
            agent.llm = FakeStreamingLlm()

            chunks = [
                chunk
                async for chunk in agent.chat_stream(
                    ChatRequest(message="随便聊聊"), authorization=None
                )
            ]
            body = "".join(chunks)
        finally:
            await dummy_spring.aclose()

        self.assertIn("event: meta", body)
        self.assertIn('"content": "流"', body)
        self.assertIn('"content": "式"', body)
        self.assertIn("event: done", body)
        self.assertIn('"answer": "流式"', body)

    async def test_agent_uses_extracted_shopping_slots_when_profile_missing(self) -> None:
        class FakeTools:
            captured_baby_age_month = None
            captured_max_price = None

            async def search_knowledge(self, **kwargs):
                return []

            async def search_products(self, **kwargs):
                self.captured_baby_age_month = kwargs.get("baby_age_month")
                self.captured_max_price = kwargs.get("max_price")
                return {"records": []}

        dummy_spring = httpx.AsyncClient(base_url="http://example.invalid")
        try:
            agent = MuyingAgent(spring_client=dummy_spring)
            fake_tools = FakeTools()
            response = await agent._handle_shopping(
                ChatRequest(message="宝宝8个月，预算200以内，推荐纸尿裤"),
                fake_tools,
                "trace",
                AgentIntent.SHOPPING_GUIDE,
                RiskLevel.LOW,
            )
        finally:
            await dummy_spring.aclose()

        self.assertEqual(fake_tools.captured_baby_age_month, 8)
        self.assertEqual(fake_tools.captured_max_price, 200)
        self.assertIn("没有找到", response.answer)
