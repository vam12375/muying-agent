from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware

from app.agent import MuyingAgent
from app.schemas import ChatRequest

app = FastAPI(title="Muying AI Agent", version="0.1.0")
agent = MuyingAgent()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


@app.post("/api/v1/chat")
async def chat(request: ChatRequest, authorization: str | None = Header(default=None)):
    """AI Agent 聊天入口。"""
    response = await agent.chat(request, authorization)
    return response.model_dump(mode="json", by_alias=True)
