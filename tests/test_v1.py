import ast
import inspect
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main


class TestV1Contracts(unittest.TestCase):
    def test_exact_twenty_sectors(self):
        self.assertEqual(20, len(main.V1_SECTORS))
        self.assertEqual(20, len(set(main.V1_SECTORS)))

    def test_deadline_has_single_constant(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("RUN_DEADLINE_SECONDS = _env_int"))
        self.assertNotIn('_env_int("RUN_DEADLINE_SECONDS",1500)', source)

    def test_single_pipeline_entrypoints(self):
        self.assertIs(main.run_diagnose, main.run_once)
        src = inspect.getsource(main.v1_main)
        self.assertIn("return run_once(mode=\"diagnose\")", src)
        self.assertIn("return run_once(mode=\"dry-run\")", src)
        self.assertNotIn("v4_", src)

    def test_no_legacy_pipeline_symbols_remain(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        for symbol in ("v3_run_once", "v3_self_test", "v4_run_once", "v4_diagnose", "v4_self_test", "V4_SECTORS"):
            self.assertNotIn(symbol, source)

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

    def test_embedded_agent_schema_matches_repo_schema(self):
        schema = json.loads(Path("schemas/exa_agent_hard_case_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(schema, main.AGENT_HARD_CASE_SCHEMA)
        self.assertEqual(5, len(schema["properties"]))

    def test_prompt_fallback_exists(self):
        self.assertTrue(main.PROMPT_FALLBACKS["exa_sector_discovery_v1.txt"])
        with patch.object(Path, "exists", return_value=False):
            prompt = main._v1_prompt("exa_sector_discovery_v1.txt")
        self.assertTrue(prompt)

    def test_null_story_values_are_normalized(self):
        story = main.v1_normalize_story({"image": None, "sources": None, "key_points": None, "tags": None, "people": None})
        self.assertEqual({}, story["image"])
        self.assertEqual([], story["sources"])
        self.assertEqual([], story["key_points"])
        self.assertEqual([], story["tags"])
        self.assertEqual([], story["people"])

    def test_json_safe_serializes_datetime_for_ai_payloads(self):
        value = {"published": datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc), "nested": [datetime(2026, 9, 23, tzinfo=timezone.utc)]}
        out = main.json_safe(value)
        self.assertEqual("2026-09-22T12:30:00+00:00", out["published"])
        self.assertEqual("2026-09-23T00:00:00+00:00", out["nested"][0])

    def test_v1_editorialize_payload_is_json_serializable_with_datetime_candidate(self):
        class FakeAI:
            available = True
            fatal = False
            last_error = ""
            def json(self, task, system, user, schema, **kwargs):
                json.loads(user)
                return {"headline": "A Proper Evergreen Sports Headline", "deck": "", "hook": "", "body": "This is a sufficiently long evergreen body that explains the verified sports knowledge without using current news language or unsupported claims in the generated publication.", "key_points": ["One", "Two", "Three"], "why_it_matters": "Useful context.", "caption": "", "hashtags": ["#Sports"], "angle": "knowledge", "image_index": 0, "image_reason": ""}
        c = {"sector": "Sport Origin", "normalized_subject": "Example", "central_knowledge_unit": "Example origin", "central_claim": "Founded in 1901", "published": datetime(2026, 9, 22, tzinfo=timezone.utc), "sources": [{"name": "Source", "url": "https://example.com/a", "grade": "A"}]}
        out = main.v1_editorialize(FakeAI(), c, "Verified evidence from an authoritative source.", [])
        self.assertIsInstance(out, dict)

    def test_v1_rank_request_is_capped_at_twenty_and_compact(self):
        captured = {}
        class FakeAI:
            available = True
            fatal = False
            last_error = ""
            def json(self, task, system, user, schema, **kwargs):
                captured["user"] = user
                captured["kwargs"] = kwargs
                rows = json.loads(user)
                return {"rankings": [{"post_number": i + 1, "score": 100 - i} for i in range(len(rows))]}
        candidates = [{"sector": sec, "normalized_subject": f"S{i}", "central_knowledge_unit": f"K{i}", "central_claim": f"C{i}", "grade": "A", "highlights": ["x" * 500], "image": "https://example.com/i.jpg"} for i, sec in enumerate(main.V1_SECTORS)]
        rows = main.v1_rank(FakeAI(), candidates, set())
        payload = json.loads(captured["user"])
        self.assertEqual(20, len(payload))
        self.assertLessEqual(captured["kwargs"]["max_tokens"], 900)
        self.assertLessEqual(max(len(json.dumps(x)) for x in payload), 900)
        self.assertEqual(20, len(rows))

    def test_coverage_detects_same_knowledge(self):
        candidate = {"sector": "Sport Origin", "normalized_subject": "Football origins", "central_knowledge_unit": "codified association football origins", "central_claim": "Football laws codified in 1863", "sources": [{"url": "https://example.com"}]}
        coverage = {"records": [{"content_id": "x", "sector": "Sport Origin", "normalized_subject": "football origins", "central_knowledge_unit": "codified association football origins", "central_claim": "Football laws codified in 1863", "source_urls": [], "fingerprints": []}]}
        status, _, score = main.coverage_match(candidate, coverage)
        self.assertEqual("duplicate", status)
        self.assertGreaterEqual(score, 1)

    def test_coverage_allows_related_topic(self):
        candidate = {"sector": "Interesting Sports Fact", "normalized_subject": "football goal posts", "central_knowledge_unit": "football pitch dimensions", "central_claim": "goal width", "sources": [{"url": "https://example.com"}]}
        coverage = {"records": [{"content_id": "x", "sector": "Sport Origin", "normalized_subject": "football origins", "central_knowledge_unit": "codified association football origins", "central_claim": "Football laws codified in 1863", "source_urls": [], "fingerprints": []}]}
        status, _, _ = main.coverage_match(candidate, coverage)
        self.assertIn(status, {"new", "related"})

    def test_publication_id_is_stable(self):
        self.assertEqual(main.publication_id("2026-09-22-AM", "evergreen", "Football origins"), main.publication_id("2026-09-22-AM", "evergreen", "Football origins"))

    def test_idempotency_publishes_only_once(self):
        state = {"publication": {}}
        calls = []
        def pub():
            calls.append(1)
            return {"ok": True, "result": {"message_id": 123}}
        first = main.publish_with_idempotency(state, "x", pub)
        second = main.publish_with_idempotency(state, "x", pub)
        self.assertEqual(1, len(calls))
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("idempotent"))

    def test_image_validator_accepts_https_in_offline_mode(self):
        self.assertTrue(main.validate_image_candidate({"url": "https://example.com/image.jpg"}, do_network=False))

    def test_live_event_identity_collapses_source_time_drift(self):
        a = main.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
        b = main.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A-B", start=datetime(2026, 9, 23, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(main.live_event_identity(a), main.live_event_identity(b))

    def test_live_rich_message_is_native_compact_table(self):
        event = main.make_event(sport="Football", league="Test", home="A", away="B", name="A vs B", start=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
        rich = main.live_rich("next", date(2026, 9, 23), [event])
        table = rich["blocks"][0]
        self.assertEqual("table", table["type"])
        self.assertTrue(table["is_bordered"] and table["is_striped"] and table["is_compact"])
        self.assertEqual(2, len(table["cells"]))

    def test_evergreen_rich_message_uses_real_photo_block(self):
        story = {"sector": "Interesting Sports Fact", "headline": "Why This Sports Measurement Still Matters", "deck": "A concise explanation.", "body": "This is a verified evergreen story with enough detail to satisfy the editorial body length requirements in a real post.", "key_points": ["Point one", "Point two", "Point three"], "why_it_matters": "It explains a useful piece of sports knowledge.", "sources": [("Source", "https://example.com")], "tags": ["#SportsFacts"], "image": {"url": "https://example.com/image.jpg"}}
        rich = main.evergreen_rich(story)
        self.assertEqual("photo", rich["blocks"][0]["type"])

    def test_current_news_filter_rejects_obvious_live_copy(self):
        self.assertIsNotNone(main.V1_CURRENT_RX.search("today's upcoming match preview"))

    def test_visual_publisher_never_downgrades_to_plain_text(self):
        story = main.v1_normalize_story({"sector": "Sport Discovery", "headline": "A Proper Evergreen Sports Headline", "body": "This is a verified evergreen body with enough words to demonstrate the visual publisher path safely.", "key_points": ["One", "Two", "Three"], "why_it_matters": "Useful.", "sources": [("Source", "https://example.com")], "tags": ["#Sports"], "image": {"url": "https://example.com/photo.jpg"}})
        calls = []
        with patch.object(main, "send_rich", return_value={"ok": False, "description": "400 rich message rejected"}), patch.object(main, "tg_call", side_effect=lambda method, data=None, file_path="", file_field="photo": calls.append((method, data, file_path)) or {"ok": True, "result": {"message_id": 123}}):
            out = main.v1_publish_evergreen(story)
        self.assertTrue(out["ok"])
        self.assertEqual("sendPhoto", calls[0][0])
        self.assertNotIn("sendMessage", [x[0] for x in calls])

    def test_production_orchestrator_dry_run_reaches_live_pair(self):
        fixed_now = datetime(2026, 9, 22, 10, 0, tzinfo=main.BD_TZ)
        state = {"posts": []}
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_knowledge_unit": f"Knowledge {i}",
                "central_claim": "A verified claim", "topic": f"Subject {i}",
                "source_url": f"https://example.com/{i}", "source": "Example", "grade": "A",
                "highlights": ["Verified evidence about this subject."], "image": "",
                "sources": [{"name": "Example", "url": f"https://example.com/{i}", "grade": "A"}],
                "research_images": [],
            })
        ranked = [{"post_number": i + 1, "score": 100 - i, "reason": "", "_candidate_pool": candidates} for i in range(20)]
        editorial = {"headline": "How Football Laws Became Written Rules", "deck": "", "hook": "", "body": "Football's laws became more standardized after clubs agreed on a written code. The milestone helped distinguish association football from other football traditions and gave the sport a common framework. That shared framework could travel between clubs and countries over time, making the game easier to recognize across different communities. It also created a reference point for later rule development and made it easier for clubs to compare their practices.", "key_points": ["Written rules created common expectations", "The code shaped later development", "The change made comparison easier"], "why_it_matters": "It explains a useful piece of sports history.", "caption": "", "hashtags": ["#Sports"], "angle": "history", "image_index": 0, "image_reason": ""}
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), patch.object(main, "validate_image_candidate", return_value=False), \
             patch.object(main, "V1_POST_DELAY_SECONDS", 0), \
             patch.object(main, "live_pair", return_value=({"target_date": "2026-09-23"}, {"target_date": "2026-09-21"}, [{"event_id": "e1"}], [{"event_id": "e2"}])):
            rc = main.run_once(mode="dry-run")
        self.assertEqual(0, rc)
        self.assertEqual(10, len([p for p in state["posts"] if p.get("desk") == "evergreen_v1"]))


    def test_cerebras_rate_limit_storm_hits_run_cap(self):
        main.REPORT.reset()
        ai = main.AIClient()
        ai.available = True
        ai.MAX_CALLS_PER_RUN = 6
        calls = []

        def fake_http(*args, **kwargs):
            calls.append(1)
            return main.Resp(429, data={"error": {"type": "rate_limit_error", "message": "rate limited"}},
                              text_='{"error":{"type":"rate_limit_error","message":"rate limited"}}',
                              error="HTTP 429: rate limited", headers={"Retry-After": "0"})

        with patch.object(main, "http", side_effect=fake_http), patch.object(main, "sleep", return_value=None):
            first = ai.json("storm", "system", "{}", main.OBJ(value=main.STR), max_tokens=256)
            second = ai.json("storm", "system", "{}", main.OBJ(value=main.STR), max_tokens=256)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(6, len(calls))
        self.assertEqual(6, ai.calls_this_run)
        self.assertIn("per-run call cap", ai.last_error)
        self.assertGreaterEqual(ai.rate_limit_streak, 5)

    def test_partial_evergreen_volume_does_not_block_live_pair(self):
        fixed_now = datetime(2026, 9, 22, 10, 0, tzinfo=main.BD_TZ)
        state = {"posts": []}
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS[:5]):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_knowledge_unit": f"Knowledge {i}",
                "central_claim": "A verified claim", "topic": f"Subject {i}",
                "source_url": f"https://example.com/{i}", "source": "Example", "grade": "A",
                "highlights": ["Verified evidence about this subject."], "image": "",
                "sources": [{"name": "Example", "url": f"https://example.com/{i}", "grade": "A"}],
                "research_images": [], "research_evidence": "Verified evidence about this subject.",
            })
        ranked = [{"post_number": i + 1, "score": 100 - i, "reason": "", "_candidate_pool": candidates} for i in range(5)]
        editorial = {"headline": "How Football Laws Became Written Rules", "deck": "", "hook": "", "body": "Football's laws became more standardized after clubs agreed on a written code. The milestone helped distinguish association football from other football traditions and gave the sport a common framework. That shared framework could travel between clubs and countries over time, making the game easier to recognize across different communities. It also created a reference point for later rule development and made it easier for clubs to compare their practices.", "key_points": ["Written rules created common expectations", "The code shaped later development", "The change made comparison easier"], "why_it_matters": "It explains a useful piece of sports history.", "caption": "", "hashtags": ["#Sports"], "angle": "history", "image_index": 0, "image_reason": ""}
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), patch.object(main, "validate_image_candidate", return_value=False), \
             patch.object(main, "V1_POST_DELAY_SECONDS", 0), \
             patch.object(main, "live_pair", return_value=({"target_date": "2026-09-23"}, {"target_date": "2026-09-21"}, [{"event_id": "e1"}], [{"event_id": "e2"}])):
            rc = main.run_once(mode="dry-run")
        self.assertEqual(0, rc)
        self.assertEqual(5, len([p for p in state["posts"] if p.get("desk") == "evergreen_v1"]))

    def test_missing_sector_is_partial_credit_not_run_abort(self):
        fixed_now = datetime(2026, 9, 22, 10, 0, tzinfo=main.BD_TZ)
        state = {"posts": []}
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS[:-1]):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_knowledge_unit": f"Knowledge {i}",
                "central_claim": "A verified claim", "topic": f"Subject {i}",
                "source_url": f"https://example.com/{i}", "source": "Example", "grade": "A",
                "highlights": ["Verified evidence about this subject."], "image": "",
                "sources": [{"name": "Example", "url": f"https://example.com/{i}", "grade": "A"}],
                "research_images": [], "research_evidence": "Verified evidence about this subject.",
            })
        ranked = [{"post_number": i + 1, "score": 100 - i, "reason": "", "_candidate_pool": candidates} for i in range(len(candidates))]
        editorial = {"headline": "How Football Laws Became Written Rules", "deck": "", "hook": "", "body": "Football's laws became more standardized after clubs agreed on a written code. The milestone helped distinguish association football from other football traditions and gave the sport a common framework. That shared framework could travel between clubs and countries over time, making the game easier to recognize across different communities. It also created a reference point for later rule development and made it easier for clubs to compare their practices.", "key_points": ["Written rules created common expectations", "The code shaped later development", "The change made comparison easier"], "why_it_matters": "It explains a useful piece of sports history.", "caption": "", "hashtags": ["#Sports"], "angle": "history", "image_index": 0, "image_reason": ""}
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), patch.object(main, "validate_image_candidate", return_value=False), \
             patch.object(main, "V1_POST_DELAY_SECONDS", 0), \
             patch.object(main, "live_pair", return_value=({"target_date": "2026-09-23"}, {"target_date": "2026-09-21"}, [{"event_id": "e1"}], [{"event_id": "e2"}])):
            rc = main.run_once(mode="dry-run")
        self.assertEqual(0, rc)
        self.assertEqual(10, len([p for p in state["posts"] if p.get("desk") == "evergreen_v1"]))
        self.assertNotIn(main.V1_SECTORS[-1], {p.get("sector") for p in state["posts"] if p.get("desk") == "evergreen_v1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
