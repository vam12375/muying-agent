import re


class AgentIntent:
    """第一版 Agent 意图常量。"""

    SHOPPING_GUIDE = "SHOPPING_GUIDE"
    ORDER_QUERY = "ORDER_QUERY"
    REFUND_CHECK = "REFUND_CHECK"
    KNOWLEDGE_QA = "KNOWLEDGE_QA"
    COMPLAINT_HANDOFF = "COMPLAINT_HANDOFF"
    UNKNOWN = "UNKNOWN"


class RiskLevel:
    """风险等级常量。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IntentClassifier:
    """轻量规则意图识别器。

    MVP 阶段先用规则确保可解释和可演示；后续可替换为模型分类器。
    """

    SHOPPING_WORDS = ("推荐", "买", "适合", "纸尿裤", "奶瓶", "护臀膏", "湿巾", "辅食", "玩具")
    ORDER_WORDS = ("订单", "物流", "发货", "快递", "到哪", "单号", "收货")
    REFUND_WORDS = ("退款", "退货", "换货", "售后", "取消订单")
    KNOWLEDGE_WORDS = ("能不能", "怎么办", "护理", "月龄", "红屁屁", "湿疹", "喂养", "育儿")
    COMPLAINT_WORDS = ("投诉", "质量", "过敏", "变质", "破损", "假货", "危险", "医生", "医院")

    def classify(self, message: str) -> str:
        text = message or ""
        if self._contains_any(text, self.COMPLAINT_WORDS):
            return AgentIntent.COMPLAINT_HANDOFF
        if self._contains_any(text, self.REFUND_WORDS):
            return AgentIntent.REFUND_CHECK
        if self._contains_any(text, self.ORDER_WORDS):
            return AgentIntent.ORDER_QUERY
        if self._contains_any(text, self.SHOPPING_WORDS):
            return AgentIntent.SHOPPING_GUIDE
        if self._contains_any(text, self.KNOWLEDGE_WORDS):
            return AgentIntent.KNOWLEDGE_QA
        return AgentIntent.UNKNOWN

    def assess_risk(self, message: str, intent: str) -> str:
        text = message or ""
        if self._contains_any(text, self.COMPLAINT_WORDS):
            return RiskLevel.HIGH
        if intent in (AgentIntent.REFUND_CHECK, AgentIntent.COMPLAINT_HANDOFF):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def extract_order_id(self, message: str) -> int | None:
        match = re.search(r"(?:订单ID|订单id|orderId|order_id)[：:\s]*(\d+)", message or "", re.IGNORECASE)
        return int(match.group(1)) if match else None

    def extract_order_no(self, message: str) -> str | None:
        match = re.search(r"(?:订单号|单号|orderNo|order_no)[：:\s]*([A-Za-z0-9_-]{8,})", message or "", re.IGNORECASE)
        return match.group(1) if match else None

    def extract_keyword(self, message: str) -> str:
        text = message or ""
        for word in ("推荐", "想买", "适合", "宝宝", "请问", "有没有"):
            text = text.replace(word, " ")
        return " ".join(text.split())[:80] or "母婴用品"

    def _contains_any(self, text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)
