import unittest

import httpx
from pydantic import ValidationError

from app.schemas import ProductSearchToolParams
from app.tools import SpringToolClient


class ToolGovernanceTest(unittest.IsolatedAsyncioTestCase):
    def test_product_search_params_reject_invalid_limit(self) -> None:
        with self.assertRaises(ValidationError):
            ProductSearchToolParams(
                trace_id="trace",
                conversation_id=1,
                intent="SHOPPING_GUIDE",
                risk_level="LOW",
                keyword="纸尿裤",
                baby_age_month=8,
                limit=0,
            )

    async def test_tool_call_retries_timeout_and_records_final_success(self) -> None:
        attempts = 0
        trace_payloads: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.path == "/ai/tools/products/search":
                attempts += 1
                if attempts == 1:
                    raise httpx.ReadTimeout("first attempt timeout", request=request)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"records": [{"productId": 1, "productName": "纸尿裤"}]},
                    },
                )
            if request.url.path == "/ai/tools/trace/tool-call":
                trace_payloads.append(__import__("json").loads(request.content.decode()))
                return httpx.Response(200, json={"success": True, "data": {}})
            return httpx.Response(404, json={"success": False, "message": "missing"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tool_client = SpringToolClient(None, client)
            result = await tool_client.search_products(
                trace_id="trace",
                conversation_id=1,
                intent="SHOPPING_GUIDE",
                risk_level="LOW",
                keyword="纸尿裤",
                baby_age_month=8,
                limit=3,
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(result["records"][0]["productName"], "纸尿裤")
        self.assertEqual(len(trace_payloads), 1)
        self.assertTrue(trace_payloads[0]["success"])
        self.assertEqual(trace_payloads[0]["toolName"], "searchProducts")
