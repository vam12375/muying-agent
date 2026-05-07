import unittest
from decimal import Decimal

from app.intent import IntentClassifier


class IntentClassifierTest(unittest.TestCase):
    def test_extracts_shopping_slots_without_polluting_keyword(self) -> None:
        classifier = IntentClassifier()
        message = "宝宝8个月，预算200以内，推荐纸尿裤和护臀膏"

        self.assertEqual(classifier.extract_baby_age_month(message), 8)
        self.assertEqual(classifier.extract_price_range(message), (None, Decimal("200")))
        self.assertEqual(classifier.extract_keyword(message), "纸尿裤 护臀膏")

    def test_extracts_newborn_age_and_price_range(self) -> None:
        classifier = IntentClassifier()
        message = "新生儿奶瓶，100到300元之间"

        self.assertEqual(classifier.extract_baby_age_month(message), 0)
        self.assertEqual(classifier.extract_price_range(message), (Decimal("100"), Decimal("300")))

    def test_extracts_embedded_order_no_when_user_asks_logistics(self) -> None:
        classifier = IntentClassifier()
        message = "帮我查询这个订单到哪里了；OD1777034666940cc7ee7"

        self.assertEqual(classifier.classify(message), "ORDER_QUERY")
        self.assertEqual(classifier.extract_order_no(message), "OD1777034666940cc7ee7")
