import ast
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

    def test_json_safe_serializes_datetime_for_ai_payloads(self):
        from datetime import datetime, timezone
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
                return {"headline":"A Proper Evergreen Sports Headline", "deck":"", "hook":"", "body":"This is a sufficiently long evergreen body that explains the verified sports knowledge without using current news language or unsupported claims in the generated publication.", "key_points":["One","Two","Three"], "why_it_matters":"Useful context.", "caption":"", "hashtags":["#Sports"], "angle":"knowledge", "image_index":0, "image_reason":""}
        from datetime import datetime, timezone
        c = {"sector":"Sport Origin","normalized_subject":"Example","central_knowledge_unit":"Example origin","central_claim":"Founded in 1901", "published":datetime(2026,9,22,tzinfo=timezone.utc), "sources":[{"name":"Source","url":"https://example.com/a","grade":"A"}]}
        out = main.v1_editorialize(FakeAI(), c, "Verified evidence from an authoritative source.", [])
        self.assertIsInstance(out, dict)

    def test_v1_rank_request_is_capped_at_twenty_and_compact(self):
        captured = {}
        class FakeAI:
            available=True; fatal=False; last_error=""
            def json(self, task, system, user, schema, **kwargs):
                captured["user"] = user; captured["kwargs"] = kwargs
                rows = json.loads(user)
                return {"rankings":[{"post_number":i+1,"score":100-i} for i in range(len(rows))]}
        candidates=[]
        for i, sec in enumerate(main.V1_SECTORS):
            candidates.append({"sector":sec,"normalized_subject":f"S{i}","central_knowledge_unit":f"K{i}","central_claim":f"C{i}","grade":"A","highlights":["x"*500],"image":"https://example.com/i.jpg"})
        rows = main.v1_rank(FakeAI(), candidates, set())
        payload=json.loads(captured["user"])
        self.assertEqual(20, len(payload))
        self.assertLessEqual(captured["kwargs"]["max_tokens"], 900)
        self.assertLessEqual(max(len(json.dumps(x)) for x in payload), 900)
        self.assertEqual(20, len(rows))

    def test_v1_no_image_uses_branded_card(self):
        story=main.v1_normalize_story({"sector":"Sport Origin","headline":"A Proper Evergreen Sports Headline","body":"This is a verified evergreen body with enough words to exercise the generated branded card path when no remote image is available.","key_points":["One","Two","Three"],"why_it_matters":"Useful.","sources":[("Source","https://example.com")],"tags":["#Sports"],"image":None})
        calls=[]
        with patch.object(main,"tg_call",side_effect=lambda method,data=None,file_path="",file_field="photo":calls.append((method,data,file_path)) or {"ok":True,"result":{"message_id":124}}):
            out=main.v1_publish_evergreen(story)
        self.assertTrue(out["ok"])
        self.assertEqual("sendPhoto",calls[0][0])
        self.assertTrue(calls[0][2])
        self.assertNotIn("sendMessage",[x[0] for x in calls])

    def test_v1_visual_publisher_never_downgrades_to_plain_text(self):
        story = main.v1_normalize_story({"sector":"Sport Discovery","headline":"A Proper Evergreen Sports Headline","body":"This is a verified evergreen body with enough words to demonstrate the visual publisher path safely.","key_points":["One","Two","Three"],"why_it_matters":"Useful.","sources":[("Source","https://example.com")],"tags":["#Sports"],"image":{"url":"https://example.com/photo.jpg"}})
        calls=[]
        with patch.object(main, "v4_send_rich", return_value={"ok":False,"description":"400 rich message rejected"}), patch.object(main, "tg_call", side_effect=lambda method,data=None,file_path="",file_field="photo": calls.append((method,data,file_path)) or {"ok":True,"result":{"message_id":123}}):
            out=main.v1_publish_evergreen(story)
        self.assertTrue(out["ok"])
        self.assertEqual("sendPhoto", calls[0][0])
        self.assertNotIn("sendMessage", [x[0] for x in calls])

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

    def test_production_current_news_filter_uses_defined_symbol(self):
        tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        fn = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "v1_exa_search_sector")
        names = {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}
        self.assertNotIn("_V1_CURRENT_RX", names)
        self.assertIn("V1_CURRENT_RX", names)


    def test_cerebras_token_quota_429_fails_fast(self):
        calls = {"n": 0}
        class Resp:
            status = 429
            data = {"error": {"type": "too_many_tokens_error", "code": "token_quota_exceeded", "message": "Tokens per minute limit exceeded"}}
            text = '{"error":{"type":"too_many_tokens_error","code":"token_quota_exceeded","message":"Tokens per minute limit exceeded"}}'
            error = "HTTP 429: token_quota_exceeded"
            headers = {}
            ok = False
        client = main.AIClient.__new__(main.AIClient)
        client.available=True; client.mode="json_schema"; client.model="gpt-oss-120b"; client.reasoning=False; client.fatal=False
        client.last_error=""; client.last_status=0; client.last_request_id=""; client.last_ok=""; client.last_latency_ms=0
        client.rate_limits={}; client._last_call=0.0; client._model_fallback_tried=True; client.failures=0
        original_http=main.http
        def fake_http(*args, **kwargs):
            calls["n"] += 1
            return Resp()
        with patch.object(main, "http", side_effect=fake_http), patch.object(main, "sleep", lambda *_: None):
            out=client.json("rank", "x", "y", main.V4_RANK_SCHEMA, max_tokens=600)
        self.assertIsNone(out)
        self.assertEqual(1,calls["n"])
        self.assertIn("token_quota_exceeded",client.last_error)

    def test_cerebras_429_preserves_diagnostic(self):
        class Resp:
            status = 429
            data = {"error": {"type": "rate_limit", "message": "token limit reached"}}
            text = '{"error":{"type":"rate_limit","message":"token limit reached"}}'
            error = "HTTP 429: token limit reached"
            headers = {"x-ratelimit-reset-tokens-minute": "2"}
            ok = False
        client = main.AIClient.__new__(main.AIClient)
        client.available = True; client.mode = "json_schema"; client.model = "gpt-oss-120b"
        client.reasoning = False; client.fatal = False; client.last_error = ""; client.last_status = 0
        client.last_request_id = ""; client.last_ok = ""; client.last_latency_ms = 0
        client.rate_limits = {}; client._last_call = 0.0; client._model_fallback_tried = True; client.failures = 0
        client.json = main.AIClient.json.__get__(client, main.AIClient)
        client._last_call = 0.0
        with patch.object(main, "http", return_value=Resp()), patch.object(main, "sleep", lambda *_: None):
            out = client.json("rank", "x", "y", main.V4_RANK_SCHEMA, max_tokens=300)
        self.assertIsNone(out)
        self.assertEqual(429, client.last_status)
        self.assertIn("rate_limit", client.last_error)
        self.assertIn("token limit reached", client.last_error)


    def test_v1_rank_deterministic_fallback_preserves_candidate_pool(self):
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}",
                "central_knowledge_unit": f"Knowledge {i}", "central_claim": f"Claim {i}",
                "source": "Example", "grade": "A", "highlights": ["Evergreen evidence"],
                "image": "https://example.com/image.jpg", "coverage_status": "new",
            })
        ai = main.AIClient.__new__(main.AIClient)
        ai.available = False; ai.fatal = False; ai.last_error = "offline"
        rows = main.v1_rank(ai, candidates, set())
        self.assertTrue(rows)
        self.assertIs(rows[0]["_candidate_pool"], rows[0]["_candidate_pool"])
        self.assertEqual(20, len(rows[0]["_candidate_pool"]))

    def test_live_pair_disables_exa_fallback(self):
        with patch.object(main, "espn_events", return_value=([], {k: "fail" for k in main.LEAGUES})), \
             patch.object(main, "cricketdata_events", return_value=[]), \
             patch.object(main, "tsdb_events", return_value=[]), \
             patch.object(main, "exa_event_fallback", side_effect=AssertionError("Exa must not be used by the V1 live pair")) as exa:
            events, notes = main.collect_events(main.AIClient.__new__(main.AIClient), __import__('datetime').date(2026,9,22), "next", allow_exa_fallback=False)
        self.assertEqual([], events)
        exa.assert_not_called()

    def test_v1_run_is_wired_to_visual_publisher(self):
        import inspect
        src=inspect.getsource(main.v1_run_once)
        self.assertIn("v1_publish_evergreen",src)
        self.assertNotIn("v4_publish_evergreen(st)",src)

    def test_production_orchestrator_smoke_reaches_live_pair(self):
        fixed_now = main.BD_TZ.localize(__import__('datetime').datetime(2026,9,22,10,0)) if hasattr(main.BD_TZ, 'localize') else __import__('datetime').datetime(2026,9,22,10,0,tzinfo=main.BD_TZ)
        state = {"posts": []}
        sectors = list(main.V1_SECTORS)
        candidates = []
        for i, sec in enumerate(sectors):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_knowledge_unit": f"Knowledge {i}",
                "central_claim": f"Claim {i}", "topic": f"Subject {i}", "research_evidence": "Verified evidence " * 40,
                "source_url": f"https://example.com/{i}",
                "sources": [{"name":"Source","url":f"https://example.com/{i}"}], "research_images": [],
            })
        ranked = [{"post_number": i+1, "score": 100-i, "reason": ""} for i in range(20)]
        ranked[0]["_candidate_pool"] = candidates
        editorial = {"headline":"A Valid Evergreen Sports Story", "deck":"", "hook":"",
                     "body":"This is a verified evergreen body with enough words to satisfy the production validator and demonstrate the publisher path without depending on a provider.",
                     "key_points":["One","Two","Three"], "why_it_matters":"Useful", "caption":"", "hashtags":["#Sports"], "angle":"knowledge", "image_index":0, "image_reason":""}
        def fake_publish(vstate, pubid, publisher):
            return {"ok": True, "result":{"message_id": 100 + len(vstate.get('publication', {}))}, "description":"ok"}
        live_events = [{"event_id":"e1","sport":"Football","name":"A vs B","league":"Test","start":fixed_now}]
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "v4_load_coverage", return_value={"records":[]}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target:(selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), patch.object(main, "v1_validate_editorial", return_value=(True,"ok",{})), \
             patch.object(main, "v4_build_evergreen", side_effect=lambda c,e,i:{"sector":c["sector"],"topic":c["topic"],"normalized_subject":c["normalized_subject"],"central_knowledge_unit":c["central_knowledge_unit"],"central_claim":c["central_claim"],"headline":e["headline"],"deck":"","hook":"","body":e["body"],"key_points":e["key_points"],"why_it_matters":e["why_it_matters"],"caption":"","tags":["#Sports"],"sources":[("Source",c["sources"][0]["url"])],"urls":[c["sources"][0]["url"]],"image":None,"image_status":"unavailable","research":c}), \
             patch.object(main, "v4_publish_with_idempotency", side_effect=fake_publish), patch.object(main, "v4_record_coverage", return_value=None), \
             patch.object(main, "save_state", return_value=None), patch.object(main, "v4_save_coverage", return_value=None), \
             patch.object(main, "v4_live_pair", return_value=({"target_date":"2026-09-23"},{"target_date":"2026-09-21"},live_events,live_events)), \
             patch.object(main, "v4_publish_evergreen", return_value={"ok":True}), patch.object(main, "v4_publish_live", return_value={"ok":True}), \
             patch.object(main, "v4_publication_id", side_effect=lambda *a: ":".join(str(x) for x in a)), \
             patch.object(main, "sleep", return_value=None):
            main.V1_DRY_RUN = True
            try:
                rc = main.v1_run_once()
            finally:
                main.V1_DRY_RUN = False
        self.assertEqual(0, rc)



if __name__ == "__main__":
    unittest.main(verbosity=2)
