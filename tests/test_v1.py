import ast
import inspect
import json
import re
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main

EDITORIAL_BODY = (
    "Football's laws became more standardized after clubs agreed on a written code. "
    "The change gave the sport a shared framework and helped distinguish association football from other traditions. "
    "That framework could travel between clubs and countries, creating a common reference for later rule development "
    "and making established practices easier to compare across communities."
)
EDITORIAL_SAMPLE = {
    "headline": "How Football Laws Became Written Rules",
    "body": EDITORIAL_BODY,
    "hashtags": ["#Sports", "#History"],
    "angle": "history",
}


class TestV1Contracts(unittest.TestCase):
    def test_exact_twenty_sectors(self):
        self.assertEqual(20, len(main.V1_SECTORS))
        self.assertEqual(20, len(set(main.V1_SECTORS)))

    def test_deadline_has_single_constant(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("RUN_DEADLINE_SECONDS = _env_int"))
        self.assertNotIn('_env_int("RUN_DEADLINE_SECONDS",1500)', source)

    def test_single_pipeline_entrypoints(self):
        self.assertEqual("2.2.0", main.APP_VERSION)
        self.assertIs(main.run_diagnose, main.run_once)
        src = inspect.getsource(main.v1_main)
        self.assertIn("return run_once(mode=\"diagnose\")", src)
        self.assertIn("return run_once(mode=\"dry-run\")", src)
        self.assertNotIn("v4_", src)

    def test_no_legacy_pipeline_symbols_remain(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        for symbol in ("v3_run_once", "v3_self_test", "v4_run_once", "v4_diagnose", "v4_self_test", "V4_SECTORS"):
            self.assertNotIn(symbol, source)

    def test_single_evergreen_renderer_is_the_only_text_renderer(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("def render_evergreen_post("))
        for symbol in ("def evergreen_rich(", "def knowledge_html(", "def fit_knowledge_html(", "def source_links("):
            self.assertNotIn(symbol, source)
        self.assertEqual(1, source.count("render_evergreen_post(story)"))

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
        story = main.v1_normalize_story({"image": None, "sources": None, "tags": None, "people": None})
        self.assertEqual({}, story["image"])
        self.assertEqual([], story["sources"])
        self.assertEqual([], story["tags"])
        self.assertEqual([], story["people"])
        self.assertNotIn("key_points", story)

    def test_json_safe_serializes_datetime_for_ai_payloads(self):
        value = {"published": datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc), "nested": [datetime(2026, 9, 23, tzinfo=timezone.utc)]}
        out = main.json_safe(value)
        self.assertEqual("2026-09-22T12:30:00+00:00", out["published"])
        self.assertEqual("2026-09-23T00:00:00+00:00", out["nested"][0])

    def test_editorial_schema_is_compact_and_has_no_legacy_fields(self):
        self.assertEqual({"headline", "body", "hashtags"}, set(main.EDITORIAL_SCHEMA["properties"]))
        self.assertFalse(set(main.EDITORIAL_SCHEMA["properties"]) & {"deck", "hook", "key_points", "why_it_matters", "caption", "angle", "image_index", "image_reason"})

    def test_editorial_prompt_requests_one_40_to_60_word_paragraph(self):
        prompt = main.PROMPT_FALLBACKS["cerebras_editorial_v1.txt"]
        file_prompt = Path("prompts/cerebras_editorial_v1.txt").read_text(encoding="utf-8")
        for current in (prompt, file_prompt):
            self.assertIn("ONE short paragraph, 40-60 words", current)
            self.assertIn("No lists", current)
            self.assertIn("at most 3 relevant hashtags", current)
            self.assertIn("Do not generate or select image URLs", current)

    def test_v1_editorialize_payload_is_json_serializable_with_datetime_candidate(self):
        class FakeAI:
            available = True
            fatal = False
            last_error = ""
            def json(self, task, system, user, schema, **kwargs):
                json.loads(user)
                return {"headline": EDITORIAL_SAMPLE["headline"], "body": EDITORIAL_SAMPLE["body"], "hashtags": EDITORIAL_SAMPLE["hashtags"]}
        c = {"sector": "Sport Origin", "normalized_subject": "Example", "central_knowledge_unit": "Example origin", "central_claim": "Founded in 1901", "published": datetime(2026, 9, 22, tzinfo=timezone.utc), "sources": [{"name": "Source", "url": "https://example.com/a", "grade": "A"}]}
        out = main.v1_editorialize(FakeAI(), c, "Verified evidence from an authoritative source.")
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

    def test_live_event_identity_collapses_source_time_drift(self):
        a = main.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
        b = main.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A-B", start=datetime(2026, 9, 23, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(main.live_event_identity(a), main.live_event_identity(b))

    def test_v1_state_reasserts_live_retirement_queue_after_legacy_migration(self):
        state={"v1":{}, "v4":{"live":{"schedule":{"message_id":11,"target_date":"2026-09-24"},"results":{"message_id":12,"target_date":"2026-09-23"}}}}
        with patch.object(main, "now_bd", return_value=datetime(2026,9,23,12,tzinfo=timezone.utc)):
            live=main.v1_state(state)["live"]
        self.assertEqual([], live["retiring_message_ids"])
        self.assertEqual(11, live["schedule"]["message_id"])
        self.assertEqual(12, live["results"]["message_id"])

    def test_live_rich_has_h1_and_exactly_two_columns(self):
        event = main.make_event(sport="Football", league="Premier League", home="Arsenal", away="Chelsea", name="Arsenal vs Chelsea", start=datetime(2026, 9, 24, 18, tzinfo=timezone.utc))
        rich = main.live_rich("next", date(2026, 9, 24), [event])
        self.assertEqual("heading", rich["blocks"][0]["type"])
        self.assertEqual(1, rich["blocks"][0]["size"])
        table = rich["blocks"][2]
        self.assertEqual("table", table["type"])
        self.assertTrue(all(len(row) == 2 for row in table["cells"]))
        self.assertEqual(["GAME", "TIME"], [c["text"] for c in table["cells"][0]])

    def test_live_rich_results_uses_game_and_result_columns(self):
        event = main.make_event(sport="Football", league="Premier League", home="Arsenal", away="Chelsea", name="Arsenal vs Chelsea", start=datetime(2026, 9, 23, 18, tzinfo=timezone.utc), state="final", home_score="2", away_score="1")
        rich = main.live_rich("past", date(2026, 9, 23), [event])
        table = rich["blocks"][2]
        self.assertEqual(["GAME", "RESULT"], [c["text"] for c in table["cells"][0]])
        self.assertEqual(2, len(table["cells"]))

    def test_live_retirement_cleanup_uses_delete_messages_and_clears_pending(self):
        state = {"live": {"retiring_message_ids": [101, 102]}}
        calls = []
        def fake_tg(method, data=None, file_path="", file_field="photo"):
            calls.append((method, data))
            return {"ok": True}
        with patch.object(main, "tg_call", side_effect=fake_tg):
            ok = main.cleanup_live_retirements(state, exclude_ids=[])
        self.assertTrue(ok)
        self.assertEqual([101, 102], json.loads(calls[0][1]["message_ids"]))
        self.assertEqual("deleteMessages", calls[0][0])
        self.assertEqual([], state["live"]["retiring_message_ids"])

    def test_live_retirement_delete_messages_serializes_ids_for_telegram(self):
        live = {"retiring_message_ids": [101, 102]}
        seen = {}
        def fake_http(source, method, url, **kwargs):
            seen.update(kwargs.get("data") or {})
            return main.Resp(200, {"ok": True, "result": True})
        with patch.object(main, "http", side_effect=fake_http):
            with patch.object(main, "TELEGRAM_BOT_TOKEN", "test-token"):
                ok = main.cleanup_live_retirements({"live": live})
        self.assertTrue(ok)
        self.assertEqual([101, 102], json.loads(seen["message_ids"]))

    def test_live_retirement_cleanup_keeps_pending_on_failure(self):
        live = {"retiring_message_ids": [101, 102]}
        with patch.object(main, "tg_call", return_value={"ok": False, "description": "permission denied"}):
            ok = main.cleanup_live_retirements({"live": live})
        self.assertFalse(ok)
        self.assertEqual([101, 102], live["retiring_message_ids"])

    def test_live_rotation_queues_old_pair_then_deletes_it(self):
        state={"ai":{},"last_run_at":""}
        vstate=main._v1_default_state()
        vstate["live"]["schedule"]["message_id"]=101
        vstate["live"]["results"]["message_id"]=102
        sched={"target_date":"2026-09-24"}; res={"target_date":"2026-09-22"}
        events=[{"event_id":"n1"}]; past=[{"event_id":"p1"}]
        sent=[]; deleted=[]
        def fake_publish_with_idempotency(vs,pubid,publisher):
            sent.append(pubid)
            return {"ok":True,"result":{"message_id":201 if "live_schedule" in pubid else 202}}
        def fake_cleanup(vs, exclude_ids=()):
            deleted.append(list(vs["live"].get("retiring_message_ids", [])))
            vs["live"]["retiring_message_ids"]=[]
            return True
        with patch.object(main,"publish_with_idempotency",side_effect=fake_publish_with_idempotency), \
             patch.object(main,"cleanup_live_retirements",side_effect=fake_cleanup), \
             patch.object(main,"save_state",return_value=None), \
             patch.object(main,"utc_iso",return_value="2026-09-23T00:00:00+00:00"):
            result=main.rotate_live_pair(state,vstate,"run-1",sched,res,events,past,dry=False)
        self.assertEqual(201,vstate["live"]["schedule"]["message_id"])
        self.assertEqual(202,vstate["live"]["results"]["message_id"])
        self.assertEqual([101,102],result["retired_ids"])
        self.assertIn([101,102],deleted)
        self.assertEqual([],vstate["live"]["retiring_message_ids"])

    def test_live_rich_message_is_native_compact_table(self):
        event = main.make_event(sport="Football", league="Test", home="A", away="B", name="A vs B", start=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
        rich = main.live_rich("next", date(2026, 9, 23), [event])
        table = rich["blocks"][2]
        self.assertEqual("table", table["type"])
        self.assertTrue(table["is_bordered"] and table["is_striped"] and table["is_compact"])
        self.assertTrue(all(len(row) == 2 for row in table["cells"]))

    def test_render_evergreen_post_exact_html_contract(self):
        story={
            "headline":"How Football Laws Became Written Rules",
            "body":EDITORIAL_BODY,
            "sources":[("Example Source","https://example.com/story"),("Second Source","https://example.org/item")],
            "tags":["#Football","#History","#Games","#Extra"],
        }
        rendered=main.render_evergreen_post(story)
        html=rendered["html"]
        self.assertEqual("HTML", rendered["parse_mode"])
        self.assertEqual(html, main.tg_sanitize(html))
        plain=main.plain_text(html)
        self.assertNotIn("<", plain)
        self.assertNotIn("&lt;", plain)
        lines=html.splitlines()
        self.assertEqual("<b>How Football Laws Became Written Rules</b>", lines[0])
        self.assertEqual(main.esc(EDITORIAL_BODY), lines[2])
        self.assertTrue(lines[4].startswith("Source: "))
        self.assertTrue(lines[-1].startswith("#"))
        self.assertEqual(["#Football", "#History", "#Games"], lines[-1].split())
        self.assertLessEqual(len(lines[-1].split()),3)
        self.assertIn('<a href="https://example.com/story">Example Source</a>', html)
        visible_part=re.sub(r'href="[^"]+"', '', html)
        self.assertNotIn("https://example.com/story", visible_part)
        self.assertNotIn("https://example.org/item", visible_part)

    def test_tg_call_rejects_anchor_without_html_parse_mode(self):
        with self.assertRaises(AssertionError):
            main.tg_call("sendMessage", {"chat_id":"@test","text":'<b>Headline</b>\nSource: <a href="https://example.com">Example</a>'})

    def test_evergreen_external_photo_path_is_local_file_only(self):
        publish = inspect.getsource(main.v1_publish_evergreen)
        self.assertIn("tg_call(\"sendPhoto\",payload,chosen_path)", publish)
        self.assertIn("prepare_evergreen_media(story)", publish)
        self.assertNotIn("photo_url", publish)
        self.assertNotIn("send_rich", publish)

    def test_bing_image_parser_returns_original_murl_not_thumbnail(self):
        import html as html_mod
        meta = {
            "murl": "https://cdn.example.com/original-1920.jpg",
            "turl": "https://cdn.example.com/thumb-320.jpg",
            "purl": "https://example.com/story",
            "t": "Exact story photo",
            "desc": "Players in a historic match",
        }
        raw = f'<a class="iusc" m="{html_mod.escape(json.dumps(meta), quote=True)}"></a>'
        rows = main._parse_bing_image_results(raw, 1)
        self.assertEqual(1, len(rows))
        self.assertEqual(meta["murl"], rows[0]["image_url"])
        self.assertNotEqual(meta["turl"], rows[0]["image_url"])

    def test_source_page_meta_image_is_extracted(self):
        page = "https://example.com/story"
        raw = '<html><head><meta property="og:image" content="/images/story.jpg"><meta name="twitter:image" content="https://cdn.example.com/story.png"></head></html>'
        rows = main._extract_meta_image_candidates(page, raw)
        urls = {row["image_url"] for row in rows}
        self.assertIn("https://example.com/images/story.jpg", urls)
        self.assertIn("https://cdn.example.com/story.png", urls)

    def test_verified_image_requires_200_image_mime_and_preserves_dimensions(self):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (641, 479), "white").save(buf, format="JPEG", quality=91)
        payload = buf.getvalue()

        class Resp:
            def __init__(self, status, headers, body=b"", url="https://img.example/original.jpg"):
                self.status_code = status
                self.headers = headers
                self._body = body
                self.url = url
            def iter_content(self, chunk_size=65536):
                yield self._body

        class FakeSession:
            def head(self, *args, **kwargs):
                return Resp(200, {"Content-Type":"image/jpeg", "Content-Length":str(len(payload))})
            def get(self, *args, **kwargs):
                return Resp(200, {"Content-Type":"image/jpeg", "Content-Length":str(len(payload))}, payload)

        candidate={"image_url":"https://img.example/original.jpg","page_url":"https://example.com/story","source_name":"Example","provider":"bing_images","score":90}
        with patch.object(main, "session", return_value=FakeSession()):
            verified=main.download_verified_image(candidate)
        self.assertIsNotNone(verified)
        try:
            self.assertEqual((641,479),(verified["width"],verified["height"]))
            self.assertEqual(payload, Path(verified["path"]).read_bytes())
            self.assertEqual(len(payload), verified["bytes"])
        finally:
            if verified:
                Path(verified["path"]).unlink(missing_ok=True)

    def test_verified_image_allows_head_not_supported_when_get_is_valid(self):
        from io import BytesIO
        from PIL import Image
        buf=BytesIO(); Image.new("RGB",(500,300),"white").save(buf,format="JPEG",quality=90); payload=buf.getvalue()
        class HeadResp:
            status_code=405; headers={}; url="https://img.example/original.jpg"
        class GetResp:
            status_code=200; headers={"Content-Type":"image/jpeg","Content-Length":str(len(payload))}; url="https://img.example/original.jpg"
            def iter_content(self,chunk_size=65536): yield payload
        class FakeSession:
            def head(self,*a,**k): return HeadResp()
            def get(self,*a,**k): return GetResp()
        with patch.object(main,"session",return_value=FakeSession()):
            verified=main.download_verified_image({"image_url":"https://img.example/original.jpg"})
        self.assertIsNotNone(verified)
        if verified:
            Path(verified["path"]).unlink(missing_ok=True)

    def test_verified_image_rejects_html_even_with_success_status(self):
        class Resp:
            status_code=200
            headers={"Content-Type":"text/html","Content-Length":"100"}
            url="https://img.example/bad"
            def iter_content(self, chunk_size=65536):
                yield b"<html></html>"
        class FakeSession:
            def head(self,*a,**k): return Resp()
            def get(self,*a,**k): return Resp()
        with patch.object(main, "session", return_value=FakeSession()):
            self.assertIsNone(main.download_verified_image({"image_url":"https://img.example/bad"}))

    def test_publish_uses_selected_original_file_without_transform(self):
        from io import BytesIO
        from PIL import Image
        buf=BytesIO(); Image.new("RGB",(641,479),"white").save(buf,format="JPEG",quality=91); original=buf.getvalue()
        path=Path(main.tempfile.gettempdir())/"sports_test_original.jpg"; path.write_bytes(original)
        captured={}
        def fake_tg(method,data=None,file_path="",file_field="photo"):
            captured["method"]=method; captured["data"]=data or {}; captured["bytes"]=Path(file_path).read_bytes()
            with Image.open(file_path) as im:
                captured["size"]=im.size
            return {"ok":True,"result":{"message_id":1}}
        story={"headline":"Example sports story","body":EDITORIAL_BODY,"sources":[("Example","https://example.com/story")],"tags":["#Sports"],"image":{"url":"https://img.example/original.jpg"},"image_status":"verified_external"}
        try:
            with patch.object(main,"tg_call",side_effect=fake_tg):
                result=main.v1_publish_evergreen(story,str(path))
            self.assertTrue(result["ok"])
            self.assertEqual("sendPhoto",captured["method"])
            self.assertEqual(original,captured["bytes"])
            self.assertEqual((641,479),captured["size"])
            self.assertEqual("HTML",captured["data"]["parse_mode"])
        finally:
            path.unlink(missing_ok=True)

    def test_exa_image_search_is_only_used_after_normal_candidates_fail(self):
        story={"headline":"Example football history","normalized_subject":"Example football history","central_knowledge_unit":"Example football history","topic":"Example football history","sector":"Sport Discovery","sources":[],"people":[]}
        candidate={"image_url":"https://img.example/original.jpg","page_url":"https://example.com/story","source_name":"Example","provider":"bing_images","score":90}
        verified={"path":"/tmp/verified.jpg","url":candidate["image_url"],"source_page_url":candidate["page_url"],"source_name":"Example","provider":"bing_images","bytes":1000,"width":641,"height":479,"score":90}
        with patch.object(main,"discover_evergreen_image_candidates",return_value=[candidate]), patch.object(main,"download_verified_image",return_value=verified), patch.object(main,"v1_exa_search_query",side_effect=AssertionError("Exa image fallback must not run when normal image succeeds")):
            path,meta=main.prepare_evergreen_media(story)
        self.assertEqual("/tmp/verified.jpg",path)
        self.assertEqual("verified_external",story["image_status"])

    def test_exa_image_fallback_runs_only_after_normal_candidates_fail(self):
        story={"headline":"Example football history","normalized_subject":"Example football history","central_knowledge_unit":"Example football history","topic":"Example football history","sector":"Sport Discovery","sources":[],"people":[]}
        exa_row={"url":"https://example.com/story","title":"Example football history photo","text":"Example football history","source":"Example","image":"https://img.example/exa.jpg"}
        verified={"path":"/tmp/verified-exa.jpg","url":"https://img.example/exa.jpg","source_page_url":"https://example.com/story","source_name":"Example","provider":"exa_image_fallback","bytes":1000,"width":800,"height":600,"score":60}
        with patch.object(main,"discover_evergreen_image_candidates",return_value=[]), patch.object(main,"download_verified_image",return_value=verified), patch.object(main,"v1_exa_search_query",return_value=[exa_row]) as mocked, patch.object(main,"EXA_API_KEY","test"):
            path,meta=main.prepare_evergreen_media(story)
        self.assertEqual("/tmp/verified-exa.jpg",path)
        mocked.assert_called_once()
        self.assertEqual("verified_external",story["image_status"])

    def test_external_image_failure_falls_back_to_branded_card(self):
        story={"headline":"Example sports story","body":EDITORIAL_BODY,"sources":[("Example","https://example.com/story")],"tags":["#Sports"],"image":{},"image_status":"pending"}
        with patch.object(main,"discover_evergreen_image_candidates",return_value=[]), patch.object(main,"make_card",return_value="/tmp/fallback-card.jpg"):
            path,meta=main.prepare_evergreen_media(story)
        self.assertEqual("/tmp/fallback-card.jpg",path)
        self.assertEqual("card_fallback",story["image_status"])
        self.assertEqual("card_fallback",meta["mode"])

    def test_invalid_discovered_image_falls_back_to_card_after_verification_failure(self):
        story={"headline":"Example sports story","body":EDITORIAL_BODY,"sources":[("Example","https://example.com/story")],"tags":["#Sports"],"image":{},"image_status":"pending"}
        bad={"image_url":"https://img.example/bad.jpg","page_url":"https://example.com/story","source_name":"Example","provider":"bing_images","score":80}
        with patch.object(main,"discover_evergreen_image_candidates",return_value=[bad]), patch.object(main,"download_verified_image",return_value=None), patch.object(main,"make_card",return_value="/tmp/fallback-card.jpg"):
            path,meta=main.prepare_evergreen_media(story)
        self.assertEqual("/tmp/fallback-card.jpg",path)
        self.assertEqual("card_fallback",story["image_status"])
        self.assertEqual("card_fallback",meta["mode"])

    def test_production_orchestrator_image_fetch_is_in_pipeline(self):
        source=inspect.getsource(main.v1_run_once)
        self.assertIn("prepare_evergreen_media(story)",source)
    def test_build_evergreen_strips_legacy_editorial_fields_and_caps_tags(self):
        candidate = {"sector": "Sport Discovery", "sources": [{"name": "Source", "url": "https://example.com"}]}
        editorial = {**EDITORIAL_SAMPLE, "deck": "legacy", "hook": "legacy", "key_points": ["legacy"], "why_it_matters": "legacy", "caption": "legacy", "hashtags": ["#One", "#Two", "#Three", "#Four"]}
        story = main.build_evergreen(candidate, editorial)
        self.assertNotIn("deck", story)
        self.assertNotIn("hook", story)
        self.assertNotIn("key_points", story)
        self.assertNotIn("why_it_matters", story)
        self.assertNotIn("caption", story)
        self.assertEqual(["#One", "#Two", "#Three"], story["tags"])

    def test_current_news_filter_rejects_obvious_live_copy(self):
        self.assertIsNotNone(main.V1_CURRENT_RX.search("today's upcoming match preview"))

    def test_editorial_validator_accepts_40_to_60_word_body_without_lists(self):
        candidate = {"sources": [{"name": "Source", "url": "https://example.com", "evidence_note": "laws were standardized"}], "central_claim": "Football laws were standardized", "research_evidence": "laws were standardized"}
        ok, why, _ = main.v1_validate_editorial(candidate, dict(EDITORIAL_SAMPLE))
        self.assertTrue(ok, why)

    def test_editorial_validator_rejects_body_over_60_words(self):
        candidate = {"sources": [{"name": "Source", "url": "https://example.com"}], "central_claim": "Football laws were standardized", "research_evidence": "laws were standardized"}
        too_long = dict(EDITORIAL_SAMPLE)
        too_long["body"] = " ".join(["Verified"] * 61)
        ok, why, _ = main.v1_validate_editorial(candidate, too_long)
        self.assertFalse(ok)
        self.assertEqual("body_word_count", why)

    def test_editorial_validator_rejects_more_than_three_hashtags(self):
        candidate = {"sources": [{"name": "Source", "url": "https://example.com"}], "central_claim": "Football laws were standardized", "research_evidence": "laws were standardized"}
        too_many = dict(EDITORIAL_SAMPLE)
        too_many["hashtags"] = ["#One", "#Two", "#Three", "#Four"]
        ok, why, _ = main.v1_validate_editorial(candidate, too_many)
        self.assertFalse(ok)
        self.assertEqual("hashtag_count", why)

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
        editorial = dict(EDITORIAL_SAMPLE)
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), \
             patch.object(main, "prepare_evergreen_media", side_effect=lambda story: ("/tmp/test-card.jpg", {"mode":"card_fallback"})), \
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
        editorial = dict(EDITORIAL_SAMPLE)
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), \
             patch.object(main, "prepare_evergreen_media", side_effect=lambda story: ("/tmp/test-card.jpg", {"mode":"card_fallback"})), \
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
        editorial = dict(EDITORIAL_SAMPLE)
        with patch.object(main, "CEREBRAS_API_KEY", "test"), patch.object(main, "TELEGRAM_BOT_TOKEN", "test"), \
             patch.object(main, "now_bd", return_value=fixed_now), patch.object(main, "load_state", return_value=state), \
             patch.object(main, "load_coverage", return_value={"records": []}), patch.object(main, "v1_discover_all_sectors", return_value=candidates), \
             patch.object(main, "v1_rank", return_value=ranked), patch.object(main, "v1_verify_selected", side_effect=lambda selected, target: (selected, "ok")), \
             patch.object(main, "v1_editorialize", return_value=editorial), \
             patch.object(main, "prepare_evergreen_media", side_effect=lambda story: ("/tmp/test-card.jpg", {"mode":"card_fallback"})), \
             patch.object(main, "V1_POST_DELAY_SECONDS", 0), \
             patch.object(main, "live_pair", return_value=({"target_date": "2026-09-23"}, {"target_date": "2026-09-21"}, [{"event_id": "e1"}], [{"event_id": "e2"}])):
            rc = main.run_once(mode="dry-run")
        self.assertEqual(0, rc)
        self.assertEqual(10, len([p for p in state["posts"] if p.get("desk") == "evergreen_v1"]))
        self.assertNotIn(main.V1_SECTORS[-1], {p.get("sector") for p in state["posts"] if p.get("desk") == "evergreen_v1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestV2SearchAndCard(unittest.TestCase):
    def test_v22_version_is_single_public_version(self):
        self.assertEqual("2.2.0", main.APP_VERSION)
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "2.2.0"', source)

    def test_normal_search_parser_extracts_duckduckgo_and_bing(self):
        ddg='''<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example Title</a><a class="result__snippet">A useful snippet here.</a>'''
        bing='''<li class="b_algo"><h2><a href="https://example.org/a">Example Title</a></h2><div class="b_caption"><p>A useful snippet here.</p></div></li>'''
        d=main._normal_search_parse(ddg,"duckduckgo",6)
        b=main._normal_search_parse(bing,"bing",6)
        self.assertEqual("https://example.com/a",d[0]["url"])
        self.assertEqual("A useful snippet here.",d[0]["text"])
        self.assertEqual("https://example.org/a",b[0]["url"])
        self.assertEqual("A useful snippet here.",b[0]["text"])

    def test_normal_search_is_primary_and_does_not_call_exa_when_sufficient(self):
        html='''<a class="result__a" href="https://example.com/a">Example A</a><a class="result__snippet">Snippet A</a>'''
        calls=[]
        def fake_normal(url, engine, limit):
            calls.append(("normal",engine))
            return main._normal_search_parse(html,"duckduckgo",limit)
        with patch.object(main,"_normal_search_engine",fake_normal), patch.object(main,"v1_exa_search_sector",side_effect=AssertionError("Exa should not be called")), patch.object(main,"NORMAL_SEARCH_FALLBACK_MIN",1):
            rows=main._search_with_exa_fallback_sector("Sport Discovery",date(2026,9,23),set(),num=1)
        self.assertEqual(1,len(rows))
        self.assertEqual("normal",calls[0][0])

    def test_normal_search_falls_back_to_exa_when_sparse(self):
        exa=[{"sector":"Sport Discovery","normalized_subject":"Fallback","source_url":"https://example.org/f","source":"Example","grade":"B","highlights":["fallback evidence"],"text":"fallback evidence","image":"","search_provider":"exa_fallback"}]
        calls=[]
        def fake_normal(url, engine, limit):
            calls.append(engine)
            return []
        with patch.object(main,"_normal_search_engine",fake_normal), patch.object(main,"v1_exa_search_sector",return_value=exa) as mocked_exa:
            rows=main._search_with_exa_fallback_sector("Sport Discovery",date(2026,9,23),set(),num=6)
        self.assertEqual(["duckduckgo","bing"],calls)
        mocked_exa.assert_called_once()
        self.assertEqual("Fallback",rows[-1]["normalized_subject"])

    def test_v2_normal_search_does_not_require_exa_secret_for_config(self):
        with patch.object(main,"EXA_API_KEY",""):
            with patch.object(main,"CEREBRAS_API_KEY","x"), patch.object(main,"TELEGRAM_BOT_TOKEN","x"), patch.object(main,"v1_cerebras_preflight",return_value=(True,"ok")):
                self.assertEqual(0,main.v1_validate_config(require_secrets=True))

    def test_discovery_entrypoint_is_normal_search_first(self):
        calls=[]
        def fake(sector,target,used_sectors,**kwargs):
            calls.append((sector,kwargs))
            return [{"sector":sector,"normalized_subject":sector,"source_url":"https://example.com/"+main.slugify(sector),"source":"Example","grade":"B","highlights":["evidence"],"text":"evidence","image":"","search_provider":"normal"}]
        with patch.object(main,"_search_with_exa_fallback_sector",fake):
            rows=main.v1_discover_all_sectors(date(2026,9,23),set())
        self.assertEqual(20,len(calls))
        self.assertEqual(20,len(rows))
        self.assertTrue(all(k.get("num")==main.NORMAL_SEARCH_RESULTS for _,k in calls))

    def test_normal_image_search_is_non_exa_primary(self):
        with patch.object(main, "http", return_value=main.Resp(200, text_='<a class="iusc" m="{&quot;murl&quot;:&quot;https://img.example/x.jpg&quot;,&quot;purl&quot;:&quot;https://example.com/story&quot;,&quot;t&quot;:&quot;Example football photo&quot;}"></a>', headers={"Content-Type":"text/html"})) as mocked:
            rows=main.normal_image_search("Example football story", num=1)
        self.assertEqual(1,len(rows))
        self.assertEqual("bing_images",rows[0]["provider"])
        mocked.assert_called_once()
        self.assertIn("www.bing.com/images/search",mocked.call_args.args[2])

    def test_discovery_quality_does_not_reward_scraped_images(self):
        base={"grade":"B","highlights":["x"],"normalized_subject":"Useful sport history"}
        with_image={**base,"image":"https://example.com/photo.jpg"}
        self.assertEqual(main.v1_discovery_quality(base),main.v1_discovery_quality(with_image))

    def test_card_has_at_most_three_information_columns(self):
        items=main._card_info_items({
            "central_claim":"A documented claim about the sport",
            "historical_year":"1863",
            "country":"England",
            "region":"Europe",
            "topic":"Association football",
        })
        self.assertLessEqual(len(items),3)
        self.assertEqual("KEY FACT",items[0][0])
        self.assertEqual("WHEN",items[1][0])
        self.assertEqual("WHERE",items[2][0])

    def test_card_is_fixed_1200x675(self):
        if main.Image is None:
            self.skipTest("Pillow unavailable")
        story={"format":"history","label":"GAME / SPORTS HISTORY","headline":"A Short Sports History Headline","central_claim":"Rules became standardized in the nineteenth century.","historical_year":"1863","country":"England"}
        path=main.make_card(story)
        self.assertTrue(path and Path(path).exists())
        try:
            with main.Image.open(path) as im:
                self.assertEqual((1200,675),im.size)
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
