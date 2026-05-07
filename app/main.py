from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent import MuyingAgent
from app.config import settings
from app.logging_setup import get_logger, setup_logging
from app.schemas import ChatRequest

# 全局只初始化一次日志
setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：在启动时创建 httpx 连接池，关闭时优雅释放。

    与每请求新建相比，连接池可复用 keep-alive，
    实测可省 30%+ 网络握手开销。
    """
    spring_client = httpx.AsyncClient(
        base_url=settings.spring_base_url,
        timeout=settings.request_timeout_seconds,
        # 限制最大并发连接，避免对 muying-mall 形成连接风暴
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    llm_client = httpx.AsyncClient(
        base_url=settings.openai_base_url,
        timeout=settings.request_timeout_seconds,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )

    app.state.spring_client = spring_client
    app.state.llm_client = llm_client
    app.state.agent = MuyingAgent(spring_client=spring_client, llm_client=llm_client)

    logger.info(
        "Agent 启动完成 spring=%s llm_enabled=%s history_max_chars=%d",
        settings.spring_base_url, settings.enable_llm, settings.history_max_chars,
    )
    try:
        yield
    finally:
        await spring_client.aclose()
        await llm_client.aclose()
        logger.info("Agent 已优雅关闭")


app = FastAPI(title="Muying AI Agent", version="0.1.0", lifespan=lifespan)


# ===== CORS：禁用通配，仅放行配置的来源 =====
allowed_origins = settings.allowed_origins_list or ["http://localhost:8080"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Token"],
)


# ===== 内网 Token 校验：只对 /api/v1/* 生效，/health 等放行 =====
class InternalTokenMiddleware(BaseHTTPMiddleware):
    """共享密钥校验。

    Spring Boot（muying-mall）调用本服务时必须带 X-Internal-Token，
    防止前端或外网绕过 mall 直接打 8001 端口。
    设计为可选：当 settings.internal_token 留空时不校验（仅本地调试）。
    """

    PROTECTED_PREFIX = "/api/"

    async def dispatch(self, request: Request, call_next):
        if not settings.internal_token:
            return await call_next(request)
        if not request.url.path.startswith(self.PROTECTED_PREFIX):
            return await call_next(request)
        # OPTIONS 预检放行，由 CORSMiddleware 处理
        if request.method == "OPTIONS":
            return await call_next(request)

        token = request.headers.get("X-Internal-Token")
        if token != settings.internal_token:
            logger.warning(
                "拒绝未授权请求 path=%s ip=%s",
                request.url.path,
                request.client.host if request.client else "-",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing X-Internal-Token"},
            )
        return await call_next(request)


app.add_middleware(InternalTokenMiddleware)


# ===== 路由 =====


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


@app.post("/api/v1/chat")
async def chat(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(default=None),
):
    """AI Agent 默认流式聊天入口。"""
    return _stream_chat_response(request, body, authorization)


@app.post("/api/v1/chat/json")
async def chat_json(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(default=None),
):
    """AI Agent 非流式 JSON 聊天入口。"""
    agent = _resolve_agent(request)
    response = await agent.chat(body, authorization)
    return response.model_dump(mode="json", by_alias=True)


@app.post("/api/v1/chat/stream")
async def chat_stream(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(default=None),
):
    """AI Agent 流式聊天入口。"""
    return _stream_chat_response(request, body, authorization)


def _resolve_agent(request: Request) -> MuyingAgent:
    """从 app.state 获取 lifespan 注入的 agent 实例。"""
    agent: MuyingAgent | None = getattr(request.app.state, "agent", None)
    if agent is None:
        # 理论不会发生，除非 lifespan 未触发；保留兜底以便单测
        raise HTTPException(status_code=503, detail="agent not ready")
    return agent


def _stream_chat_response(
    request: Request,
    body: ChatRequest,
    authorization: str | None,
) -> StreamingResponse:
    """统一创建 SSE 响应，保证默认入口和兼容入口行为一致。"""
    agent = _resolve_agent(request)
    return StreamingResponse(
        agent.chat_stream(body, authorization),
        media_type="text/event-stream",
        headers={
            # 禁用代理缓冲，避免 SSE 在 Nginx 等网关后退化成一次性返回。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
