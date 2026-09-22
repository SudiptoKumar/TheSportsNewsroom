import ast
import copy
import json
import unittest
from datetime import date
from unittest.mock import patch
from pathlib import Path
import main


class FakeResponse:
    ok = True
    status = 200
    error = ""
    data = {"results": [], "requestId": "test"}
    headers = {}
    text = ""


class TestV14(unittest.TestCase):
    def test_exact_twenty_sectors(self):
        self.assertEqual(20, len(main.V1_SECTORS))
        self.assertEqual(20, len(set(main.V1_SECTORS)))

    def test_day_plan_is_exact_10_plus_10_and_disjoint(self):
        p = main._v1_day_sector_plan(date(2026, 9, 22))
        self.assertEqual(10, len(p["AM"]))
        self.assertEqual(10, len(p["PM"]))
        self.assertTrue(set(p["AM"]).isdisjoint(p["PM"]))
        self.assertEqual(set(main.V1_SECTORS), set(p["AM"]) | set(p["PM"]))

    def test_exa_discovery_contract_has_no_output_schema(self):
        calls = []
        r = type("R", (), {"ok": True, "status": 200, "data": {"results": [], "requestId": "x"}, "error": ""})()
        with patch.object(main, "EXA_API_KEY", "x"), patch.object(main, "_v1_exa_request", lambda u, b, **k: (calls.append(b) or r)):
            main.v1_exa_search_sector("Sport Discovery", date(2026, 9, 22), num=10)
        self.assertEqual(1, len(calls))
        self.assertEqual(10, calls[0]["numResults"])
        self.assertEqual("auto", calls[0]["type"])
        self.assertNotIn("outputSchema", calls[0])
        self.assertEqual({"highlights": True, "summary": True}, calls[0]["contents"])

    def test_ranker_selects_exactly_one_per_sector_from_matrix(self):
        raw = []
        for i, sec in enumerate(main.V1_SECTORS):
            for j in range(10):
                raw.append({
                    "sector": sec,
                    "normalized_subject": f"{sec} Subject {j}",
                    "central_knowledge_unit": f"{sec} Knowledge {j}",
                    "central_claim": f"Documented claim about {sec} subject {j}",
                    "source_url": f"https://source{i}.example/{j}",
                    "source": f"Source {i}",
                    "grade": "A",
                    "highlights": ["Specific evidence"],
                    "summary": "Useful documented source evidence.",
                    "exa_score": 0.99 - j * 0.01,
                    "image": "https://img.example/x.jpg",
                })
        selected, reserves, diag = main.v1_rank_and_select_20(raw, {"records": []})
        self.assertEqual(20, len(selected))
        self.assertEqual(set(main.V1_SECTORS), {x["sector"] for x in selected})
        self.assertEqual(20, len(reserves))
        self.assertFalse(diag["missing_sectors"])

    def test_cross_sector_same_knowledge_is_blocked(self):
        a = {"sector": "Sport Discovery", "normalized_subject": "Bandy history", "central_knowledge_unit": "Bandy history", "central_claim": "Bandy developed from an old winter ball game."}
        b = {"sector": "Sport Origin", "normalized_subject": "Bandy history", "central_knowledge_unit": "Bandy history", "central_claim": "Bandy developed from an old winter ball game."}
        self.assertTrue(main._v1_same_knowledge(a, b))

    def test_cross_sector_near_same_claim_is_blocked(self):
        a={"sector":"Sport Discovery","normalized_subject":"Historic Bandy Rules","central_knowledge_unit":"Historic Bandy Rules","central_claim":"The sport uses a distinctive rule that changes how possession restarts after a stoppage."}
        b={"sector":"Rule Check","normalized_subject":"Bandy Rule Restart","central_knowledge_unit":"Bandy Rule Restart","central_claim":"The sport uses a distinctive rule that changes how possession restarts after a stoppage."}
        self.assertTrue(main._v1_same_knowledge(a,b))

    def test_contents_batch_contract(self):
        calls = []
        r = type("R", (), {"ok": True, "status": 200, "data": {"results": [], "statuses": []}, "error": ""})()
        old = main.EXA_API_KEY
        main.EXA_API_KEY = "x"
        try:
            with patch.object(main, "_v1_exa_request", lambda u, b, **k: (calls.append((u, b)) or r)):
                main.v1_contents_for_urls(["https://example.com/a", "https://example.com/a/?utm_source=x"])
        finally:
            main.EXA_API_KEY = old
        self.assertTrue(calls)
        self.assertTrue(calls[0][0].endswith("/contents"))
        self.assertEqual(1, len(calls[0][1]["urls"]))
        self.assertEqual(720, calls[0][1]["maxAgeHours"])

    def test_storytelling_editorial_schema_is_strict(self):
        schema = main.V1_BATCH_EDITORIAL_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        item = schema["properties"]["stories"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertFalse(item["properties"]["key_points"]["items"].get("additionalProperties", False) if item["properties"]["key_points"]["items"]["type"] == "object" else False)
        self.assertEqual(20, schema["properties"]["stories"]["minItems"])
        self.assertEqual(20, schema["properties"]["stories"]["maxItems"])

    def test_batch_editorial_uses_one_ai_request(self):
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_claim": "A documented claim without unsupported numbers.",
                "research_evidence": "The source explains a specific durable sports or games fact with supporting context. It does not require current information.",
                "sources": [{"name": "Source", "url": f"https://example{i}.example/page", "grade": "A"}],
            })
        calls = {"n": 0}
        class AI:
            available = True
            fatal = False
            model = "gpt-oss-120b"
            last_error = ""
            def json(self, *args, **kwargs):
                calls["n"] += 1
                return {"stories": [
                    {"post_number": i + 1,
                     "headline": f"A Useful {main.V1_SECTORS[i]} Subject Story",
                     "lead": "This lead immediately explains the subject with useful context and tells the reader why the documented detail matters today.",
                     "story": "The story then explains the documented background and distinctive detail in a compact editorial form that stays focused on one knowledge unit without repeating the lead or drifting into a blog narrative. The useful detail stays specific to the supplied evidence. It keeps the explanation concrete, readable, and centered on the supplied source rather than adding speculative context.",
                     "key_points": ["The source documents the subject", "The distinctive detail is explained", "The evidence preserves useful context"],
                     "takeaway": "The central documented detail is the memorable point readers should keep in mind after reading this source-backed story."}
                    for i in range(20)
                ]}
        out = main.v1_batch_editorialize(AI(), candidates)
        self.assertEqual(1, calls["n"])
        self.assertEqual(20, len(out))

    def test_ai_output_numeric_guard(self):
        c = {"sources": [{"url": "https://example.com"}], "research_evidence": "The page says the event began in 1912.", "central_claim": "The event began in 1912.", "normalized_subject": "Example event 1912"}
        good = {"headline": "A Historic Event Began in 1912", "lead": "This lead immediately explains the documented historical starting point and gives useful context without adding new facts or speculation.", "story": "The source records the event and places its beginning in the documented year while preserving the original historical context for readers. The explanation stays focused on the supplied evidence, identifies the documented starting point clearly, and avoids adding unsupported details or modern comparisons that are not present in the source.", "key_points": ["The source records the event clearly", "The documented starting year is 1912", "The surrounding context is historical"], "takeaway": "The recorded year is the key historical detail readers should remember."}
        bad = copy.deepcopy(good)
        bad["headline"] = "A Historic Event Began in 1913"
        self.assertEqual((True, "ok"), main.v1_validate_editorial(c, good))
        self.assertEqual("unsupported_number", main.v1_validate_editorial(c, bad)[1])

    def test_nullable_story_fields_are_safe(self):
        s = main.v1_normalize_story({"image": None, "sources": None, "key_points": None, "tags": None, "people": None})
        self.assertEqual({}, s["image"])
        self.assertEqual([], s["sources"])
        self.assertEqual([], s["key_points"])
        self.assertEqual([], s["tags"])
        self.assertEqual([], s["people"])

    def test_source_name_is_clickable_and_raw_url_hidden(self):
        story = main.make_v1_story(
            {"sector": "Sport Discovery", "normalized_subject": "Example Topic", "central_knowledge_unit": "Example Topic", "central_claim": "Documented claim", "sources": [{"name": "FIFA", "url": "https://www.fifa.com/example"}]},
            {"headline": "A Useful Example Sports Story", "lead": "This lead gives readers immediate context about the subject and why its documented historical detail matters.", "story": "The story explains the documented context and distinctive detail in a concise editorial way that remains useful after publication without filler or repetition.", "key_points": ["The source documents the subject", "The defining detail is explained", "The evidence remains specific"], "takeaway": "The documented detail is the main point readers should remember from this source-backed story."},
            None,
        )
        h = main._v1_caption_html(story)
        self.assertIn('<a href="https://www.fifa.com/example">FIFA</a>', h)
        self.assertNotIn("https://www.fifa.com/example)" , h)

    def test_photo_first_no_plain_text_fallback(self):
        story = main.make_v1_story(
            {"sector": "Sport Discovery", "normalized_subject": "Example Topic", "central_knowledge_unit": "Example Topic", "central_claim": "Documented claim", "sources": [{"name": "Source", "url": "https://example.com/a"}]},
            {"headline": "A Useful Example Sports Story", "lead": "This lead gives readers immediate context about the subject and why its documented historical detail matters.", "story": "The story explains the documented context and distinctive detail in a concise editorial way that remains useful after publication without filler or repetition.", "key_points": ["The source documents the subject", "The defining detail is explained", "The evidence remains specific"], "takeaway": "The documented detail is the main point readers should remember from this source-backed story."},
            None,
        )
        calls = []
        with patch.object(main, "tg_call", side_effect=lambda method, data=None, file_path="", file_field="photo": (calls.append((method, data, file_path)) or {"ok": True, "result": {"message_id": 1}})), patch.object(main, "make_card", lambda st: "/tmp/fake-card.jpg"), patch.object(main.os, "remove", lambda p: None):
            result = main.v1_publish_evergreen(story)
        self.assertTrue(result["ok"])
        self.assertEqual("sendPhoto", calls[0][0])
        self.assertNotIn("sendMessage", [x[0] for x in calls])

    def test_daily_batch_validation_requires_20_distinct_sectors(self):
        b = main._v1_blank_batch(date(2026, 9, 22))
        b["status"] = "editorial_ready"
        b["selected"] = [{"sector": s} for s in main.V1_SECTORS]
        b["stories"] = [{"sector": s} for s in main.V1_SECTORS]
        self.assertTrue(main._v1_batch_valid(b, date(2026, 9, 22)))
        b["stories"][-1]["sector"] = b["stories"][0]["sector"]
        self.assertFalse(main._v1_batch_valid(b, date(2026, 9, 22)))

    def test_public_run_never_researches_pm_when_batch_exists(self):
        src = Path(main.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "v1_run_once")
        code = ast.get_source_segment(src, fn)
        self.assertIn("v1_build_or_restore_batch", code)
        self.assertIn("v1_prepare_editorial_batch", code)
        self.assertIn("_v1_publish_targets", code)
        self.assertIn("_v1_rotate_live_pair", code)

    def test_live_pair_cleanup_is_after_both_new_posts(self):
        src = Path(main.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_v1_rotate_live_pair")
        code = ast.get_source_segment(src, fn)
        self.assertGreater(code.index('v4_publish_with_idempotency'), -1)
        self.assertGreater(code.index('for mid in (old_sched,old_res)'), code.index('save_state(state)'))

    def test_deterministic_fallback_can_publish_short_evidence_without_inventing_facts(self):
        c={"sources":[{"name":"Source","url":"https://example.com"}],"research_evidence":"The page records that the event began in 1912.","central_claim":"The event began in 1912.","normalized_subject":"Example event"}
        fb=main._v1_story_fallback(c)
        self.assertTrue(main.v1_validate_editorial(c,fb,strict_shape=False)[0])


    def test_discovery_requests_ten_per_sector_and_targets_200(self):
        calls = []
        def fake_search(sector, target, *, variant=0, num=None):
            calls.append((sector, variant, num))
            return [{
                "sector": sector,
                "normalized_subject": f"{sector} subject {i}",
                "central_knowledge_unit": f"{sector} knowledge {i}",
                "central_claim": f"Documented {sector} detail {i}.",
                "source_url": f"https://{i}.{sector.lower().replace(' ','-').replace('/','')}.example/page",
            } for i in range(10)]
        with patch.object(main, "v1_exa_search_sector", side_effect=fake_search):
            raw, manifest = main.v1_discover_200(date(2026, 9, 22))
        self.assertEqual(200, len(raw))
        self.assertEqual(200, manifest["actual"])
        self.assertEqual(200, manifest["target"])
        self.assertEqual(20, len(manifest["sector_counts"]))
        self.assertTrue(all(n == 10 for _, _, n in calls))
        self.assertEqual(20, len(calls))
        self.assertTrue(all(v == 0 for _, v, _ in calls))

    def test_daily_batch_is_research_once_and_editorial_once(self):
        target = date(2026, 9, 22)
        state = {"v1": {"daily": {target.isoformat(): {"published_sectors": [], "run_ids": [], "sector_plan": main._v1_day_sector_plan(target)}}}}
        vs = main.v1_state(state)
        selected = [{
            "sector": sec,
            "normalized_subject": f"Subject {i}",
            "central_knowledge_unit": f"Knowledge unit {i}",
            "central_claim": f"Documented claim for subject {i}.",
            "topic": f"Subject {i}",
            "source_url": f"https://example{i}.com/page",
            "source": f"Source {i}",
            "grade": "A",
            "highlights": [f"Evidence for subject {i} is documented by the source."],
            "text": f"Evidence for subject {i} is documented by the source.",
            "exa_score": 0.99 - i * 0.001,
            "sources": [{"name": f"Source {i}", "url": f"https://example{i}.com/page", "grade": "A"}],
            "research_evidence": f"The source documents subject {i} with useful context.",
            "evidence_status": "verified",
        } for i, sec in enumerate(main.V1_SECTORS)]
        reserves = {sec: [] for sec in main.V1_SECTORS}
        calls = {"discover": 0, "editorial": 0}
        def fake_discover(day):
            calls["discover"] += 1
            return [], {"target": 200, "actual": 200, "sector_counts": {sec: 10 for sec in main.V1_SECTORS}}
        def fake_rank(raw, coverage):
            return selected, reserves, {"missing_sectors": [], "selected": 20}
        def fake_enrich(rows, reserve_map):
            return rows, "ok"
        class AI:
            available = True
            fatal = False
            model = "gpt-oss-120b"
            last_error = ""
            def json(self, *args, **kwargs):
                calls["editorial"] += 1
                return {"stories": [{
                    "post_number": i + 1,
                    "headline": f"A Useful Subject Story Number {i}",
                    "lead": "This lead immediately gives useful context and explains why the documented subject matters to the reader today without unnecessary narrative filler.",
                    "story": "This compact story explains the documented background and distinctive detail while staying centered on one knowledge unit. It uses the supplied evidence to make the subject understandable, keeps the language lively, and avoids adding facts that the research package does not support. The explanation remains concise and editorial rather than blog-like.",
                    "key_points": ["The source documents the subject clearly", "The distinctive detail is explained", "The evidence preserves useful context"],
                    "takeaway": "The central documented detail is the memorable point readers should keep after the story and source review.",
                } for i in range(20)]}
        with patch.object(main, "v1_discover_200", side_effect=fake_discover), patch.object(main, "v1_rank_and_select_20", side_effect=fake_rank), patch.object(main, "v1_enrich_selected", side_effect=fake_enrich), patch.object(main, "v1_prepare_images", return_value=[]), patch.object(main, "save_state"):
            batch, status = main.v1_build_or_restore_batch(state, vs, {"records": []}, target, AI())
            self.assertEqual("built", status)
            self.assertEqual(1, calls["discover"])
            self.assertEqual(200, batch["discovered_actual"])
            self.assertEqual(20, len(batch["selected"]))
            self.assertTrue(main.v1_prepare_editorial_batch(state, vs, batch, AI()))
            self.assertEqual(1, calls["editorial"])
            self.assertEqual("editorial_ready", batch["status"])
            # A second run restores the durable daily batch and must not research again.
            batch2, status2 = main.v1_build_or_restore_batch(state, vs, {"records": []}, target, AI())
            self.assertEqual("restored", status2)
            self.assertIs(batch2, batch)
            self.assertEqual(1, calls["discover"])


    def test_one_bad_editorial_item_does_not_discard_the_other_nineteen(self):
        target = date(2026, 9, 22)
        candidates = []
        for i, sec in enumerate(main.V1_SECTORS):
            candidates.append({
                "sector": sec, "normalized_subject": f"Subject {i}", "central_knowledge_unit": f"Knowledge {i}",
                "central_claim": f"Documented claim {i}.", "research_evidence": f"The source documents subject {i} with useful historical context.",
                "sources": [{"name": f"Source {i}", "url": f"https://example{i}.com/page", "grade": "A"}],
                "highlights": [f"The source documents subject {i} clearly with useful context."],
            })
        class AI:
            available=True; fatal=False; model="gpt-oss-120b"; last_error=""
            def json(self, *args, **kwargs):
                rows=[]
                for i in range(20):
                    rows.append({
                        "post_number": i+1,
                        "headline": "Bad",
                        "lead": "Too short",
                        "story": "Too short",
                        "key_points": ["Bad", "Bad", "Bad"],
                        "takeaway": "Bad",
                    } if i == 7 else {
                        "post_number": i+1,
                        "headline": f"A Useful Subject Story Number {i}",
                        "lead": "This lead immediately gives useful context and explains why the documented subject matters to readers without unnecessary narrative filler or repetition.",
                        "story": "This compact story explains the documented background and distinctive detail while staying centered on one knowledge unit. It uses supplied evidence to make the subject understandable, keeps the language lively, and avoids unsupported additions. The explanation remains concise and editorial rather than blog-like, with enough context to make the central detail memorable without repeating the lead.",
                        "key_points": ["The source documents the subject clearly", "The distinctive detail is explained", "The evidence preserves useful context"],
                        "takeaway": "The central documented detail is the memorable point readers should keep after the story and source review.",
                    })
                return {"stories": rows}
        state={"v1":{"daily":{target.isoformat():{"published_sectors":[],"run_ids":[],"sector_plan":main._v1_day_sector_plan(target)}}}}
        vs=main.v1_state(state)
        batch=main._v1_blank_batch(target); batch.update({"status":"researched","selected":candidates})
        with patch.object(main,"v1_prepare_images",return_value=[]), patch.object(main,"save_state"):
            self.assertTrue(main.v1_prepare_editorial_batch(state,vs,batch,AI()))
        self.assertEqual("editorial_ready",batch["status"])
        self.assertEqual(20,len(batch["stories"]))
        self.assertNotEqual("Bad",batch["stories"][7]["headline"])


    def test_old_daily_history_is_pruned(self):
        old = date(2026, 9, 1).isoformat()
        today = date(2026, 9, 22)
        root = {"daily": {old: {"batch": {"status": "editorial_ready"}}}}
        main._v1_prune_daily_history(root, today, keep_days=7)
        self.assertNotIn(old, root["daily"])

    def test_save_state_serializes_datetime_values(self):
        import tempfile
        from datetime import datetime, timezone
        old_file = main.STATE_FILE
        old_dry = main.DRY_RUN
        with tempfile.TemporaryDirectory() as td:
            main.STATE_FILE = str(Path(td) / "state.json")
            main.DRY_RUN = False
            try:
                main.save_state({"when": datetime(2026, 9, 22, 1, 2, 3, tzinfo=timezone.utc)})
                data = json.loads(Path(main.STATE_FILE).read_text(encoding="utf-8"))
                self.assertEqual("2026-09-22T01:02:03+00:00", data["when"])
            finally:
                main.STATE_FILE = old_file
                main.DRY_RUN = old_dry

    def test_hard_sector_uses_followup_query_variants_until_full(self):
        calls=[]
        def fake_search(sector, target, *, variant=0, num=None):
            calls.append((sector, variant))
            count = 6 if variant == 0 else 4
            return [{
                "sector": sector,
                "normalized_subject": f"On Date Subject {variant}-{i}",
                "central_knowledge_unit": f"On Date Knowledge {variant}-{i}",
                "central_claim": "Documented historical claim.",
                "source_url": f"https://date{variant}-{i}.example/page",
            } for i in range(count)]
        with patch.object(main, "v1_exa_search_sector", side_effect=fake_search):
            raw, manifest = main.v1_discover_200(date(2026, 9, 22))
        date_rows=[x for x in raw if x["sector"] == "On This Date"]
        self.assertEqual(10, len(date_rows))
        date_calls=[v for sec,v in calls if sec == "On This Date"]
        self.assertEqual([0,1], date_calls)


    def test_day_publication_split_is_exactly_ten_then_the_other_ten(self):
        target=date(2026,9,22)
        state={"posts":[],"v1":{"daily":{target.isoformat():{"published_sectors":[]}}}}
        vs=main.v1_state(state)
        plan=vs["daily"][target.isoformat()]["sector_plan"]
        batch=main._v1_blank_batch(target)
        batch["status"]="editorial_ready"
        batch["selected"]=[{"sector":sec,"candidate_id":sec} for sec in main.V1_SECTORS]
        batch["stories"]=[{
            "sector":sec,"topic":sec,"normalized_subject":f"{sec} subject","central_knowledge_unit":f"{sec} knowledge",
            "central_claim":"Documented claim.","headline":f"A Useful Story About {sec}","image":{},"image_status":"generated",
            "sources":[("Source",f"https://example.com/{i}")],"urls":[f"https://example.com/{i}"],
        } for i,sec in enumerate(main.V1_SECTORS)]
        published=[]
        old_delay=main.V1_POST_DELAY
        main.V1_POST_DELAY=0
        def fake_pub(vs_,pubid,publish_fn):
            return {"ok":True,"result":{"message_id":len(published)+1}}
        try:
            with patch.object(main,"v4_publish_with_idempotency",side_effect=fake_pub), patch.object(main,"v4_record_coverage"), patch.object(main,"save_state"):
                rc, _=main._v1_publish_targets(state,vs,vs["daily"][target.isoformat()],batch,list(plan["AM"]),"2026-09-22-AM",{"records":[]})
                published.extend(vs["daily"][target.isoformat()]["published_sectors"])
                self.assertEqual(0,rc); self.assertEqual(10,len(published)); self.assertEqual(set(plan["AM"]),set(published))
                rc, _=main._v1_publish_targets(state,vs,vs["daily"][target.isoformat()],batch,list(plan["PM"]),"2026-09-22-PM",{"records":[]})
                self.assertEqual(0,rc)
        finally:
            main.V1_POST_DELAY=old_delay
        all_published=set(vs["daily"][target.isoformat()]["published_sectors"])
        self.assertEqual(20,len(all_published)); self.assertTrue(set(plan["AM"]).isdisjoint(plan["PM"])); self.assertEqual(set(main.V1_SECTORS),all_published)

    def test_version(self):
        self.assertEqual("1.4.0", main.APP_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
