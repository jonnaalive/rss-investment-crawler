import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import classify, clean_text, discord_payload, parse_feed, translate_summary


class CrawlerTests(unittest.TestCase):
    def test_parse_rss(self):
        payload = b"""<rss><channel><item><title>Natural gas demand rises</title><link>https://example.com/a</link><guid>x1</guid><description>Data center power</description></item></channel></rss>"""
        result = parse_feed(payload, {"id": "test", "name": "Test"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Natural gas demand rises")

    def test_relative_link_is_resolved(self):
        payload = b"""<rss><channel><item><title>Release</title><link>/press/a</link></item></channel></rss>"""
        result = parse_feed(payload, {"id": "test", "name": "Test", "url": "https://example.com/rss.xml"})
        self.assertEqual(result[0]["url"], "https://example.com/press/a")

    def test_classification(self):
        article = {"title": "GE Vernova expands gas turbine capacity", "summary": "Data center electricity demand", "url": "x"}
        config = {"themes": {"AI power": ["data center", "gas turbine"]}, "companies": {"GEV": ["ge vernova"]}}
        result = classify(article, config, 3)
        self.assertEqual(result["companies"], ["GEV"])
        self.assertEqual(result["themes"], ["AI power"])
        self.assertEqual(result["score"], 10)

    def test_html_cleanup(self):
        self.assertEqual(clean_text("<p>A &amp; B</p>"), "A & B")

    def test_discord_payload_has_investment_context(self):
        item = {
            "title": "Refinery exports rise", "url": "https://example.com/a",
            "source": "EIA", "summary": "Exports reached a record.",
            "themes": ["정유"], "companies": [], "score": 5,
        }
        config = {"theme_context": {"정유": {
            "related_companies": ["VLO", "MPC"],
            "why_it_matters": "정유 마진에 연결됩니다.",
            "check": "크랙스프레드 확인",
        }}}
        content = discord_payload([item], [], config)["content"]
        self.assertIn("무슨 일", content)
        self.assertIn("왜 보나", content)
        self.assertIn("VLO, MPC", content)
        self.assertIn("확인 행동", content)

    def test_translation_without_key_falls_back(self):
        self.assertEqual(translate_summary("Natural gas demand rose.", ""), "Natural gas demand rose.")


if __name__ == "__main__":
    unittest.main()
