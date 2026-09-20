from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

# Make src importable when running `python main.py` from the repository root.
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
    assert is_video_game_contaminated("Xbox game announcement")
    assert not is_video_game_contaminated("Ludo official rules")
    assert not is_video_game_contaminated("new physical card game")
    assert similarity("Why does tennis use love?", "Origin of the word love in tennis") > 0.35

    state = default_state()
    candidate = {"subject": "tennis scoring terminology", "claim_or_event": "The term love has a documented historical origin.", "angle": "etymology"}
    key = claim_key(candidate["subject"], candidate["claim_or_event"], candidate["angle"])
    assert key not in state["claims"]
    state["claims"][key] = {**candidate}
    assert key in state["claims"]

    story = {
        "format": "fact",
        "date_anchor": "",
        "headline": "Why This Sports Word Has a Surprising Origin",
        "dek": "A familiar sports term has a documented history.",
        "body": "Historical evidence traces the term through earlier usage before it took on its modern sporting meaning.",
        "why_interesting": "A word that looks ordinary today has a history connected to the development of the sport.",
        "key_points": ["Historical origin"],
        "sources": ["https://www.britannica.com/sports/tennis"],
        "game_or_sport": "Tennis",
        "subject": "tennis terminology",
        "claim": candidate["claim_or_event"],
        "category": "evergreen_fact",
        "angle": "etymology",
    }
    rendered = render_rich_html(story)
    assert "DID YOU KNOW?" in rendered
    assert visible_length(rendered) < 32768
    assert "@TheSportsNewsroom" in rendered

    # History/date language should be explicit rather than disposable.
    historical = dict(story)
    historical.update({"format": "on_this_date", "date_anchor": "20 September 1926"})
    rendered_history = render_rich_html(historical)
    assert "20 September 1926" in rendered_history

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
