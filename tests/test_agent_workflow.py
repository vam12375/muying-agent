import unittest

import httpx

from app.agent import MuyingAgent
from app.intent import AgentIntent, RiskLevel
from app.schemas import ChatRequest


class AgentWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_knowledge_answer_includes_sources_and_workflow(self) -> None:
        class FakeTools:
            async def search_knowledge(self, **kwargs):
                return [
                    {
                        "id": 7,
                        "title": "6个月辅食添加",
                        "summary": "优先从高铁米粉开始，观察过敏反应。",
                    }
                ]

        dummy_spring = httpx.AsyncClient(base_url="http://example.invalid")
        try:
            agent = MuyingAgent(spring_client=dummy_spring)
            response = await agent._handle_knowledge(
                ChatRequest(message="6个月宝宝辅食怎么加"),
                FakeTools(),
                "trace",
                AgentIntent.KNOWLEDGE_QA,
                RiskLevel.LOW,
            )
        finally:
            await dummy_spring.aclose()

        self.assertIn("依据来源", response.answer)
        self.assertEqual(response.tool_results["sources"][0]["title"], "6个月辅食添加")
        self.assertEqual(response.workflow["status"], "completed")
        self.assertIn("retrieve", response.workflow["steps"])
