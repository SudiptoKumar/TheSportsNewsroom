import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from sportsgames.discovery import is_video_game_contaminated
from sportsgames.editorial import preliminary_score
from sportsgames.state import claim_key, default_state, prune_state
from sportsgames.telegram import render_rich_html, visible_length, plain_caption
from sportsgames.utils import canonical_url, similarity


class CoreTests(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(canonical_url("https://www.example.com/a/?utm_source=x&ref=y"), "example.com/a")

    def test_video_game_filter(self):
        self.assertTrue(is_video_game_contaminated("PlayStation patch notes"))
        self.assertTrue(is_video_game_contaminated("Xbox console announcement"))
        self.assertTrue(is_video_game_contaminated("Steam DLC release"))
        self.assertFalse(is_video_game_contaminated("Ludo rule explanation"))
        self.assertFalse(is_video_game_contaminated("new board game release"))
        self.assertFalse(is_video_game_contaminated("history of gaming tables"))

    def test_similarity(self):
        self.assertGreater(similarity("Why does tennis use love?", "Where did the tennis term love come from?"), 0.35)

    def test_claim_key_stable(self):
        self.assertEqual(claim_key("UNO", "Draw card rule", "rule"), claim_key("UNO", "Draw card rule", "rule"))

    def test_preliminary_score(self):
        state = default_state()
        candidate = {
            "source_tier": 1,
            "score_dimensions": {"surprise": 5, "evergreen_fit": 5, "simplicity": 4, "curiosity": 5, "novelty_signal": 5, "usefulness": 4},
            "subject": "UNO rule",
            "claim_or_event": "official rule",
            "angle": "rule",
        }
        self.assertGreaterEqual(preliminary_score(candidate, state), 30)

    def test_render_fact(self):
        story = {
            "format": "rule_check", "date_anchor": "", "headline": "A Rule Worth Checking",
            "dek": "This game has an official rule many casual players overlook.",
            "body": "The rule should be described using the official rulebook or another directly supporting source.",
            "why_interesting": "Common house rules can differ from official rules.", "key_points": ["Official rule"],
            "sources": ["https://example.com/rules"], "game_or_sport": "UNO", "subject": "UNO rules",
            "claim": "A rule", "category": "rule_check", "angle": "rule",
        }
        html = render_rich_html(story)
        self.assertIn("RULE CHECK", html)
        self.assertIn("@TheSportsNewsroom", html)
        self.assertLess(visible_length(html), 32768)
        self.assertLessEqual(len(plain_caption(html)), 1024)

    def test_render_daily(self):
        story = {
            "format": "daily_next", "date_anchor": "2026-09-21", "headline": "Sports scheduled for 2026-09-21",
            "dek": "A dated guide.", "events": [
                {"sport": "Football", "event": "Club A vs Club B", "competition": "League", "stage": "Round", "time_utc": "18:00 UTC", "location": "", "importance": 90, "reason": "Notable fixture."},
                {"sport": "Cricket", "event": "Team A vs Team B", "competition": "Series", "stage": "Match", "time_utc": "09:00 UTC", "location": "", "importance": 80, "reason": "Important match."},
            ], "sources": ["https://example.com"],
        }
        html = render_rich_html(story)
        self.assertIn("NEXT UP", html)
        self.assertIn("BY SPORT", html)
        self.assertIn("Club A vs Club B", html)

    def test_prune_queue(self):
        state = default_state()
        state["queue"]["old"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}
        prune_state(state)
        self.assertNotIn("old", state["queue"])


if __name__ == "__main__":
    unittest.main()
