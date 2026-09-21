import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as app


class V4ArchitectureTests(unittest.TestCase):
    def test_sector_matrix_is_exactly_twenty(self):
        self.assertEqual(len(app.V4_SECTORS), 20)
        self.assertEqual(set(app.V4_SECTORS), app.V4_SECTOR_SET)

    def test_coverage_detects_same_knowledge_without_fingerprint(self):
        candidate = {
            "sector": "Sport Origin",
            "normalized_subject": "Football origins",
            "central_knowledge_unit": "codified association football origins",
            "central_claim": "Football laws were codified in 1863.",
            "topic": "Football",
        }
        coverage = {"records": [{
            "content_id": "old-record",
            "normalized_subject": "football origins",
            "central_knowledge_unit": "codified association football origins",
            "central_claim": "Football laws codified in 1863",
            "source_urls": [],
            "fingerprints": [],
        }]}
        status, _, score = app.v4_coverage_match(candidate, coverage)
        self.assertEqual(status, "duplicate")
        self.assertGreaterEqual(score, 1.0)

    def test_coverage_allows_materially_related_topic(self):
        candidate = {
            "sector": "Interesting Sports Fact",
            "normalized_subject": "football goal posts",
            "central_knowledge_unit": "football pitch dimensions",
            "central_claim": "goal width has a standardized measurement",
            "topic": "Football",
        }
        coverage = {"records": [{
            "content_id": "old-record",
            "normalized_subject": "football origins",
            "central_knowledge_unit": "codified association football origins",
            "central_claim": "Football laws codified in 1863",
            "source_urls": [],
            "fingerprints": [],
        }]}
        status, _, _ = app.v4_coverage_match(candidate, coverage)
        self.assertIn(status, {"new", "related"})


    def test_filter_keeps_already_used_today_sectors_for_full_batch_ranking(self):
        vocab = ["hurling", "go", "kabaddi", "carrom", "fencing", "korfball", "sepak", "shogi", "polo", "jai", "biathlon", "capoeira", "pankration", "curling", "teeball", "buzkashi", "gatka", "xiangqi", "sambo", "pelota"]
        candidates = []
        for index, sector in enumerate(app.V4_SECTORS, 1):
            token = vocab[index - 1]
            candidates.append({
                "sector": sector,
                "normalized_subject": token,
                "central_knowledge_unit": token,
                "central_claim": token,
                "sources": [{"url": f"https://example.com/{index}"}],
            })
        accepted, _ = app.v4_filter_candidates(candidates, {"records": []}, {app.V4_SECTORS[0]}, None)
        self.assertEqual(len(accepted), 20)

    def test_live_event_identity_collapses_source_time_drift(self):
        start = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
        a = app.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=start)
        b = app.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=start + timedelta(minutes=15))
        self.assertEqual(app.v4_event_identity(a), app.v4_event_identity(b))

    def test_major_live_events_deduplicate_events(self):
        start = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
        a = app.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=start)
        b = app.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=start + timedelta(minutes=15))
        rows = app.v4_major_live_events([a, b], "next", date(2026, 9, 23))
        self.assertEqual(len(rows), 1)

    def test_live_rich_message_is_native_compact_table(self):
        start = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
        a = app.make_event(sport="Football", league="UEFA Champions League", home="A", away="B", name="A vs B", start=start)
        payload = app.v4_live_rich("next", date(2026, 9, 23), [a])
        self.assertEqual(payload["blocks"][0]["type"], "table")
        table = payload["blocks"][0]
        self.assertEqual(len(table["cells"]), 2)
        self.assertTrue(table["is_bordered"])
        self.assertTrue(table["is_striped"])
        self.assertTrue(table["is_compact"])
        self.assertIn("caption", table)

    def test_evergreen_rich_message_uses_real_photo_block(self):
        story = {
            "sector": "Interesting Sports Fact",
            "headline": "Why This Sports Measurement Still Matters",
            "deck": "A concise explanation.",
            "body": "This is a verified evergreen story with enough detail to satisfy the editorial body length requirements in a real post.",
            "key_points": ["Point one", "Point two", "Point three"],
            "why_it_matters": "It explains a useful piece of sports knowledge.",
            "sources": [("Source", "https://example.com")],
            "tags": ["#SportsFacts"],
            "image": {"url": "https://example.com/image.jpg"},
        }
        payload = app.v4_evergreen_rich(story)
        self.assertEqual(payload["blocks"][0]["type"], "photo")
        self.assertTrue(any(b.get("type") == "heading" for b in payload["blocks"]))

    def test_editorial_validator_accepts_valid_evergreen_copy(self):
        candidate = {
            "sector": "Sport Origin",
            "normalized_subject": "Football origins",
            "central_knowledge_unit": "codified association football origins",
            "central_claim": "Football laws were codified in 1863.",
            "main_story": "The written laws became a shared reference point.",
            "historical_year": "1863",
            "sources": [{"name": "Archive", "url": "https://example.com", "evidence_note": "Laws were codified in 1863."}],
            "people_involved": [],
        }
        editorial = {
            "headline": "How Football Laws Became Written Rules",
            "deck": "",
            "hook": "",
            "body": (
                "Football's laws became more standardized after clubs agreed on a written code. The milestone helped distinguish association football from other football traditions and gave the sport a common framework. That shared framework could travel between clubs and countries over time, making the game easier to recognize across different communities. It also created a reference point for later rule development and comparison."
            ),
            "key_points": ["Written rules created common expectations", "The code shaped later development", "The change made comparison easier"],
            "why_it_matters": "It explains why a familiar sport has shared rules.",
            "caption": "",
            "hashtags": ["#Football"],
            "angle": "history",
            "image_index": 0,
            "image_reason": "none",
        }
        ok, reason, _ = app.v4_validate_editorial(candidate, editorial, [])
        self.assertTrue(ok, reason)

    def test_editorial_validator_rejects_current_news_language(self):
        candidate = {
            "sector": "Sport Origin",
            "normalized_subject": "Football origins",
            "central_knowledge_unit": "codified association football origins",
            "central_claim": "Football laws were codified in 1863.",
            "main_story": "The written laws became a shared reference point.",
            "historical_year": "1863",
            "sources": [{"name": "Archive", "url": "https://example.com", "evidence_note": "Laws were codified in 1863."}],
            "people_involved": [],
        }
        editorial = {
            "headline": "How Football Laws Became Written Rules",
            "deck": "",
            "hook": "",
            "body": (
                "This is the latest football story and it covers a major current development in the sport. The update concerns the current season, an upcoming match, and a breaking change that belongs in a live news desk rather than an evergreen history post. It therefore fails the evergreen language gate even though the writing remains concise and readable."
            ),
            "key_points": ["Current stories need a different desk", "Upcoming games are temporary information", "Evergreen posts require lasting context"],
            "why_it_matters": "This is only a validation test.",
            "caption": "",
            "hashtags": ["#Football"],
            "angle": "history",
            "image_index": 0,
            "image_reason": "none",
        }
        ok, reason, _ = app.v4_validate_editorial(candidate, editorial, [])
        self.assertFalse(ok)
        self.assertEqual(reason, "current_news_language")

    def test_exa_discovery_request_uses_compact_schema(self):
        import copy
        original_key = app.EXA_API_KEY
        original_http = app.http
        calls = []
        class FakeResp:
            ok = True
            status = 200
            error = ""
            data = {
                "output": {"content": {
                    "calendar_date": "2026-09-23", "daily_complete": True,
                    "posts": [
                        {"sector": sec, "normalized_subject": name, "central_knowledge_unit": f"historical knowledge unit of {name}", "central_claim": f"{name} has a distinct documented historical or rules-based record.", "research_focus": f"Investigate {name} using authoritative evidence."}
                        for sec, name in zip(app.V4_SECTORS, [
                            "hurling","go","kabaddi","carrom","fencing","korfball","sepak takraw","shogi","polo","jai alai",
                            "biathlon","capoeira","pankration","curling","teeball","buzkashi","gatka","xiangqi","sambo","pelota"
                        ])
                    ]
                }},
                "results": []
            }
        def fake_http(source, method, url, **kwargs):
            calls.append(kwargs.get("json_body"))
            return FakeResp()
        app.EXA_API_KEY = "test-key"
        app.http = fake_http
        try:
            posts, results, status = app.v4_exa_research(None, {}, {"records": []}, date(2026,9,23), "2026-09-23-AM", set())
        finally:
            app.EXA_API_KEY = original_key
            app.http = original_http
        self.assertEqual(status, "ok")
        self.assertEqual(len(posts), 20)
        self.assertEqual(len(calls), 1)
        schema = calls[0]["outputSchema"]
        props, depth = app.v4_schema_contract_stats(schema)
        self.assertLessEqual(props, 10)
        self.assertLessEqual(depth, 2)
        self.assertNotIn("headline", json.dumps(schema))

    def test_exa_evidence_request_and_merge_are_subject_specific(self):
        original_key = app.EXA_API_KEY
        original_http = app.http
        calls = []
        names=[
            "hurling","go","kabaddi","carrom","fencing","korfball","sepak takraw","shogi","polo","jai alai"
        ]
        selected=[{
            "sector": app.V4_SECTORS[i], "normalized_subject": names[i],
            "central_knowledge_unit": f"history of {names[i]}",
            "central_claim": f"{names[i]} has a documented history",
            "research_focus": f"verify {names[i]}",
        } for i in range(10)]
        class FakeResp:
            ok=True; status=200; error=""
            data={
                "output":{"content":{
                    "research_complete":True,
                    "items":[{
                        "post_number":i, "evidence":f"Evidence for {names[i-1]}",
                        "source_urls":[f"https://example.com/source/{i}"],
                        "source_names":[f"Source {i}"],
                        "image_urls":[f"https://example.com/image/{i}.jpg"],
                        "image_source_page_urls":[f"https://example.com/page/{i}"],
                        "image_notes":[f"Photo of {names[i-1]}"]
                    } for i in range(1,11)]
                }},
                "results":[]
            }
        def fake_http(source, method, url, **kwargs):
            calls.append(kwargs.get("json_body")); return FakeResp()
        app.EXA_API_KEY="test-key"; app.http=fake_http
        try:
            evidence, results, status = app.v4_exa_evidence_research(None, selected, date(2026,9,23), "2026-09-23-AM")
            merged=app.v4_apply_evidence(selected,evidence,results)
        finally:
            app.EXA_API_KEY=original_key; app.http=original_http
        self.assertEqual(status,"ok")
        self.assertEqual(len(calls),1)
        self.assertEqual(len(merged),10)
        self.assertEqual(merged[0]["sources"][0]["url"],"https://example.com/source/1")
        self.assertEqual(merged[0]["research_images"][0]["source_page_url"],"https://example.com/page/1")
        self.assertEqual(merged[9]["research_images"][0]["url"],"https://example.com/image/10.jpg")

    def test_exa_schema_respects_real_provider_contract(self):
        schema = json.loads((app.V4_SCHEMAS_DIR / "exa_daily_sports_games_output_schema_v1.json").read_text(encoding="utf-8"))
        props, depth = app.v4_schema_contract_stats(schema)
        self.assertLessEqual(props, 10)
        self.assertLessEqual(depth, 2)
        evidence = json.loads((app.V4_SCHEMAS_DIR / "exa_selected_evidence_v4.json").read_text(encoding="utf-8"))
        eprops, edepth = app.v4_schema_contract_stats(evidence)
        self.assertLessEqual(eprops, 10)
        self.assertLessEqual(edepth, 2)

    def test_exa_package_validator_enforces_twenty_sectors(self):
        vocab = [
            "hurling", "go", "kabaddi", "carrom", "fencing", "korfball", "sepak takraw", "shogi",
            "polo", "jai alai", "biathlon", "capoeira", "pankration", "curling stone", "tee ball",
            "buzkashi", "gatka", "xiangqi", "sambo", "pelota",
        ]
        posts = []
        for index, sector in enumerate(app.V4_SECTORS, 1):
            token = vocab[index - 1]
            posts.append({
                "sector": sector,
                "normalized_subject": token,
                "central_knowledge_unit": f"{token} knowledge",
                "central_claim": f"{token} has a distinct documented history.",
                "research_focus": f"Research the documented history and rules of {token}."
            })
        package = {"calendar_date": "2026-09-23", "daily_complete": True, "posts": posts}
        ok, errors, normalized = app.v4_exa_validate_package(package, [], date(2026, 9, 23))
        self.assertTrue(ok, errors)
        self.assertEqual(len(normalized), 20)
        bad_package = dict(package)
        bad_package["daily_complete"] = False
        ok, errors, _ = app.v4_exa_validate_package(bad_package, [], date(2026, 9, 23))
        self.assertFalse(ok)
        self.assertIn("daily_complete_false", errors)

    def test_idempotency_publishes_only_once(self):
        state = {"publication": {}}
        calls = []

        def publisher():
            calls.append(1)
            return {"ok": True, "result": {"message_id": 9876}}

        first = app.v4_publish_with_idempotency(state, "run:evergreen:test", publisher)
        second = app.v4_publish_with_idempotency(state, "run:evergreen:test", publisher)
        self.assertTrue(first["ok"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["publication"]["run:evergreen:test"]["message_id"], 9876)

    def test_image_validator_accepts_https_in_offline_mode(self):
        candidate = {"url": "https://example.com/photo.jpg"}
        self.assertTrue(app.v4_validate_image_candidate(candidate, do_network=False))

    def test_publication_id_is_stable(self):
        a = app.v4_publication_id("2026-09-23-AM", "evergreen", "Football Origins")
        b = app.v4_publication_id("2026-09-23-AM", "evergreen", "Football Origins")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("2026-09-23-AM:evergreen:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
