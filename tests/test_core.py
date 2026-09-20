import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from sportsgames.discovery import is_video_game_contaminated
from sportsgames.state import claim_key, default_state, prune_state
from sportsgames.telegram import render_rich_html, visible_length
from sportsgames.utils import canonical_url, similarity


class CoreTests(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(canonical_url("https://www.example.com/a/?utm_source=x&ref=y"), "example.com/a")

    def test_video_game_filter(self):
        self.assertTrue(is_video_game_contaminated("PlayStation patch notes"))
        self.assertTrue(is_video_game_contaminated("Xbox console announcement"))
        self.assertFalse(is_video_game_contaminated("Ludo rule explanation"))
        self.assertFalse(is_video_game_contaminated("new board game release"))

    def test_similarity(self):
        self.assertGreater(similarity("Why does tennis use love?", "Where did the tennis term love come from?"), 0.35)

    def test_claim_key_stable(self):
        self.assertEqual(claim_key("UNO", "Draw card rule", "rule"), claim_key("UNO", "Draw card rule", "rule"))

    def test_render(self):
        story = {
            "format": "rule_check", "date_anchor": "", "headline": "A Rule Worth Checking", "dek": "This game has an official rule many casual players overlook.",
            "body": "The rule should be described using the official rulebook or another directly supporting source.",
            "why_interesting": "Common house rules can differ from official rules.", "key_points": ["Official rule"],
            "sources": ["https://example.com/rules"], "game_or_sport": "UNO", "subject": "UNO rules", "claim": "A rule", "category": "rule_check", "angle": "rule"
        }
        html = render_rich_html(story)
        self.assertIn("RULE CHECK", html)
        self.assertIn("@TheSportsNewsroom", html)
        self.assertLess(visible_length(html), 32768)

    def test_prune_queue(self):
        state = default_state()
        state["queue"]["old"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}
        prune_state(state)
        self.assertNotIn("old", state["queue"])


if __name__ == "__main__":
    unittest.main()
