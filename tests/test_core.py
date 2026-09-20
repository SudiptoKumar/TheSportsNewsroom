import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sportsgames.discovery import is_video_game_contaminated
from sportsgames.observability import AiBudget, PipelineReport, balanced_pool, select_quality_gated
from sportsgames.state import claim_key, core_claim_key, default_state, prune_state
from sportsgames.telegram import render_rich_html, visible_length
from sportsgames.utils import canonical_url, similarity


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
        # Two mandatory calls remain protected from discovery.
        self.assertEqual(budget.remaining("discovery"), 2)
        self.assertTrue(budget.take("discovery"))
        self.assertTrue(budget.take("discovery"))
        self.assertEqual(budget.remaining("discovery"), 0)
        self.assertTrue(budget.take("mandatory"))

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

    def test_report_tracks_pipeline_events(self):
        report = PipelineReport()
        report.count("discovery.results", 3)
        report.reject("filter", "video_game", "Console update", candidate_id="c1")
        report.source("https://example.com/a", True, 120, 2, elapsed=0.12)
        report.publish("fact", "A useful fact", True)
        rendered = report.render()
        self.assertIn("discovery", rendered.lower())
        self.assertIn("video_game", rendered)
        self.assertIn("A useful fact", rendered)

    def test_prune_queue(self):
        state = default_state()
        state["queue"]["old"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}
        prune_state(state)
        self.assertNotIn("old", state["queue"])


if __name__ == "__main__": unittest.main()
