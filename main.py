from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from sportsgames.config import APP_NAME, APP_VERSION
from sportsgames.discovery import is_video_game_contaminated
from sportsgames.state import claim_key, default_state, prune_state
from sportsgames.telegram import render_rich_html, visible_length
from sportsgames.utils import canonical_url, similarity

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("sports-games-hub")


def self_test() -> None:
    assert canonical_url("https://www.example.com/story/?utm_source=x&ref=y") == "example.com/story"
    assert is_video_game_contaminated("PlayStation patch notes")
    assert is_video_game_contaminated("Steam DLC announcement")
    assert not is_video_game_contaminated("Ludo official rules")
    assert not is_video_game_contaminated("new physical card game")
    assert not is_video_game_contaminated("history of gaming tables")
    assert similarity("Why does tennis use love?", "Origin of the word love in tennis") > 0.35

    state = default_state()
    candidate = {"subject": "tennis scoring terminology", "claim_or_event": "The term love has a documented historical origin.", "angle": "etymology"}
    key = claim_key(candidate["subject"], candidate["claim_or_event"], candidate["angle"])
    assert key not in state["claims"]
    state["claims"][key] = {**candidate, "claim": candidate["claim_or_event"]}
    assert key in state["claims"]

    story = {
        "format": "fact", "date_anchor": "", "headline": "Why This Sports Word Has a Surprising Origin",
        "dek": "A familiar sports term has a documented history.",
        "body": "Historical evidence traces the term through earlier usage before it took on its modern sporting meaning.",
        "why_interesting": "A word that looks ordinary today has a documented history connected to the sport.",
        "key_points": ["Historical origin"], "sources": ["https://example.com/rules"], "game_or_sport": "Tennis",
        "subject": "tennis terminology", "claim": candidate["claim_or_event"], "category": "evergreen_fact", "angle": "etymology",
    }
    rendered = render_rich_html(story)
    assert "DID YOU KNOW?" in rendered
    assert visible_length(rendered) < 32768
    assert "@TheSportsNewsroom" in rendered

    daily = {
        "format": "daily_next", "date_anchor": "2026-09-21", "headline": "Sports scheduled for 2026-09-21",
        "dek": "A dated guide.", "events": [
            {"sport": "Football", "event": "Example FC vs Example United", "competition": "League", "stage": "Round", "time_utc": "18:00 UTC", "location": "Dhaka", "importance": 90, "reason": "A notable fixture."},
            {"sport": "Cricket", "event": "Example A vs Example B", "competition": "Series", "stage": "Match", "time_utc": "09:00 UTC", "location": "", "importance": 80, "reason": "Important match."},
        ], "sources": ["https://example.com"],
    }
    daily_html = render_rich_html(daily)
    assert "NEXT UP" in daily_html and "BY SPORT" in daily_html

    old = default_state()
    old["queue"]["x"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}
    prune_state(old)
    assert "x" not in old["queue"]

    logger.info("Self-test passed for %s v%s", APP_NAME, APP_VERSION)


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return
    if args.self_test:
        self_test()
        return
    from sportsgames.pipeline import run_once
    run_once()


if __name__ == "__main__":
    main()
