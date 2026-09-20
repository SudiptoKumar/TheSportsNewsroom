import unittest
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sportsgames.discovery import is_video_game_contaminated
from sportsgames.editorial import classify_candidates
from sportsgames.lanes import plan_mandatory_lanes
from sportsgames.pipeline import _evergreen_candidates, _publish, _run_daily_lane
from sportsgames.observability import AiBudget, PipelineReport, balanced_pool, select_quality_gated
from sportsgames.schemas import CANDIDATE_SCHEMA
from sportsgames.state import claim_key, core_claim_key, default_state, ensure_state_shape, load_state, mark_daily, prune_state
from sportsgames.telegram import render_rich_html, visible_length
from sportsgames.utils import canonical_url, similarity


class FakeClassifierProvider:
    def __init__(self, missing_first=False):
        self.calls = []
        self.missing_first = missing_first

    def ai(self, *, system, user, schema_name, schema, max_tokens, lane):
        ids = []
        for line in user.splitlines():
            if line.startswith("ID: "):
                ids.append(int(line.split(":", 1)[1].strip()))
        self.calls.append(ids)
        if self.missing_first and len(self.calls) == 1:
            ids = ids[:1]
        items = []
        for item_id in ids:
            items.append({
                "id": item_id,
                "kind": "rule",
                "domain": "sports",
                "type": "rule",
                "category": "rule_check",
                "angle": "rule",
                "game_or_sport": "Banana Ball",
                "subject": "Banana Ball rules",
                "claim_or_event": "Banana Ball has a documented rule difference.",
                "source_urls": [],
                "why_interesting": "Its rules differ from standard baseball.",
                "date_anchor": "",
            })
        return {"items": items}

class FakeCerebras:
    def __init__(self):
        self.calls = 0
        self.kwargs = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self.create

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.calls == 1:
            raise RuntimeError("HTTP 500")
        return type("R", (), {
            "choices": [type("C", (), {
                "message": type("M", (), {"content": '{"ok": true}'})(),
                "finish_reason": "stop",
            })()]
        })()



class CoreTests(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(canonical_url("https://www.example.com/a/?utm_source=x&ref=y"), "example.com/a")

    def test_video_game_filter(self):
        self.assertTrue(is_video_game_contaminated("PlayStation patch notes"))
        self.assertTrue(is_video_game_contaminated("Xbox console announcement"))
        self.assertFalse(is_video_game_contaminated("Ludo official rules"))
        self.assertFalse(is_video_game_contaminated("new board game release"))

    def test_similarity(self):
        self.assertGreater(similarity("Why does tennis use love?", "Where did the tennis term love come from?"), 0.35)

    def test_claim_keys(self):
        self.assertEqual(claim_key("UNO", "Draw card rule", "rule"), claim_key("UNO", "Draw card rule", "rule"))
        self.assertEqual(core_claim_key("UNO", "Draw card rule"), core_claim_key("UNO", "Draw card rule"))

    def test_budget_reserves_mandatory(self):
        report = PipelineReport(); budget = AiBudget(6, 2, report)
        self.assertTrue(budget.take("discovery"))
        self.assertTrue(budget.take("discovery"))
        self.assertEqual(budget.remaining("discovery"), 2)
        self.assertTrue(budget.take("discovery"))
        self.assertTrue(budget.take("discovery"))
        self.assertEqual(budget.remaining("discovery"), 0)
        self.assertTrue(budget.take("mandatory"))

    def test_budget_releases_unused_mandatory_reserve(self):
        report = PipelineReport(); budget = AiBudget(10, 4, report)
        self.assertTrue(budget.take("mandatory"))
        self.assertEqual(budget.release_unused_mandatory(), 3)
        self.assertEqual(budget.remaining("discovery"), 9)

    def test_balanced_pool(self):
        items = [
            {"family": "facts", "s": 9}, {"family": "games", "s": 8}, {"family": "rules", "s": 7},
            {"family": "facts", "s": 6}, {"family": "games", "s": 5},
        ]
        pool = balanced_pool(items, lambda x: x["family"], lambda x: x["s"], {"facts": 1, "games": 1, "rules": 1}, 4)
        self.assertEqual(len(pool), 4)
        self.assertEqual({x["family"] for x in pool[:3]}, {"facts", "games", "rules"})

    def test_quality_gate(self):
        items = [{"s": 10}, {"s": 15}]
        self.assertEqual(select_quality_gated(items, lambda x: x["s"], 20, 2), [])

    def test_render(self):
        story = {
            "format": "rule_check", "date_anchor": "", "headline": "A Rule Worth Checking",
            "dek": "This game has an official rule many casual players overlook.",
            "body": "The rule should be described using the official rulebook or another directly supporting source.",
            "why_interesting": "Common house rules can differ from official rules.", "key_points": ["Official rule"],
            "sources": ["https://example.com/rules"], "game_or_sport": "UNO", "subject": "UNO rules",
            "claim": "A rule", "category": "rule_check", "angle": "rule"
        }
        html = render_rich_html(story)
        self.assertIn("RULE CHECK", html)
        self.assertIn("@TheSportsNewsroom", html)
        self.assertLess(visible_length(html), 32768)

    def test_report_asserts_on_any_stage_unaccounted(self):
        report = PipelineReport()
        report.candidate_gate("editorial", ["c1", "c2"])
        with self.assertRaises(RuntimeError):
            report.assert_no_unaccounted()

    def test_report_tracks_pipeline_events(self):
        report = PipelineReport()
        report.count("discovery.results", 3)
        report.reject("filter", "video_game", "Console update", candidate_id="c1")
        report.source("https://example.com/a", True, 120, 2, elapsed=0.12)
        report.publish("fact", "A useful fact", True)
        report.candidate("classification", "c1", "rejected", "video_game")
        self.assertEqual(report.candidate_gate("classification", ["c1"]), [])
        rendered = report.render()
        self.assertIn("discovery", rendered.lower())
        self.assertIn("video_game", rendered)
        self.assertIn("A useful fact", rendered)

    def test_prune_queue(self):
        state = default_state()
        state["queue"]["old"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}
        prune_state(state)
        self.assertNotIn("old", state["queue"])

    def test_state_migrates_legacy_daily_flags(self):
        old = {"schema_version": 2, "daily_flags": {"next:2026-09-21": "2026-09-20T20:00:00+06:00"}}
        migrated = ensure_state_shape(old)
        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(migrated["mandatory_lanes"]["next:2026-09-21"]["status"], "published")
        self.assertIn("publish_intents", migrated)

    def test_mandatory_planner_catches_up_both_lanes_early_morning(self):
        tz = ZoneInfo("Asia/Dhaka")
        state = default_state()
        now = datetime(2026, 9, 21, 1, 25, tzinfo=tz)
        jobs = plan_mandatory_lanes(state, now)
        self.assertEqual({j["lane"] for j in jobs}, {"next", "past"})
        self.assertEqual({j["target_date"].isoformat() for j in jobs}, {"2026-09-21", "2026-09-20"})

    def test_mandatory_planner_expires_missed_windows(self):
        tz = ZoneInfo("Asia/Dhaka")
        state = default_state()
        now = datetime(2026, 9, 21, 8, 0, tzinfo=tz)
        jobs = plan_mandatory_lanes(state, now)
        self.assertEqual([(j["lane"], j["target_date"].isoformat()) for j in jobs], [("next", "2026-09-22")])
        self.assertEqual(state["mandatory_lanes"]["next:2026-09-21"]["status"], "expired")
        self.assertEqual(state["mandatory_lanes"]["past:2026-09-20"]["status"], "expired")

    def test_mandatory_planner_gives_up_after_max_attempts(self):
        tz = ZoneInfo("Asia/Dhaka")
        state = default_state()
        state["mandatory_lanes"]["next:2026-09-21"] = {
            "lane": "next", "target_date": "2026-09-21", "status": "no_data", "attempts": 3,
            "updated_at": "2026-09-21T04:00:00+06:00", "last_error": "no_candidates", "trace": []
        }
        jobs = plan_mandatory_lanes(state, datetime(2026, 9, 21, 4, 30, tzinfo=tz))
        self.assertEqual([(j["lane"], j["target_date"].isoformat()) for j in jobs], [("past", "2026-09-20")])
        self.assertEqual(state["mandatory_lanes"]["next:2026-09-21"]["status"], "gave_up")

    def test_mandatory_planner_retries_no_data(self):
        tz = ZoneInfo("Asia/Dhaka")
        state = default_state()
        state["mandatory_lanes"]["next:2026-09-21"] = {
            "lane": "next", "target_date": "2026-09-21", "status": "no_data", "attempts": 1,
            "updated_at": "2026-09-21T00:00:00+06:00", "last_error": "", "trace": []
        }
        now = datetime(2026, 9, 21, 4, 0, tzinfo=tz)
        jobs = plan_mandatory_lanes(state, now)
        self.assertEqual(len(jobs), 2)
        next_job = next(j for j in jobs if j["lane"] == "next")
        self.assertEqual(next_job["prior_status"], "no_data")

    def test_classifier_batches_of_eight(self):
        candidates = [
            {"candidate_id": str(i), "title": f"Candidate {i}", "query_family": "facts", "source": "X", "url": f"https://example.com/{i}", "excerpt": "evidence"}
            for i in range(36)
        ]
        provider = FakeClassifierProvider()
        report = PipelineReport()
        output = classify_candidates(provider, candidates, "test", report=report)
        self.assertEqual(len(provider.calls), 5)
        self.assertEqual([len(x) for x in provider.calls], [8, 8, 8, 8, 4])
        self.assertEqual(len(output), 36)
        self.assertEqual(report.counts["classification.unreturned"], 0)
        self.assertEqual(report.counts["ledger.classification.unaccounted"], 0)

    def test_classifier_missing_only_retry(self):
        candidates = [
            {"candidate_id": str(i), "title": f"Candidate {i}", "query_family": "rules", "source": "X", "url": f"https://example.com/{i}", "excerpt": "evidence"}
            for i in range(8)
        ]
        provider = FakeClassifierProvider(missing_first=True)
        report = PipelineReport()
        output = classify_candidates(provider, candidates, "test", report=report)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0], list(range(1, 9)))
        self.assertEqual(provider.calls[1], list(range(1, 8)))
        self.assertEqual(len(output), 8)
        self.assertEqual(report.counts["ledger.classification.unaccounted"], 0)

    def test_candidate_schema_uses_hard_taxonomy_enums(self):
        props = CANDIDATE_SCHEMA["properties"]["items"]["items"]["properties"]
        self.assertIn("rule_check", props["category"]["enum"])
        self.assertIn("rule", props["angle"]["enum"])
        self.assertIn("physical_games", props["domain"]["enum"])


    def test_publish_recovery_restores_missing_post_metadata(self):
        import sportsgames.pipeline as pipeline
        state = default_state()
        story = {
            "format": "fact", "date_anchor": "", "headline": "Recovered fact",
            "dek": "A durable fact.", "body": "A supported durable fact.",
            "why_interesting": "It remains useful.", "key_points": ["Fact"],
            "sources": ["https://example.com/recovered"], "game_or_sport": "Chess",
            "subject": "Chess", "claim": "A durable fact.",
            "category": "interesting_fact", "angle": "weird",
            "candidate": {"candidate_id": "recover-c1", "canonical": "example.com/recovered"},
        }
        intent_key = pipeline._publish_intent_key(story)
        pipeline.set_publish_intent(
            state, intent_key, status="published", attempts=1, message_id=77,
            story_digest="digest-1", updated_at="2026-09-21T02:00:00+06:00",
        )
        report = PipelineReport()
        with patch.object(pipeline, "save_state") as save_mock:
            self.assertTrue(pipeline._publish(state, story, 1, report))
        self.assertEqual(len(state["posts"]), 1)
        self.assertEqual(state["posts"][0]["message_id"], 77)
        self.assertEqual(state["posts"][0]["publish_intent_key"], intent_key)
        self.assertEqual(report.counts["publish.recovered_metadata"], 1)
        self.assertTrue(save_mock.called)

    def test_publish_intent_guard_blocks_unknown_resend(self):
        state = default_state()
        state["publish_intents"]["discovery:c1"] = {"status": "unknown", "attempts": 1}
        story = {
            "format": "fact", "date_anchor": "", "headline": "Guarded Story",
            "dek": "Evidence-backed summary.", "body": "A supported statement.",
            "why_interesting": "It is durable.", "key_points": [], "sources": [],
            "game_or_sport": "Chess", "subject": "Chess", "claim": "A claim",
            "category": "evergreen_fact", "angle": "weird",
            "candidate": {"candidate_id": "c1", "canonical": "example.com/a"},
        }
        report = PipelineReport()
        with patch("sportsgames.pipeline.send_story") as send_story:
            self.assertFalse(_publish(state, story, 1, report))
            send_story.assert_not_called()

    def test_publish_sets_intent_before_success(self):
        state = default_state()
        story = {
            "format": "fact", "date_anchor": "", "headline": "Guarded Story",
            "dek": "Evidence-backed summary.", "body": "A supported statement.",
            "why_interesting": "It is durable.", "key_points": [], "sources": [],
            "game_or_sport": "Chess", "subject": "Chess", "claim": "A claim",
            "category": "evergreen_fact", "angle": "weird",
            "candidate": {"candidate_id": "c2", "canonical": "example.com/a"},
        }
        report = PipelineReport()
        with patch("sportsgames.pipeline.save_state"), \
             patch("sportsgames.pipeline.create_visual", return_value="/tmp/nonexistent-sports-newsroom-image.jpg"), \
             patch("sportsgames.pipeline.send_story", return_value={"ok": True, "result": {"message_id": 7}}), \
             patch("sportsgames.pipeline.remember_published_url"):
            self.assertTrue(_publish(state, story, 1, report))
        self.assertEqual(state["publish_intents"]["discovery:c2"]["status"], "published")
        self.assertEqual(state["publish_intents"]["discovery:c2"]["message_id"], 7)
        self.assertEqual(len(state["posts"]), 1)

    def test_daily_executor_persists_ready_trace(self):
        from datetime import date
        import sportsgames.pipeline as pipeline

        tz = ZoneInfo("Asia/Dhaka")
        state = default_state()
        job = {
            "lane": "next", "target_date": date(2026, 9, 22), "key": "next:2026-09-22",
            "prior_status": "", "window_open": "2026-09-21T06:00:00", "window_close": "2026-09-22T06:00:00",
        }
        candidate = {
            "candidate_id": "daily-1", "title": "Fixture", "source": "Official", "url": "https://example.com/fixture", "excerpt": "Event on 2026-09-22.",
        }
        plan = {
            "mode": "NEXT", "target_date": "2026-09-22", "events": [{
                "sport": "Athletics", "event": "Meet", "date": "2026-09-22", "time_utc": "10:00",
                "competition": "Meet", "stage": "Final", "location": "Venue", "status": "scheduled", "importance": 90,
                "reason": "Notable", "source_urls": [candidate["url"]],
                "evidence_packets": [{"url": candidate["url"], "tier": 1, "text": candidate["excerpt"]}],
            }]
        }
        story = {
            "format": "daily_next", "date_anchor": "2026-09-22", "headline": "NEXT UP",
            "dek": "Scheduled events.", "body": "The date has scheduled events.",
            "why_interesting": "A dated reference.", "key_points": ["Athletics: Meet"],
            "sources": [candidate["url"]], "candidate": {
                "evidence_packets": plan["events"][0]["evidence_packets"], "claim_or_event": "Sports calendar for 2026-09-22"
            }
        }
        class FakeProviders: pass
        report = PipelineReport()
        with patch.object(pipeline, "discover_next_sports", return_value=[candidate]), \
             patch.object(pipeline, "build_daily_plan", return_value=plan), \
             patch.object(pipeline, "generate_daily_story", return_value=story), \
             patch.object(pipeline, "verify_story_against_evidence", return_value=True), \
             patch.object(pipeline, "save_state"):
            got, status, reason = _run_daily_lane(FakeProviders(), state, job, report)
        self.assertIsNotNone(got)
        self.assertEqual(status, "ready")
        self.assertEqual(reason, "")
        self.assertEqual(state["mandatory_lanes"][job["key"]]["status"], "ready")
        self.assertTrue(any(item.get("step") == "ready" for item in state["mandatory_lanes"][job["key"]]["trace"]))

    def test_classification_capacity_accounts_for_100_to_36_without_loss(self):
        candidates = [
            {"candidate_id": f"cid-{i}", "title": f"Candidate {i}", "query_family": "facts",
             "source": "X", "url": f"https://example.com/{i}", "excerpt": "evidence", "prelim_score": 100-i}
            for i in range(100)
        ]
        report = PipelineReport()
        output = classify_candidates(FakeClassifierProvider(), candidates, "integration", report=report)
        self.assertEqual(len(output), 36)
        self.assertEqual(report.counts["classification.input"], 36)
        self.assertEqual(report.counts["classification.capacity_rejected"], 64)
        self.assertEqual(report.counts["ledger.classification.unaccounted"], 0)
        self.assertEqual(report.counts["classification.output"], 36)
        for cid in (f"cid-{i}" for i in range(100)):
            self.assertIn(cid, report.candidate_ledger)
            self.assertIn("classification", report.candidate_ledger[cid]["stages"])

    def test_terminal_gate_catches_a_candidate_that_has_only_intermediate_state(self):
        report = PipelineReport()
        report.candidate("classification", "c1", "classified")
        report.candidate_terminal("c2", "rejected", "capacity")
        self.assertEqual(report.finalize_candidates(["c1", "c2"]), ["c1"])
        self.assertEqual(report.counts["ledger.terminal.unaccounted"], 1)

    def test_terminal_gate_zero_after_complete_fixture_funnel(self):
        report = PipelineReport()
        ids = [f"c{i}" for i in range(100)]
        for cid in ids:
            report.candidate("raw", cid, "available")
            report.candidate("pre_filter", cid, "accepted")
        for cid in ids[:64]:
            report.candidate("classification", cid, "rejected", "classification_capacity")
            report.candidate_terminal(cid, "rejected", "classification_capacity")
        for cid in ids[64:97]:
            report.candidate("classification", cid, "classified")
            report.candidate("verification", cid, "rejected", "not_verification_pool")
            report.candidate_terminal(cid, "rejected", "not_verification_pool")
        for cid in ids[97:99]:
            report.candidate("classification", cid, "classified")
            report.candidate("verification", cid, "verified")
            report.candidate("editorial", cid, "rejected", "editorial_capacity")
            report.candidate_terminal(cid, "rejected", "editorial_capacity")
        report.candidate("classification", ids[99], "classified")
        report.candidate("verification", ids[99], "verified")
        report.candidate("editorial", ids[99], "selected")
        report.candidate("publish", ids[99], "published")
        report.candidate_terminal(ids[99], "published", "telegram_confirmed")
        self.assertEqual(report.finalize_candidates(ids), [])
        self.assertEqual(report.counts.get("ledger.terminal.unaccounted", 0), 0)
        self.assertEqual(report.problems(), [])

    def test_provider_ai_retries_consume_actual_api_budget(self):
        from sportsgames.providers import Providers
        client = FakeCerebras()
        report = PipelineReport()
        budget = AiBudget(2, 0, report)
        provider = Providers(exa=None, cerebras=client, report=report, ai_budget=budget)
        result = provider.ai(system="s", user="u", schema_name="x", schema={"type": "object"}, max_tokens=100, lane="discovery")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.calls, 2)
        self.assertEqual(budget.used, 2)
        self.assertEqual(report.counts["ai.api_attempts"], 2)

    def test_corrupt_existing_state_fails_closed(self):
        import tempfile
        import sportsgames.state as state_module
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "knowledge_state.json"
            path.write_text("{not-json", encoding="utf-8")
            old = state_module.CONFIG
            state_module.CONFIG = type(old)(
                app_name=old.app_name, version=old.version, channel=old.channel, tz=old.tz,
                state_file=path, published_file=path.with_name("published_urls.txt"),
                run_report_file=path.with_name("last_run_report.json"),
            )
            try:
                with self.assertRaises(RuntimeError):
                    load_state()
            finally:
                state_module.CONFIG = old

    def test_budget_report_preserves_initial_reserve_after_release(self):
        report = PipelineReport()
        budget = AiBudget(10, 4, report)
        budget.take("mandatory")
        budget.release_unused_mandatory()
        self.assertEqual(report.ai["reserved"], 4)
        self.assertEqual(report.ai["reserve_held"], 1)
        self.assertEqual(report.ai["reserve_released"], 3)

    def test_select_candidates_accounts_prepared_but_not_selected(self):
        from sportsgames.editorial import select_candidates
        state = default_state()
        candidates = []
        for i in range(3):
            candidates.append({
                "candidate_id": f"sel-{i}", "title": f"Candidate {i}", "query_family": "facts",
                "category": "interesting_fact", "angle": "weird", "subject": f"Subject {i}",
                "claim_or_event": f"A supported fact {i}", "prelim_score": 12,
                "verification": {"status": "verified", "confidence": 95}, "evidence_packets": [{"tier": 1}],
            })
        report = PipelineReport()
        selected = select_candidates(None, candidates, state, max_items=1, report=report)
        self.assertEqual(len(selected), 1)
        # Selection is intermediate. The publish stage supplies the terminal outcome.
        chosen = selected[0]["candidate_id"]
        report.candidate("publish", chosen, "published")
        report.candidate_terminal(chosen, "published", "fixture")
        self.assertEqual(report.finalize_candidates([c["candidate_id"] for c in candidates]), [])

    def test_full_fixture_run_accounts_106_raw_candidates_to_terminal(self):
        import sportsgames.editorial as editorial
        import sportsgames.pipeline as pipeline

        evergreen = [
            {"candidate_id": f"e{i}", "title": f"Evergreen {i}", "query_family": "facts",
             "source": "X", "url": f"https://example.com/e{i}", "excerpt": "evidence", "prelim_score": 100-i}
            for i in range(100)
        ]
        history = [
            {"candidate_id": f"h{i}", "title": f"History {i}", "query_family": "history",
             "source": "Y", "url": f"https://example.com/h{i}", "excerpt": "history evidence", "prelim_score": 10-i}
            for i in range(6)
        ]
        report = PipelineReport()
        state = default_state()
        provider = FakeClassifierProvider()
        with patch.object(pipeline, "discover_evergreen", return_value=evergreen), \
             patch.object(pipeline, "discover_historical_date", return_value=history), \
             patch.object(editorial, "verify_candidate", return_value=(True, {"status": "verified", "confidence": 95, "reason": "ok"})), \
             patch.object(pipeline, "save_state"):
            verified, raw_ids = _evergreen_candidates(provider, state, report)
            self.assertEqual(len(raw_ids), 106)
            self.assertEqual(report.counts["candidate.raw"], 106)
            self.assertEqual(report.counts["candidate.pre_filtered"], 100)
            self.assertEqual(report.counts["candidate.capacity_rejected"], 6)
            self.assertEqual(report.counts["classification.input"], 36)
            self.assertEqual(report.counts["classification.capacity_rejected"], 64)
            self.assertEqual(report.counts["ledger.classification.unaccounted"], 0)
            selected = editorial.select_candidates(None, verified, state, max_items=1, report=report)
            self.assertEqual(len(selected), 1)
            chosen = selected[0]["candidate_id"]
            report.candidate("publish", chosen, "published")
            report.candidate_terminal(chosen, "published", "fixture_publish")

        missing = report.finalize_candidates(raw_ids)
        self.assertEqual(missing, [])
        self.assertEqual(report.problems(), [])
        self.assertEqual(report.counts.get("ledger.terminal.unaccounted", 0), 0)

    def test_telegram_429_retry_rewinds_upload(self):
        import io
        import sportsgames.providers as providers
        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
            def json(self):
                return self._payload
        class FakeHTTP:
            def __init__(self):
                self.calls = []
            def post(self, url, data=None, files=None, timeout=None):
                handle = files["photo"]
                self.calls.append(handle.read())
                if len(self.calls) == 1:
                    return Response(429, {"ok": False, "parameters": {"retry_after": 0}})
                return Response(200, {"ok": True, "result": {"message_id": 1}})
        old_http = providers.HTTP
        old_token = providers.TELEGRAM_BOT_TOKEN
        fake = FakeHTTP()
        providers.HTTP = fake
        providers.TELEGRAM_BOT_TOKEN = "test"
        try:
            upload = io.BytesIO(b"photo-bytes")
            result = providers.telegram_call("sendPhoto", data={"chat_id": "x"}, files={"photo": upload})
        finally:
            providers.HTTP = old_http
            providers.TELEGRAM_BOT_TOKEN = old_token
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, [b"photo-bytes", b"photo-bytes"])

    def test_telegram_5xx_is_uncertain_and_not_retried(self):
        import sportsgames.providers as providers
        class Response:
            status_code = 500
            def json(self):
                return {"ok": False, "description": "server error"}
        class Http:
            def __init__(self): self.calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                return Response()
        http = Http()
        old_token, old_http = providers.TELEGRAM_BOT_TOKEN, providers.HTTP
        providers.TELEGRAM_BOT_TOKEN, providers.HTTP = "token", http
        try:
            result = providers.telegram_call("sendPhoto")
        finally:
            providers.TELEGRAM_BOT_TOKEN, providers.HTTP = old_token, old_http
        self.assertTrue(result["_delivery_uncertain"])
        self.assertEqual(http.calls, 1)

    def test_telegram_transport_failure_is_uncertain_and_not_retried(self):
        import sportsgames.providers as providers
        class Http:
            def __init__(self): self.calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("read timeout")
        http = Http()
        old_token, old_http = providers.TELEGRAM_BOT_TOKEN, providers.HTTP
        providers.TELEGRAM_BOT_TOKEN, providers.HTTP = "token", http
        try:
            result = providers.telegram_call("sendPhoto")
        finally:
            providers.TELEGRAM_BOT_TOKEN, providers.HTTP = old_token, old_http
        self.assertTrue(result["_delivery_uncertain"])
        self.assertEqual(http.calls, 1)

    def test_run_once_fixture_completes_and_publishes_selected_candidate(self):
        import sportsgames.pipeline as pipeline

        state = default_state()
        candidate = {
            "candidate_id": "run-c1", "title": "A durable fact", "query_family": "facts",
            "category": "interesting_fact", "angle": "weird", "subject": "Chess",
            "claim_or_event": "Chess is a documented board game.", "prelim_score": 20,
            "verification": {"status": "verified", "confidence": 95},
            "evidence_packets": [{"tier": 1}], "game_or_sport": "Chess",
            "source": "Example", "url": "https://example.com/a", "canonical": "example.com/a",
            "excerpt": "Chess is a board game.",
        }
        story = {
            "format": "fact", "date_anchor": "", "headline": "Chess fact",
            "dek": "A durable fact.", "body": "Chess is a board game.",
            "why_interesting": "It is evergreen.", "key_points": ["Board game"],
            "sources": ["https://example.com/a"], "game_or_sport": "Chess",
            "subject": "Chess", "claim": "Chess is a documented board game.",
            "category": "interesting_fact", "angle": "weird", "candidate": candidate,
        }
        class FakeProviders: pass
        with patch.object(pipeline, "load_state", return_value=state), \
             patch.object(pipeline, "save_state"), \
             patch.object(pipeline, "Providers") as provider_cls, \
             patch.object(pipeline, "plan_mandatory_lanes", return_value=[]), \
             patch.object(pipeline, "_evergreen_candidates", return_value=([candidate], [candidate["candidate_id"]])), \
             patch.object(pipeline, "generate_story", return_value=story), \
             patch.object(pipeline, "_publish", return_value=True), \
             patch.object(pipeline, "update_source_health"), \
             patch.object(pipeline, "record_run_summary"), \
             patch.object(pipeline, "discovery_posts_today", return_value=0):
            provider_cls.from_env.return_value = FakeProviders()
            self.assertEqual(pipeline.run_once(), 1)
        self.assertEqual(state["candidate_index"]["run-c1"]["outcome"], "published")


if __name__ == "__main__":
    unittest.main()
