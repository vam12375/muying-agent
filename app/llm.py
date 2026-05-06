import httpx

from app.config import settings


class OptionalLlmClient:
    """可选的大模型润色客户端。

    没有配置密钥时直接返回 None，保证演示环境仍可跑通 Agent 流程。
    """

    async def polish(self, *, user_message: str, draft_answer: str) -> str | None:
        if not settings.enable_llm or not settings.openai_api_key:
            return None

        payload = {
            "model": settings.openai_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是母婴电商平台的AI助手。回答要简洁、可靠，涉及医疗或质量投诉必须建议人工处理。",
                },
                {
                    "role": "user",
                    "content": f"用户问题：{user_message}\n\n业务系统草稿：{draft_answer}\n\n请润色为自然中文回答。",
                },
            ],
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds) as client:
            response = await client.post("/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
