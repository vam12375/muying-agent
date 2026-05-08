import unittest

from app.rag import build_retrieval_bundle


class RagSourcesTest(unittest.TestCase):
    def test_builds_citations_from_knowledge_and_products(self) -> None:
        bundle = build_retrieval_bundle(
            query="纸尿裤 红屁屁",
            knowledge=[
                {
                    "id": 12,
                    "title": "红屁屁护理指南",
                    "summary": "保持干爽，及时更换纸尿裤。",
                    "content": "护理正文",
                }
            ],
            products=[
                {
                    "productId": 88,
                    "productName": "柔软纸尿裤 M 码",
                    "priceNew": "129.00",
                    "stock": 20,
                }
            ],
        )

        self.assertEqual(bundle.query, "纸尿裤 红屁屁")
        self.assertEqual([source.type for source in bundle.sources], ["KNOWLEDGE", "PRODUCT"])
        self.assertEqual(bundle.sources[0].source_id, "12")
        self.assertEqual(bundle.sources[0].title, "红屁屁护理指南")
        self.assertIn("保持干爽", bundle.sources[0].snippet)
        self.assertEqual(bundle.sources[1].source_id, "88")
        self.assertIn("柔软纸尿裤", bundle.format_answer_suffix())

    def test_answer_suffix_is_empty_without_sources(self) -> None:
        bundle = build_retrieval_bundle(query="未知", knowledge=[], products=[])

        self.assertEqual(bundle.sources, [])
        self.assertEqual(bundle.format_answer_suffix(), "")
