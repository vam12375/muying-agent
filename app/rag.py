from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievedSource:
    """归一化后的检索来源，供回答引用和前端/后台展示。"""

    type: str
    source_id: str
    title: str
    snippet: str
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "sourceId": self.source_id,
            "title": self.title,
            "snippet": self.snippet,
        }
        if self.score is not None:
            data["score"] = self.score
        return data


@dataclass(frozen=True)
class RetrievalBundle:
    """一次 RAG 检索的结果包。

    当前先接入已有商品/育儿知识工具；后续接 ES + pgvector/Qdrant 时，
    只需要在这里继续扩展来源类型，不改 Agent 主流程。
    """

    query: str
    sources: list[RetrievedSource] = field(default_factory=list)

    def to_tool_result(self) -> list[dict[str, Any]]:
        return [source.to_dict() for source in self.sources]

    def format_answer_suffix(self, *, max_sources: int = 3) -> str:
        if not self.sources:
            return ""

        lines = ["", "依据来源："]
        for index, source in enumerate(self.sources[:max_sources], start=1):
            label = {
                "KNOWLEDGE": "育儿知识",
                "PRODUCT": "商品",
                "FAQ": "FAQ",
                "POLICY": "政策",
                "LOGISTICS": "物流规则",
            }.get(source.type, source.type)
            lines.append(f"[{index}] {label}《{source.title}》：{source.snippet}")
        return "\n".join(lines)


def build_retrieval_bundle(
    *,
    query: str,
    knowledge: list[dict[str, Any]] | None = None,
    products: list[dict[str, Any]] | None = None,
) -> RetrievalBundle:
    """把业务工具返回值归一化为可引用来源。

    KISS：先只做来源归一化和引用展示，不在 Python 侧复制电商主库。
    真正的向量索引可以由后续后台任务写入 ES/pgvector，再复用本函数输出结构。
    """

    sources: list[RetrievedSource] = []
    for item in knowledge or []:
        source_id = _first_text(item, "id", "tipId", "knowledgeId") or "unknown"
        title = _first_text(item, "title", "name") or "育儿知识"
        snippet = _first_text(item, "summary", "content") or "暂无摘要"
        sources.append(
            RetrievedSource(
                type="KNOWLEDGE",
                source_id=source_id,
                title=title,
                snippet=_compact(snippet),
                score=_optional_float(item.get("score")),
            )
        )

    for item in products or []:
        source_id = _first_text(item, "productId", "id") or "unknown"
        title = _first_text(item, "productName", "name") or "未命名商品"
        price = _first_text(item, "priceNew", "price", "minPrice")
        stock = _first_text(item, "stock")
        parts = []
        if price:
            parts.append(f"价格 {price}")
        if stock:
            parts.append(f"库存 {stock}")
        snippet = "，".join(parts) or _first_text(item, "productDetail") or "暂无商品摘要"
        sources.append(
            RetrievedSource(
                type="PRODUCT",
                source_id=source_id,
                title=title,
                snippet=_compact(snippet),
                score=_optional_float(item.get("score")),
            )
        )

    return RetrievalBundle(query=query, sources=sources)


def _first_text(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return None


def _compact(text: str, *, limit: int = 96) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
