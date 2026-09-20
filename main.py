from __future__ import annotations

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path: sys.path.insert(0, SRC)

from sportsgames.config import APP_NAME, APP_VERSION
from sportsgames.discovery import is_video_game_contaminated
from sportsgames.observability import AiBudget, PipelineReport, balanced_pool, select_quality_gated
from sportsgames.state import claim_key, core_claim_key, default_state, prune_state
from sportsgames.telegram import render_rich_html, visible_length
from sportsgames.utils import canonical_url, similarity

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("sports-games-hub")


def self_test() -> None:
    assert canonical_url("https://www.example.com/a/?utm_source=x&ref=y") == "example.com/a"
    assert is_video_game_contaminated("PlayStation patch notes")
    assert is_video_game_contaminated("Xbox console announcement")
    assert not is_video_game_contaminated("Ludo official rules")
    assert not is_video_game_contaminated("new physical card game")
    assert similarity("Why does tennis use love?", "Origin of the word love in tennis") > 0.35

    state = default_state()
    candidate = {"subject": "tennis scoring terminology", "claim_or_event": "The term love has a documented historical origin.", "angle": "etymology"}
    k = claim_key(candidate["subject"], candidate["claim_or_event"], candidate["angle"])
    ck = core_claim_key(candidate["subject"], candidate["claim_or_event"])
    assert k not in state["claims"] and ck not in state["claims"]
    state["claims"][k] = {**candidate}
    assert k in state["claims"]

    # Budget: discovery cannot consume the reserved mandatory slice.
    report = PipelineReport(); budget = AiBudget(10, 4, report)
    assert budget.take("discovery"); assert budget.take("mandatory")
    for _ in range(5): budget.take("discovery")
    assert budget.remaining("discovery") >= 0

    # Quality gate and diverse pool helpers.
    items = [
        {"family": "facts", "s": 9}, {"family": "games", "s": 8}, {"family": "rules", "s": 7},
        {"family": "facts", "s": 6}, {"family": "games", "s": 5},
    ]
    pool = balanced_pool(items, lambda x: x["family"], lambda x: x["s"], {"facts": 1, "games": 1, "rules": 1}, 4)
    assert len(pool) == 4 and len({x["family"] for x in pool[:3]}) == 3
    assert len(select_quality_gated(items, lambda x: x["s"], 20, 2)) == 0

    story = {
        "format": "rule_check", "date_anchor": "", "headline": "A Rule Worth Checking",
        "dek": "This game has an official rule many casual players overlook.",
        "body": "The rule should be described using the official rulebook or another directly supporting source.",
        "why_interesting": "Common house rules can differ from official rules.", "key_points": ["Official rule"],
        "sources": ["https://example.com/rules"], "game_or_sport": "UNO", "subject": "UNO rules",
        "claim": "A rule", "category": "rule_check", "angle": "rule",
    }
    rendered = render_rich_html(story)
    assert "RULE CHECK" in rendered and "@TheSportsNewsroom" in rendered and visible_length(rendered) < 32768

    historical = dict(story); historical.update({"format": "on_this_date", "date_anchor": "20 September 1926"})
    assert "20 September 1926" in render_rich_html(historical)

    old = default_state(); old["queue"]["x"] = {"first_seen_at": "2000-01-01T00:00:00+00:00"}; prune_state(old); assert "x" not in old["queue"]
    logger.info("Self-test passed for %s v%s", APP_NAME, APP_VERSION)


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"{APP_NAME} {APP_VERSION}"); return
    if args.self_test:
        self_test(); return
    from sportsgames.pipeline import run_once
    run_once()

if __name__ == "__main__": main()
