from __future__ import annotations

import logging
from datetime import date

from .discovery import is_video_game_contaminated
from .providers import Providers, source_tier
from .schemas import DAILY_SCHEMA, POST_SCHEMA
from .utils import complete_sentence, sentence_count, text
from .verification import deterministic_story_checks, verify_story_against_evidence

logger = logging.getLogger("sports-games-hub.content")


def format_for(candidate: dict) -> str:
    category = text(candidate.get("category"))
    mapping = {
        "game_discovery": "game_discovery",
        "new_board_game": "game_discovery",
        "new_card_game": "game_discovery",
        "new_tabletop_game": "game_discovery",
        "new_sport": "game_discovery",
        "rule_check": "rule_check",
        "how_to_play": "how_to_play",
        "on_this_date": "on_this_date",
        "century_ago": "century_ago",
        "game_history": "history",
        "sport_history": "history",
        "game_origin": "history",
        "sport_origin": "history",
        "why_explained": "why",
        "first_last_only": "first_last_only",
        "then_vs_now": "then_vs_now",
        "forgotten_game": "forgotten",
        "forgotten_sport": "forgotten",
        "interesting_number": "number",
        "myth_vs_fact": "fact",
    }
    return mapping.get(category, "fact")


def _evidence_text(candidate: dict) -> tuple[str, str]:
    packets = candidate.get("evidence_packets") or []
    if not packets:
        return "", ""
    blocks = []
    for packet in packets[:5]:
        blocks.append(
            f"SOURCE: {packet.get('url')}\n"
            f"TIER: {packet.get('tier')}\n"
            f"TEXT:\n{packet.get('text', '')[:9000]}"
        )
    image_url = next(
        (text(packet.get("image_url")) for packet in packets if text(packet.get("image_url"))),
        "",
    )
    return "\n\n".join(blocks), image_url


def _repair_story(
    providers: Providers,
    story: dict,
    candidate: dict,
    reason: str,
    lane: str,
) -> dict | None:
    evidence, _ = _evidence_text(candidate)
    system = """
You repair a Telegram post for The Sports Newsroom.
Fix ONLY the stated validation problem while preserving supported factual content.
Use only the supplied evidence. Never add facts from memory.
Keep the same format and write in original wording.
Remove unsupported specifics and disposable time language.
Return the complete JSON object using the supplied schema.
""".strip()
    user = (
        f"REPAIR REASON: {reason}\n"
        f"CURRENT POST:\n{story}\n\n"
        f"EVIDENCE:\n{evidence[:24000]}"
    )
    try:
        repaired = providers.ai(
            system=system,
            user=user,
            schema_name="sports_games_story_repair_v3",
            schema=POST_SCHEMA,
            max_tokens=2400,
            lane=lane,
        )
    except Exception as exc:
        logger.warning("Story repair failed: %s", exc)
        return None
    repaired.update({
        key: candidate.get(key, repaired.get(key, ""))
        for key in ("game_or_sport", "subject", "claim", "category", "angle")
    })
    repaired["candidate"] = candidate
    repaired["source_text"] = evidence
    return repaired


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
    return True, "ok"


def generate_story(
    providers: Providers,
    candidate: dict,
    lane: str = "discovery",
    max_repairs: int = 1,
) -> dict | None:
    source_text, image_url = _evidence_text(candidate)
    if not source_text:
        return None
    fmt = format_for(candidate)
    system = f"""
You are the senior editor for @TheSportsNewsroom.
Create one durable Telegram post in format: {fmt}.
The channel covers only real-world sports and physical games: board, card, tabletop, party, mind, recreational and traditional games.
Never cover video games, esports, consoles, Steam, DLC, patches or gaming hardware.
Use ONLY facts directly supported by the supplied evidence. Do not use memory.
Do not invent dates, statistics, rules, origins, records, superlatives or causal explanations.
Avoid disposable words such as today, yesterday, tomorrow, tonight and latest outside daily anchor formats.
Curiosity is welcome; clickbait is not.
Maximum body length is six sentences. Key points must be evidence-backed.
Rewrite in your own words. Return only JSON.
""".strip()
    user = (
        f"CATEGORY: {candidate.get('category')}\n"
        f"ANGLE: {candidate.get('angle')}\n"
        f"GAME/SPORT: {candidate.get('game_or_sport')}\n"
        f"SUBJECT: {candidate.get('subject')}\n"
        f"CLAIM/EVENT: {candidate.get('claim_or_event')}\n"
        f"WHY: {candidate.get('why_interesting')}\n"
        f"DATE ANCHOR: {candidate.get('date_anchor', '')}\n\n"
        f"EVIDENCE:\n{source_text[:30000]}"
    )
    try:
        story = providers.ai(
            system=system,
            user=user,
            schema_name="sports_games_story_v3",
            schema=POST_SCHEMA,
            max_tokens=4200,
            lane=lane,
        )
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

    for attempt in range(max_repairs + 1):
        ok, reason = validate_story(story)
        if not ok:
            logger.info("Story validation failed: %s", reason)
            if attempt >= max_repairs:
                return None
            repaired = _repair_story(providers, story, candidate, reason, lane)
            if not repaired:
                return None
            story = repaired
            continue
        numeric_ok, numeric_reason = deterministic_story_checks(story, source_text)
        if not numeric_ok:
            logger.info("Story deterministic grounding failed: %s", numeric_reason)
            if attempt >= max_repairs:
                return None
            repaired = _repair_story(providers, story, candidate, numeric_reason, lane)
            if not repaired:
                return None
            story = repaired
            continue
        break

    fields = " ".join([
        text(story.get("headline")),
        text(story.get("body")),
        text(story.get("why_interesting")),
    ])
    if is_video_game_contaminated(fields):
        return None
    if not verify_story_against_evidence(providers, story, candidate, lane=lane):
        return None
    return story


def build_daily_plan(
    providers: Providers,
    candidates: list[dict],
    mode: str,
    target: date,
    lane: str = "mandatory",
) -> dict | None:
    rows = []
    source_lookup = {}
    allowed_urls = {text(c.get("url")) for c in candidates if text(c.get("url"))}
    for idx, candidate in enumerate(candidates[:70], 1):
        rows.append(
            f"ID: {idx}\n"
            f"TITLE: {candidate.get('title')}\n"
            f"SOURCE: {candidate.get('source')}\n"
            f"URL: {candidate.get('url')}\n"
            f"EXCERPT: {candidate.get('excerpt', '')[:1600]}"
        )
        source_lookup[text(candidate.get("url"))] = candidate
    system = f"""
You are the sports calendar editor for @TheSportsNewsroom.
Build a factual reference plan for {mode} and exact target date {target.isoformat()}.
Only include real-world sports, not video games or esports.
NEXT: an event must actually be scheduled on the target date.
PAST: a result/event must actually belong to the target date.
The source publication date is NOT the event date. Use evidence in the supplied title/excerpt for date, time, result and event identity.
Never infer a fixture, result, venue, time or competition stage.
A missing field must be an empty string, never a guess.
Preserve breadth across sports. Return only JSON.
""".strip()
    try:
        result = providers.ai(
            system=system,
            user="\n\n".join(rows),
            schema_name="daily_sports_plan_v3",
            schema=DAILY_SCHEMA,
            max_tokens=3600,
            lane=lane,
        )
    except Exception as exc:
        logger.warning("Daily plan failed: %s", exc)
        return None

    clean = []
    used = set()
    for event in result.get("events", []):
        if text(event.get("date")) != target.isoformat():
            continue
        urls = [
            u for u in event.get("source_urls", [])
            if text(u) in allowed_urls and not is_video_game_contaminated(u)
        ]
        if not urls:
            continue
        key = f"{text(event.get('sport')).lower()}|{text(event.get('event')).lower()}|{target.isoformat()}"
        if key in used:
            continue
        used.add(key)
        evidence_packets = []
        for url in urls[:4]:
            candidate = source_lookup.get(url, {})
            if text(candidate.get("excerpt")):
                evidence_packets.append({
                    "url": url,
                    "tier": source_tier(url),
                    "text": text(candidate.get("excerpt"))[:5000],
                })
        clean.append({**event, "source_urls": urls[:4], "evidence_packets": evidence_packets})
    clean.sort(key=lambda x: int(x.get("importance", 0) or 0), reverse=True)
    if not clean:
        return None
    return {"mode": mode, "target_date": target.isoformat(), "events": clean[:50]}


def generate_daily_story(
    providers: Providers,
    plan: dict,
    lane: str = "mandatory",
    max_repairs: int = 1,
) -> dict | None:
    mode = plan["mode"]
    target = plan["target_date"]
    events = plan["events"]
    system = f"""
You are the lead editor for a permanent sports calendar archive on @TheSportsNewsroom.
Create the {mode} post for exact date {target}.
NEXT explains notable sporting events scheduled for that exact date.
PAST explains notable results/events that occurred on that exact date.
Use exact date and UTC time when supplied. Never use today, tomorrow, yesterday, tonight or latest.
Organize by sport. Start with the most notable events, then compactly list additional verified events.
Use ONLY the event records supplied. Do not invent facts.
Return only JSON.
""".strip()
    user = (
        f"MODE: {mode}\nDATE: {target}\nEVENTS:\n"
        + "\n".join(
            f"- {event.get('sport')} | {event.get('event')} | {event.get('competition')} | "
            f"{event.get('stage')} | {event.get('time_utc')} | {event.get('location')} | "
            f"{event.get('status')} | {event.get('source_urls')}"
            for event in events
        )
    )
    try:
        story = providers.ai(
            system=system,
            user=user,
            schema_name="daily_sports_story_v3",
            schema=POST_SCHEMA,
            max_tokens=3200,
            lane=lane,
        )
    except Exception as exc:
        logger.warning("Daily story generation failed: %s", exc)
        return None

    source_urls = list(dict.fromkeys(u for event in events for u in event.get("source_urls", [])))[:6]
    candidate = {
        "source_urls": source_urls,
        "excerpt": "",
        "evidence_packets": [packet for event in events for packet in event.get("evidence_packets", [])][:8],
        "game_or_sport": "Sports",
        "subject": target,
        "claim_or_event": f"Sports calendar for {target}",
    }
    story.update({
        "format": "daily_next" if mode == "NEXT" else "daily_past",
        "game_or_sport": "Sports",
        "subject": target,
        "claim": f"Sports calendar for {target}",
        "category": "sports_daily_next" if mode == "NEXT" else "sports_daily_past",
        "angle": "calendar",
        "date_anchor": target,
        "sources": source_urls,
        "key_points": [f"{event.get('sport')}: {event.get('event')}" for event in events[:6]],
        "why_interesting": "A dated reference to notable sporting events.",
        "candidate": candidate,
        "source_text": "\n".join(packet.get("text", "") for packet in candidate["evidence_packets"]),
    })

    ok, reason = validate_story(story)
    if not ok:
        if not max_repairs:
            return None
        repaired = _repair_story(providers, story, candidate, reason, lane)
        if not repaired:
            return None
        story = repaired
        story.update({
            "format": "daily_next" if mode == "NEXT" else "daily_past",
            "category": "sports_daily_next" if mode == "NEXT" else "sports_daily_past",
            "angle": "calendar",
            "date_anchor": target,
            "sources": source_urls,
            "candidate": candidate,
            "game_or_sport": "Sports",
            "subject": target,
            "claim": f"Sports calendar for {target}",
        })
    for attempt in range(1 + max_repairs):
        numeric_ok, numeric_reason = deterministic_story_checks(story, story.get("source_text", ""))
        if numeric_ok:
            break
        logger.info("Daily story grounding failed: %s", numeric_reason)
        if attempt >= max_repairs:
            return None
        repaired = _repair_story(providers, story, candidate, numeric_reason, lane)
        if not repaired:
            return None
        story = repaired
        story.update({
            "format": "daily_next" if mode == "NEXT" else "daily_past",
            "category": "sports_daily_next" if mode == "NEXT" else "sports_daily_past",
            "angle": "calendar",
            "date_anchor": target,
            "sources": source_urls,
            "candidate": candidate,
            "game_or_sport": "Sports",
            "subject": target,
            "claim": f"Sports calendar for {target}",
            "source_text": "\n".join(packet.get("text", "") for packet in candidate["evidence_packets"]),
        })
    else:
        return None
    if is_video_game_contaminated(" ".join([text(story.get("headline")), text(story.get("body"))])):
        return None
    return story
