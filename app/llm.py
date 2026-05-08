import json
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


class OptionalLlmClient:
    """可选的大模型润色客户端。

    没有配置密钥时直接返回 None，保证演示环境仍可跑通 Agent 流程。
    依赖外部传入的 httpx.AsyncClient（由 FastAPI lifespan 管理）。
    """

    def __init__(self, client: httpx.AsyncClient | None = None):
        # 允许 client=None 以兼容旧调用与单测；首次需要时再 lazy 创建
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # fallback：单测或脚本场景
            self._client = httpx.AsyncClient(
                base_url=settings.openai_base_url,
                timeout=settings.request_timeout_seconds,
            )
        return self._client

    async def polish(
        self,
        *,
        user_message: str,
        draft_answer: str,
        history: list | None = None,
        max_chars: int | None = None,
    ) -> str | None:
        if not settings.enable_llm or not settings.openai_api_key:
            return None

        payload = self._build_payload(
            user_message=user_message,
            draft_answer=draft_answer,
            history=history,
            stream=False,
            max_chars=max_chars,
        )

        try:
            response = await self.client.post(
                "/chat/completions", json=payload, headers=self._headers()
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            logger.warning("LLM 润色超时 err=%s", exc)
            return None
        except httpx.HTTPError as exc:
            logger.warning("LLM 润色HTTP错误 err=%s", exc)
            return None
        except Exception:
            logger.exception("LLM 润色未知异常")
            return None

    async def stream_polish(
        self,
        *,
        user_message: str,
        draft_answer: str,
        history: list | None = None,
        max_chars: int | None = None,
    ) -> AsyncIterator[str]:
        if not settings.enable_llm or not settings.openai_api_key:
            return

        payload = self._build_payload(
            user_message=user_message,
            draft_answer=draft_answer,
            history=history,
            stream=True,
            max_chars=max_chars,
        )

        async with self.client.stream(
            "POST", "/chat/completions", json=payload, headers=self._headers()
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    # 偶发的非 JSON 心跳/注释行，跳过即可，但要记录
                    logger.debug("LLM 流式片段非JSON，已跳过 line=%s err=%s", data[:120], exc)
                    continue
                content = chunk["choices"][0].get("delta", {}).get("content")
                if content:
                    yield content

    def _build_payload(
        self,
        *,
        user_message: str,
        draft_answer: str,
        history: list | None,
        stream: bool,
        max_chars: int | None = None,
    ) -> dict:
        history_text = self._format_history(history or [], max_chars=max_chars)

        # 历史上下文只辅助润色，不允许覆盖业务工具返回的事实结果。
        return {
            "model": settings.openai_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是母婴电商平台的AI助手。回答要简洁、可靠，"
                        "涉及医疗或质量投诉必须建议人工处理。"
                        "不得把'请补充信息'的业务草稿改写成'正在查询'或'已处理'。"
                        "如果业务草稿包含'依据来源'，必须完整保留来源列表。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"同一会话历史上下文（已按字符上限裁剪）：\n"
                        f"{history_text or '暂无历史上下文'}\n\n"
                        f"用户当前问题：{user_message}\n\n"
                        f"业务系统草稿：{draft_answer}\n\n"
                        "请结合历史上下文润色为自然中文回答，"
                        "不要编造业务系统草稿中没有的商品、订单或售后结论；"
                        "如果草稿要求用户补充订单号或订单ID，必须保留这个要求；"
                        "如果草稿包含依据来源，不要删除或替换来源。"
                    ),
                },
            ],
            "temperature": 0.3,
            "stream": stream,
        }

    def _format_history(self, history: list, *, max_chars: int | None = None) -> str:
        """把 history 渲染为多行文本，并按字符上限从近到远裁剪。

        策略（KISS）：
        1. 先转成 "角色: 内容" 行；
        2. 从最新一条开始累加，超过 max_chars 就停；
        3. 反转回时间顺序输出，保证 LLM 看到的是早→新。

        max_chars 为 None 时使用 settings.history_max_chars 兜底；
        请求级 ChatRequest.max_context_chars 优先级最高，由调用方传入。
        """
        rendered: list[str] = []
        for item in history:
            role = (
                getattr(item, "role", None)
                if not isinstance(item, dict)
                else item.get("role")
            )
            content = (
                getattr(item, "content", None)
                if not isinstance(item, dict)
                else item.get("content")
            )
            if not content:
                continue
            normalized_role = "用户" if str(role).upper() == "USER" else "助手"
            rendered.append(f"{normalized_role}: {content}")

        if not rendered:
            return ""

        # 从最新（列表末尾）往前累加，命中字符上限即停止
        effective_max = max_chars if max_chars is not None else settings.history_max_chars
        effective_max = max(0, effective_max)
        kept: list[str] = []
        used = 0
        for line in reversed(rendered):
            # +1 是行间换行符的近似预算
            cost = len(line) + 1
            if used + cost > effective_max and kept:
                logger.debug(
                    "历史上下文裁剪生效 total=%d kept=%d max_chars=%d",
                    len(rendered), len(kept), effective_max,
                )
                break
            kept.append(line)
            used += cost

        return "\n".join(reversed(kept))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
