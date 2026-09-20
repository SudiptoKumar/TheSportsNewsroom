from __future__ import annotations

import html
import logging
import re
from datetime import date

from .config import MAX_CAPTION_CHARACTERS, MAX_RICH_CHARACTERS
from .discovery import is_video_game_contaminated
from .providers import Providers, fetch_article
from .schemas import DAILY_SCHEMA, POST_SCHEMA
from .utils import clamp, complete_sentence, sentence_count, text
from .verification import deterministic_story_checks, verify_story_against_evidence

logger = logging.getLogger("sports-games-hub.content")


def format_for(candidate: dict) -> str:
    category = text(candidate.get("category"))
    mapping = {
        "game_discovery": "game_discovery", "new_board_game": "game_discovery", "new_card_game": "game_discovery",
        "new_tabletop_game": "game_discovery", "new_sport": "game_discovery", "rule_check": "rule_check",
        "how_to_play": "how_to_play", "on_this_date": "on_this_date", "century_ago": "century_ago",
        "game_history": "history", "sport_history": "history", "game_origin": "history", "sport_origin": "history",
        "why_explained": "why", "first_last_only": "first_last_only", "then_vs_now": "then_vs_now",
        "forgotten_game": "forgotten", "forgotten_sport": "forgotten", "interesting_number": "number",
        "myth_vs_fact": "fact",
    }
    return mapping.get(category, "fact")


def generate_story(providers: Providers, candidate: dict) -> dict | None:
    source_urls = list(dict.fromkeys(text(u) for u in candidate.get("source_urls", []) if text(u)))
    evidence = []
    image_url = ""
    for url in source_urls[:5]:
        article = fetch_article(url, fallback_excerpt=candidate.get("excerpt", ""))
        if article.get("text"):
            evidence.append(f"SOURCE: {url}\nTEXT:\n{article['text'][:9000]}")
        if not image_url and article.get("image_url"):
            image_url = article["image_url"]
    source_text = "\n\n".join(evidence)
    if not source_text:
        return None

    fmt = format_for(candidate)
    system = f"""
You are the senior editor for @TheSportsNewsroom.
Create one durable Telegram post in format: {fmt}.
The channel covers real-world sports and physical games only: board, card, tabletop, party, mind and traditional games.
Never cover video games, esports, consoles, Steam, DLC, patches or gaming hardware.
Use ONLY facts that are directly supported by the supplied evidence.
Do not add details from memory. Do not manufacture dates, statistics, rules, origins, records or superlatives.
Keep historical/current event wording date-specific. Avoid disposable words such as today, yesterday, tomorrow, tonight and latest unless they are part of an exact quoted title (do not quote long text).
Curiosity is welcome, clickbait is not.
Maximum body length is six sentences. Key points must be evidence-backed.
Rewrite in original wording; never copy source sentences.
If evidence shows a genuine dispute, explain the dispute instead of choosing a side without evidence.
Return only JSON.
""".strip()
    user = (
        f"CATEGORY: {candidate.get('category')}\nANGLE: {candidate.get('angle')}\n"
        f"GAME/SPORT: {candidate.get('game_or_sport')}\nSUBJECT: {candidate.get('subject')}\n"
        f"CLAIM/EVENT: {candidate.get('claim_or_event')}\nWHY INTERESTING: {candidate.get('why_interesting')}\n"
        f"DATE ANCHOR: {candidate.get('date_anchor','')}\n\nEVIDENCE:\n{source_text[:30000]}"
    )
    try:
        story = providers.ai(system=system, user=user, schema_name="sports_games_story_v2", schema=POST_SCHEMA, max_tokens=2500)
    except Exception as exc:
        logger.warning("Story generation failed: %s", exc)
        return None
    story.update({
        "candidate": candidate,
        "source_text": source_text,
        "image_url": image_url,
        "game_or_sport": candidate.get("game_or_sport", ""),
        "subject": candidate.get("subject", ""),
        "claim": candidate.get("claim_or_event", ""),
        "category": candidate.get("category", ""),
        "angle": candidate.get("angle", ""),
    })
    ok, reason = validate_story(story)
    if not ok:
        logger.info("Story rejected: %s", reason)
        return None
    numeric_ok, numeric_reason = deterministic_story_checks(story, source_text)
    if not numeric_ok:
        logger.info("Story rejected by deterministic grounding: %s", numeric_reason)
        return None
    if is_video_game_contaminated(" ".join([text(story.get("headline")), text(story.get("body")), text(story.get("why_interesting"))])):
        return None
    if not verify_story_against_evidence(providers, story, candidate):
        logger.info("Story failed final evidence grounding: %s", story.get("headline"))
        return None
    return story


def validate_story(story: dict) -> tuple[bool, str]:
    for field in ("headline", "dek", "body", "why_interesting"):
        if not text(story.get(field)):
            return False, f"missing_{field}"
        if not complete_sentence(story[field]) and field != "headline":
            return False, f"incomplete_{field}"
    if sentence_count(story["body"]) > 6:
        return False, "body_too_long"
    if len(story.get("key_points", [])) > 6:
        return False, "too_many_key_points"
    if any("..." in text(x) or "…" in text(x) for x in [story.get("headline"), story.get("dek"), story.get("body"), story.get("why_interesting")]):
        return False, "ellipsis_in_content"
    return True, "ok"


def build_daily_plan(providers: Providers, candidates: list[dict], mode: str, target: date) -> dict | None:
    rows = []
    for idx, c in enumerate(candidates[:70], 1):
        rows.append(
            f"ID: {idx}\nTITLE: {c.get('title')}\nSOURCE: {c.get('source')}\nURL: {c.get('url')}\nEXCERPT: {c.get('excerpt','')[:1400]}"
        )
    system = f"""
You are the sports calendar editor for @TheSportsNewsroom.
Build a factual reference plan for {mode} and exact target date {target.isoformat()}.
Only include real-world sports, not video games or esports.
NEXT: event must actually be scheduled on the target date.
PAST: event/result must actually belong to the target date.
Never infer a fixture, result, venue, time or competition stage if the sources do not support it.
A missing field must be an empty string, not a guess.
Prioritize significance but preserve breadth across sports.
Return only JSON.
""".strip()
    try:
        result = providers.ai(system=system, user="\n\n".join(rows), schema_name="daily_sports_plan_v2", schema=DAILY_SCHEMA, max_tokens=3600)
    except Exception as exc:
        logger.warning("Daily plan failed: %s", exc)
        return None
    clean = []
    for event in result.get("events", []):
        if text(event.get("date")) != target.isoformat():
            continue
        urls = [u for u in event.get("source_urls", []) if text(u) and not is_video_game_contaminated(u)]
        if not urls:
            continue
        clean.append({**event, "source_urls": list(dict.fromkeys(urls))[:5]})
    if not clean:
        return None
    clean.sort(key=lambda x: int(x.get("importance", 0) or 0), reverse=True)
    return {"mode": mode, "target_date": target.isoformat(), "events": clean}


def generate_daily_story(providers: Providers, plan: dict) -> dict | None:
    mode = plan["mode"]
    target = plan["target_date"]
    events = plan["events"]
    system = f"""
You are the lead editor for a permanent sports calendar archive on @TheSportsNewsroom.
Create the {mode} post for exact date {target}.
For NEXT, explain the upcoming notable sporting events scheduled for that date.
For PAST, explain the notable results/events that occurred on that date.
Use exact date and UTC times when supplied. Never use today, tomorrow, yesterday, tonight or latest.
Organize by sport. Start with the most notable events, then compactly list additional verified events.
Do not invent facts. All details must come only from the event records supplied.
Keep this useful as a historical reference years later.
Return only JSON.
""".strip()
    user = f"MODE: {mode}\nDATE: {target}\nEVENTS:\n" + "\n".join(
        f"- {e.get('sport')} | {e.get('event')} | {e.get('competition')} | {e.get('stage')} | {e.get('time_utc')} | {e.get('location')} | {e.get('status')} | {e.get('source_urls')} | {e.get('reason')}"
        for e in events
    )
    try:
        story = providers.ai(system=system, user=user, schema_name="daily_sports_story_v2", schema=POST_SCHEMA, max_tokens=3200)
    except Exception as exc:
        logger.warning("Daily story generation failed: %s", exc)
        return None
    story.update({
        "format": "daily_next" if mode == "NEXT" else "daily_past",
        "game_or_sport": "Sports", "subject": target, "claim": f"Sports calendar for {target}",
        "category": "sports_daily_next" if mode == "NEXT" else "sports_daily_past", "angle": "calendar",
        "date_anchor": target, "sources": list(dict.fromkeys(u for e in events for u in e.get("source_urls", [])))[:6],
        "key_points": [f"{e.get('sport')}: {e.get('event')}" for e in events[:6]],
        "why_interesting": "A dated reference to notable sporting events.",
        "source_text": "\n".join(e.get("reason", "") for e in events),
        "candidate": {"source_urls": story.get("sources", []), "excerpt": ""},
    })
    ok, reason = validate_story(story)
    if not ok:
        logger.info("Daily story rejected: %s", reason)
        return None
    if is_video_game_contaminated(" ".join([text(story.get("headline")), text(story.get("body"))])):
        return None
    return story
