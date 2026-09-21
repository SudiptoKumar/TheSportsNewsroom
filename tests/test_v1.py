import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import main


class TestV1Contracts(unittest.TestCase):
    def test_exact_twenty_sectors(self):
        self.assertEqual(20, len(main.V1_SECTORS))
        self.assertEqual(20, len(set(main.V1_SECTORS)))

    def test_discovery_search_has_no_output_schema(self):
        calls = []
        class R:
            ok = True
            status = 200
            error = ""
            data = {"results": [], "requestId": "test"}
        def fake(url, body, **kwargs):
            calls.append((url, body))
            return R()
        with patch.object(main, "EXA_API_KEY", "test"), patch.object(main, "_v1_exa_request", fake):
            main.v1_exa_search_sector("Sport Discovery", date(2026, 9, 22), set(), num=6)
        self.assertEqual(1, len(calls))
        url, body = calls[0]
        self.assertTrue(url.endswith("/search"))
        self.assertNotIn("outputSchema", body)
        self.assertEqual(6, body["numResults"])
        self.assertEqual({"highlights": True}, body["contents"])
        self.assertTrue(body["moderation"])

    def test_auto_search_omits_type(self):
        calls = []
        class R:
            ok = True
            status = 200
            error = ""
            data = {"results": []}
        def fake(url, body, **kwargs):
            calls.append(body)
            return R()
        with patch.object(main, "EXA_API_KEY", "test"), patch.object(main, "_v1_exa_request", fake):
            main.v1_exa_search_query("specific verification query", date(2026, 9, 22), set(), mode="auto", num=4)
        self.assertNotIn("type", calls[0])

    def test_network_url_preserves_absolute_scheme_for_provider_calls(self):
        self.assertEqual("https://example.com/a/", main.v1_network_url("example.com/a/?utm_source=x"))

    def test_contents_is_batch_and_cache_friendly(self):
        calls = []
        class R:
            ok = True
            status = 200
            error = ""
            data = {"results": [], "statuses": []}
        def fake(url, body, **kwargs):
            calls.append((url, body))
            return R()
        with patch.object(main, "EXA_API_KEY", "test"), patch.object(main, "_v1_exa_request", fake):
            pages, status = main.v1_contents_for_urls(["https://example.com/a", "https://example.com/a/"])
        self.assertEqual({}, pages)
        self.assertEqual("ok", status)
        self.assertEqual(1, len(calls[0][1]["urls"]))
        self.assertEqual(720, calls[0][1]["maxAgeHours"])
        self.assertTrue(calls[0][1]["text"])
        self.assertTrue(calls[0][1]["highlights"])

    def test_agent_schema_is_small(self):
        schema = json.loads(Path("schemas/exa_agent_hard_case_v1.json").read_text())
        self.assertLessEqual(len(schema["properties"]), 10)
        self.assertEqual(5, len(schema["properties"]))

    def test_null_story_values_are_normalized(self):
        story = main.v1_normalize_story({"image": None, "sources": None, "key_points": None, "tags": None, "people": None})
        self.assertEqual({}, story["image"])
        self.assertEqual([], story["sources"])
        self.assertEqual([], story["key_points"])
        self.assertEqual([], story["tags"])
        self.assertEqual([], story["people"])

    def test_null_image_cannot_crash_renderer_after_normalization(self):
        story = main.v1_normalize_story({
            "headline": "A Valid Evergreen Sports Story",
            "deck": "",
            "body": "This is a valid body for testing the safe rendering path without requiring a real image.",
            "key_points": ["One", "Two", "Three"],
            "why_it_matters": "Useful.",
            "sources": [("Source", "https://example.com")],
            "tags": ["#Sports"],
            "image": None,
        })
        rich = main.v4_evergreen_rich(story)
        self.assertTrue(rich["blocks"])
        self.assertNotEqual("photo", rich["blocks"][0]["type"])

    def test_secondary_search_uses_url_field(self):
        candidate = {
            "sector": "Sport Origin",
            "normalized_subject": "Example Origin",
            "central_knowledge_unit": "Example Origin",
            "central_claim": "origin claim",
            "research_focus": "origin",
        }
        rows = [{"url": "https://example.com/other", "title": "Example Origin", "text": "origin claim evidence", "highlights": ["origin claim evidence"], "source": "Example"}]
        with patch.object(main, "v1_exa_search_query", return_value=rows):
            out = main.v1_secondary_search(candidate, date(2026, 9, 22))
        self.assertEqual("https://example.com/other", out[0]["url"])

    def test_current_news_filter_rejects_obvious_live_copy(self):
        self.assertIsNotNone(main.V1_CURRENT_RX.search("today's upcoming match preview"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
