import inspect
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent import MuyingAgent
from app.config import settings
from app.logging_setup import get_logger, setup_logging
from app.schemas import ChatRequest

# 全局只初始化一次日志
setup_logging(settings.log_level)
logger = get_logger(__name__)

# ===== 限流器（slowapi）=====
# per-IP 令牌桶，使用进程内内存存储（KISS）。
# 多实例部署需要共享速率时再切到 Redis 后端：storage_uri="redis://..."。
# 默认上限（chat_rate_limit）含义示例："60/minute" / "10/second"。
limiter = Limiter(
    key_func=get_remote_address,
    # 启动期就指定全局默认上限，单接口可用 @limiter.limit 单独覆盖
    default_limits=[settings.chat_rate_limit],
)


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
        "Agent 启动完成 spring=%s llm_enabled=%s history_max_chars=%d rate_limit=%s",
        settings.spring_base_url,
        settings.enable_llm,
        settings.history_max_chars,
        settings.chat_rate_limit,
    )
    try:
        yield
    finally:
        await spring_client.aclose()
        await llm_client.aclose()
        logger.info("Agent 已优雅关闭")


app = FastAPI(title="Muying AI Agent", version="0.1.0", lifespan=lifespan)

# 把 limiter 挂到 app.state，slowapi 内部会读它做装饰器
app.state.limiter = limiter


# ===== 限流命中时的统一响应 =====
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """命中限流时返回 429，并通过日志记录被限的 IP，便于排查异常调用源。"""
    client_ip = request.client.host if request.client else "-"
    logger.warning(
        "命中限流 path=%s ip=%s detail=%s",
        request.url.path, client_ip, exc.detail,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "请求过于频繁，请稍后再试。",
            "limit": exc.detail,
        },
        headers={"Retry-After": "10"},
    )


# ===== CORS：禁用通配，仅放行配置的来源 =====
allowed_origins = settings.allowed_origins_list or ["http://localhost:8080"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Token"],
)


# ===== 内网 Token 校验：只对 /api/v1/* 生效，/livez /readyz 等放行 =====
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


# ===== 健康探针：拆分为 livez / readyz =====
# K8s 推荐做法：
# - livez：进程是否存活，失败会被重启
# - readyz：是否能接受流量（依赖是否就绪），失败会从负载均衡摘流但不重启


@app.get("/livez")
async def livez() -> dict[str, str]:
    """存活探针：只要进程跑得起来就返回 ok，不依赖任何外部组件。"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """就绪探针：检测 Spring Boot 上游是否可达。

    判断依据：调用 muying-mall 的 /actuator/health 或 /health 兜底；
    任意 2xx/3xx/4xx 都视为对端在线（4xx 说明路由有人接管，只是路径不对）；
    超时或连接失败才认为没就绪。
    """
    spring_client: httpx.AsyncClient | None = getattr(request.app.state, "spring_client", None)
    if spring_client is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "spring_client_uninitialized"},
        )

    # 优先 actuator/health；失败再退到 /health；都失败认为没就绪
    for path in ("/actuator/health", "/health"):
        try:
            # 短超时探针，不要因为依赖慢拖住 K8s
            resp = await spring_client.get(path, timeout=3.0)
            if resp.status_code < 500:
                return JSONResponse(
                    status_code=200,
                    content={"status": "ready", "spring": path, "code": resp.status_code},
                )
        except Exception as exc:
            logger.debug("readyz 探测失败 path=%s err=%s", path, exc)
            continue

    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": "spring_unreachable"},
    )


@app.get("/health")
async def health_legacy() -> dict[str, str]:
    """旧版健康检查兼容入口；新接入方应改用 /livez 或 /readyz。"""
    return {"status": "ok"}


# ===== 业务路由 =====


@app.post("/api/v1/chat")
@limiter.limit(settings.chat_rate_limit)
async def chat(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(default=None),
):
    """AI Agent 默认流式聊天入口。"""
    return _stream_chat_response(request, body, authorization)


@app.post("/api/v1/chat/json")
@limiter.limit(settings.chat_rate_limit)
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
@limiter.limit(settings.chat_rate_limit)
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
    """统一创建 SSE 响应，保证默认入口和兼容入口行为一致。

    把 starlette.Request 作为 disconnect_probe 传给 agent.chat_stream，
    让 LLM 流式过程中可以感知客户端断开并主动取消，避免继续烧 token。
    """
    agent = _resolve_agent(request)
    stream_kwargs = {}
    # 测试替身或未来轻量 agent 可能不支持 disconnect_probe；按签名能力传参保持兼容。
    if "disconnect_probe" in inspect.signature(agent.chat_stream).parameters:
        stream_kwargs["disconnect_probe"] = request
    return StreamingResponse(
        agent.chat_stream(body, authorization, **stream_kwargs),
        media_type="text/event-stream",
        headers={
            # 禁用代理缓冲，避免 SSE 在 Nginx 等网关后退化成一次性返回。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
