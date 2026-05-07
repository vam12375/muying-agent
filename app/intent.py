import re
from decimal import Decimal


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
        text = message or ""
        match = re.search(r"(?:订单号|单号|orderNo|order_no)[：:\s]*([A-Za-z0-9_-]{8,})", text, re.IGNORECASE)
        if match:
            return match.group(1)

        if not self._contains_any(text, self.ORDER_WORDS):
            return None

        # 用户常写“这个订单到哪里了；ODxxx”，没有显式“订单号”前缀时也要识别业务单号。
        fallback_match = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z]{0,6}\d[A-Za-z0-9_-]{7,})(?![A-Za-z0-9_-])", text)
        return fallback_match.group(1) if fallback_match else None

    def extract_keyword(self, message: str) -> str:
        text = message or ""
        text = re.sub(r"\d{1,2}\s*(?:个)?月", " ", text)
        text = re.sub(r"\d{1,2}\s*岁", " ", text)
        text = re.sub(r"\d+(?:\.\d+)?\s*(?:-|到|至|~)\s*\d+(?:\.\d+)?\s*(?:元|块)?", " ", text)
        text = re.sub(
            r"(?:预算|价格|不超过|低于)\s*\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:以内|以下|之内|左右)?|"
            r"\d+(?:\.\d+)?\s*(?:元|块)?\s*(?:以内|以下|之内|左右|以上|起)",
            " ",
            text,
        )
        text = text.translate(str.maketrans({ch: " " for ch in "，,。.!！?？、；;：:（）()【】[]"}))
        for word in ("推荐", "想买", "适合", "宝宝", "请问", "有没有", "新生儿", "和", "之间"):
            text = text.replace(word, " ")
        return " ".join(text.split())[:80] or "母婴用品"

    def extract_baby_age_month(self, message: str) -> int | None:
        """从自然语言里提取宝宝月龄，作为导购工具的补充槽位。"""
        text = message or ""
        month_match = re.search(r"(\d{1,2})\s*(?:个)?月", text)
        if month_match:
            return self._clamp_age(int(month_match.group(1)))

        year_match = re.search(r"(\d{1,2})\s*岁", text)
        if year_match:
            return self._clamp_age(int(year_match.group(1)) * 12)

        if "新生儿" in text or "刚出生" in text:
            return 0
        return None

    def extract_price_range(self, message: str) -> tuple[Decimal | None, Decimal | None]:
        """提取预算上下限，优先满足“200以内、100到300”等常见导购表达。"""
        text = message or ""
        between_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|到|至|~)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?", text)
        if between_match:
            lower = Decimal(between_match.group(1))
            upper = Decimal(between_match.group(2))
            return (min(lower, upper), max(lower, upper))

        max_match = re.search(
            r"(?:预算|价格|不超过|低于)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?|"
            r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:以内|以下|之内)",
            text,
        )
        if max_match:
            return (None, Decimal(max_match.group(1) or max_match.group(2)))

        min_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:以上|起)", text)
        if min_match:
            return (Decimal(min_match.group(1)), None)
        return (None, None)

    def _contains_any(self, text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    def _clamp_age(self, month: int) -> int:
        return max(0, min(month, 72))
