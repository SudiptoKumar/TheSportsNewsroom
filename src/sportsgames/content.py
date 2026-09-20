from __future__ import annotations

import logging
from datetime import date

from .config import MAX_REPAIR_ATTEMPTS
from .discovery import is_video_game_contaminated
from .providers import Providers
from .schemas import DAILY_SCHEMA, POST_SCHEMA, REPAIR_SCHEMA
from .utils import clamp, complete_sentence, sentence_count, text
from .verification import build_evidence_packet, deterministic_story_checks, verify_story_against_evidence

logger = logging.getLogger("sports-games-hub.content")


FORMAT_MAP = {
    "game_discovery": "game_discovery", "new_board_game": "game_discovery", "new_card_game": "game_discovery",
    "new_tabletop_game": "game_discovery", "new_sport": "game_discovery", "rule_check": "rule_check",
    "how_to_play": "how_to_play", "on_this_date": "on_this_date", "century_ago": "century_ago",
    "game_history": "history", "sport_history": "history", "game_origin": "history", "sport_origin": "history",
    "why_explained": "why", "first_last_only": "first_last_only", "then_vs_now": "then_vs_now",
    "forgotten_game": "forgotten", "forgotten_sport": "forgotten", "interesting_number": "number",
    "myth_vs_fact": "fact",
}

FORMAT_LIMITS = {
    "fact": (2, 700), "game_discovery": (3, 1200), "rule_check": (3, 1000), "how_to_play": (4, 1200),
    "history": (4, 1000), "on_this_date": (4, 1000), "century_ago": (4, 1000), "why": (4, 1000),
    "first_last_only": (4, 1000), "then_vs_now": (5, 1200), "forgotten": (4, 1000), "number": (4, 900),
}


def format_for(candidate: dict) -> str:
    return FORMAT_MAP.get(text(candidate.get("category")), "fact")


def validate_story(story: dict) -> tuple[bool, str]:
    for field in ("headline", "dek", "body", "why_interesting"):
        if not text(story.get(field)):
            return False, f"missing_{field}"
        if field != "headline" and not complete_sentence(story[field]):
            return False, f"incomplete_{field}"
    fmt = text(story.get("format"))
    max_sentences, max_chars = FORMAT_LIMITS.get(fmt, (6, 1200))
    if sentence_count(story.get("body")) > max_sentences:
        return False, "body_too_long"
    if len(text(story.get("body"))) > max_chars:
        return False, "body_over_char_limit"
    if len(story.get("key_points", [])) > 6:
        return False, "too_many_key_points"
    if any("..." in text(x) or "…" in text(x) for x in [story.get("headline"), story.get("dek"), story.get("body"), story.get("why_interesting")]):
        return False, "ellipsis_in_content"
    return True, "ok"


def _repair_story(providers: Providers, story: dict, candidate: dict, issues: list[str], evidence_text: str) -> dict | None:
    current = dict(story)
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        system = """
You are the repair editor for The Sports Newsroom.
Repair the supplied draft using ONLY the evidence packet.
Preserve the central factual claim, remove unsupported details, fix incomplete sentences, and fit the format limits.
Do not add any new factual information from memory.
For historical/current content use exact dates instead of disposable relative words.
Return only JSON matching the supplied schema.
""".strip()
        user = (
            f"FORMAT: {current.get('format')}\nFAILURES: {', '.join(issues)}\n"
            f"DRAFT: {current}\n\nEVIDENCE:\n{evidence_text[:26000]}"
        )
        try:
            repaired = providers.ai(system=system, user=user, schema_name=f"story_repair_{attempt}", schema=REPAIR_SCHEMA, max_tokens=2200)
        except Exception as exc:
            logger.warning("Story repair %d failed: %s", attempt, exc)
            return None
        repaired.update({
            "candidate": candidate,
            "source_text": evidence_text,
            "game_or_sport": candidate.get("game_or_sport", ""),
            "subject": candidate.get("subject", ""),
            "claim": candidate.get("claim_or_event", ""),
            "category": candidate.get("category", ""),
            "angle": candidate.get("angle", ""),
        })
        ok, reason = validate_story(repaired)
        if not ok:
            issues = [reason]
            current = repaired
            continue
        numeric_ok, numeric_reason = deterministic_story_checks(repaired, evidence_text)
        if not numeric_ok:
            issues = [numeric_reason]
            current = repaired
            continue
        if is_video_game_contaminated(" ".join([text(repaired.get("headline")), text(repaired.get("body")), text(repaired.get("why_interesting"))])):
            issues = ["video_game_contamination"]
            current = repaired
            continue
        return repaired
    return None


def generate_story(providers: Providers, candidate: dict) -> dict | None:
    evidence_text = text(candidate.get("evidence_text"))
    evidence_records = candidate.get("evidence_records") or []
    if not evidence_text:
        evidence_text, evidence_records = build_evidence_packet(candidate, max_sources=4)
        candidate["evidence_text"] = evidence_text
        candidate["evidence_records"] = evidence_records
    if not evidence_text:
        return None

    fmt = format_for(candidate)
    max_sentences, max_chars = FORMAT_LIMITS.get(fmt, (6, 1200))
    system = f"""
You are the senior editor for @TheSportsNewsroom.
Create one durable Telegram post in format: {fmt}.
This channel covers real-world sports and physical games only: board, card, tabletop, party, mind and traditional games.
Never cover video games, esports, consoles, Steam, DLC, patches or gaming hardware.
Use ONLY facts directly supported by the evidence packet. Never rely on memory.
Do not manufacture dates, statistics, rules, origins, records, names or superlatives.
For a current/historical event, use exact date(s), not today, yesterday, tomorrow, tonight or latest.
Curiosity is welcome, clickbait is not.
Body limit: {max_sentences} sentences and {max_chars} characters.
Key points must be evidence-backed.
Rewrite in original wording; never copy source sentences.
Return only JSON.
""".strip()
    user = (
        f"CATEGORY: {candidate.get('category')}\nANGLE: {candidate.get('angle')}\n"
        f"GAME/SPORT: {candidate.get('game_or_sport')}\nSUBJECT: {candidate.get('subject')}\n"
        f"CLAIM/EVENT: {candidate.get('claim_or_event')}\nWHY INTERESTING: {candidate.get('why_interesting')}\n"
        f"DATE ANCHOR: {candidate.get('date_anchor','')}\n\nEVIDENCE PACKET:\n{evidence_text[:28000]}"
    )
    try:
        story = providers.ai(system=system, user=user, schema_name="sports_games_story_v3", schema=POST_SCHEMA, max_tokens=2400)
    except Exception as exc:
        logger.warning("Story generation failed: %s", exc)
        return None
    story.update({
        "candidate": candidate,
        "source_text": evidence_text,
        "game_or_sport": candidate.get("game_or_sport", ""),
        "subject": candidate.get("subject", ""),
        "claim": candidate.get("claim_or_event", ""),
        "category": candidate.get("category", ""),
        "angle": candidate.get("angle", ""),
    })

    ok, reason = validate_story(story)
    if not ok:
        repaired = _repair_story(providers, story, candidate, [reason], evidence_text)
        if repaired is None:
            logger.info("Story rejected after repair: %s", reason)
            return None
        story = repaired

    numeric_ok, numeric_reason = deterministic_story_checks(story, evidence_text)
    if not numeric_ok:
        repaired = _repair_story(providers, story, candidate, [numeric_reason], evidence_text)
        if repaired is None:
            logger.info("Story rejected by deterministic grounding: %s", numeric_reason)
            return None
        story = repaired

    if is_video_game_contaminated(" ".join([text(story.get("headline")), text(story.get("body")), text(story.get("why_interesting"))])):
        return None

    fact_ok, factcheck = verify_story_against_evidence(providers, story, candidate)
    if not fact_ok:
        unsupported = factcheck.get("unsupported_statements") or [factcheck.get("reason", "unsupported content")]
        repaired = _repair_story(providers, story, candidate, list(map(text, unsupported))[:5], evidence_text)
        if repaired is None:
            logger.info("Story failed final evidence grounding: %s", story.get("headline"))
            return None
        story = repaired
        fact_ok, _ = verify_story_against_evidence(providers, story, candidate)
        if not fact_ok:
            logger.info("Story failed evidence grounding after repair: %s", story.get("headline"))
            return None
    return story


def build_daily_plan(providers: Providers, candidates: list[dict], mode: str, target: date) -> dict | None:
    rows = []
    for idx, c in enumerate(candidates[:70], 1):
        rows.append(
            f"ID: {idx}\nTITLE: {c.get('title')}\nSOURCE: {c.get('source')}\nURL: {c.get('url')}\n"
            f"SOURCE TIER: {c.get('source_tier', 5)}\nEXCERPT: {c.get('excerpt','')[:1600]}"
        )
    if not rows:
        return None
    system = f"""
You are the sports calendar editor for @TheSportsNewsroom.
Build a factual event plan for {mode} and exact target date {target.isoformat()}.
Use only event information supported by the supplied candidate records.
For NEXT, the event must actually be scheduled on the target date.
For PAST, the event/result must actually belong to the target date.
Choose events by reading the source title/excerpt. Do not invent event data.
If the exact time, venue, stage or status is not supported, return an empty string.
Every event must cite one or more source_ids from the supplied records.
Prefer breadth across sports and include a compact set of notable events rather than padding.
Return only JSON.
""".strip()
    try:
        result = providers.ai(system=system, user="\n\n".join(rows), schema_name="daily_sports_plan_v3", schema=DAILY_SCHEMA, max_tokens=3000)
    except Exception as exc:
        logger.warning("Daily plan failed: %s", exc)
        return None
    by_id = {i + 1: c for i, c in enumerate(candidates[:70])}
    clean = []
    target_iso = target.isoformat()
    target_display = target.strftime("%d %B %Y").lower()
    for event in result.get("events", []):
        ids = [int(x) for x in event.get("source_ids", []) if int(x) in by_id]
        if not ids or text(event.get("date")) != target_iso:
            continue
        source_text = " ".join(f"{by_id[i].get('title','')} {by_id[i].get('excerpt','')}" for i in ids).lower()
        # Event dates are search-constrained, but still require some recognizable date signal when present.
        date_signals = [target_display, target.strftime("%b %d, %Y").lower(), target.strftime("%B %d, %Y").lower(), target_iso]
        if not any(sig in source_text for sig in date_signals):
            # Relative phrasing is allowed only for current schedule searches, because the query itself is date-specific.
            if mode == "PAST":
                continue
        # Only retain fields that have at least some source-text support.
        event_name = text(event.get("event"))
        source_joined = normalize_event(event_name)
        if source_joined and not any(tok in source_text for tok in source_joined.split() if len(tok) >= 4):
            continue
        event_clean = {**event, "source_ids": ids}
        # Clear details that do not appear to be evidenced.
        for field in ("time_utc", "competition", "stage", "location"):
            value = text(event_clean.get(field))
            if value and normalize_event(value) not in source_text:
                # Keep some useful values when individual tokens occur.
                tokens = [t for t in normalize_event(value).split() if len(t) >= 4]
                if tokens and not all(tok in source_text for tok in tokens[:2]):
                    event_clean[field] = ""
        clean.append(event_clean)
    if not clean:
        return None
    # Deduplicate by sport+event and retain highest importance.
    unique = {}
    for e in clean:
        key = (normalize_event(e.get("sport")), normalize_event(e.get("event")))
        if key not in unique or int(e.get("importance", 0) or 0) > int(unique[key].get("importance", 0) or 0):
            unique[key] = e
    events = sorted(unique.values(), key=lambda x: int(x.get("importance", 0) or 0), reverse=True)
    return {"mode": mode, "target_date": target_iso, "events": events[:50]}


def normalize_event(value: object) -> str:
    import re
    return re.sub(r"[^a-z0-9\s]", " ", text(value).lower())


def build_daily_story(plan: dict) -> dict:
    mode = plan["mode"]
    target = plan["target_date"]
    events = plan.get("events", [])
    is_next = mode == "NEXT"
    headline = f"Sports scheduled for {target}" if is_next else f"Notable sports events on {target}"
    dek = (
        "A dated guide to notable sporting events scheduled for this date, with exact times shown only when supported."
        if is_next else
        "A dated reference to notable sporting results and events from this calendar date."
    )
    return {
        "format": "daily_next" if is_next else "daily_past",
        "headline": headline,
        "dek": dek,
        "body": "Events are grouped by sport and sorted by editorial importance. Details that could not be supported by the source records are omitted.",
        "why_interesting": "A date-anchored sports reference remains understandable after the event itself has passed.",
        "key_points": [f"{e.get('sport')}: {e.get('event')}" for e in events[:6]],
        "date_anchor": target,
        "sources": list(dict.fromkeys(
            text(next((str(src),) for src in []), "") for _ in []
        )),
        "tags": ["sports", "calendar"],
        "events": events,
    }
