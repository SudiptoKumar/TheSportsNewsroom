from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from .config import (
    APP_NAME, APP_VERSION, DAY_IN_SPORTS_AFTER_HOUR, MAX_CANDIDATES, NEXT_UP_AFTER_HOUR,
    POST_DELAY_SECONDS, TELEGRAM_CHANNEL,
)
from .content import build_daily_plan, build_daily_story, generate_story
from .discovery import discover_evergreen, discover_historical_date, discover_next_sports, discover_past_sports, is_video_game_contaminated
from .editorial import classify_candidates, preliminary_score, select_candidates
from .media import create_visual
from .providers import Providers, fetch_rss
from .state import (
    CONFIG, claim_key, daily_done, entity_key, load_state, mark_daily, prune_state, remember_published_url, save_state,
)
from .telegram import fit_rich_html, plain_caption, send_story
from .taxonomy import RSS_FEEDS
from .utils import iso, normalize_text, text
from .verification import build_evidence_packet, is_claim_duplicate, verify_candidate

logger = logging.getLogger("sports-games-hub.pipeline")


def _record_post(state: dict, story: dict, message_id: object = None) -> None:
    subject = text(story.get("subject"))
    claim = text(story.get("claim"))
    angle = text(story.get("angle"))
    key = claim_key(subject, claim, angle)
    state.setdefault("claims", {})[key] = {
        "subject": subject, "claim": claim, "angle": angle, "category": text(story.get("category")),
        "published_at": iso(datetime.now(CONFIG.tz)), "sources": list(story.get("sources", []))[:6],
    }
    entity = entity_key(text(story.get("game_or_sport")))
    if entity:
        info = state.setdefault("entities", {}).setdefault(entity, {
            "name": text(story.get("game_or_sport")), "post_count": 0, "categories": {}, "angles": {}, "last_posted_at": "",
        })
        info["post_count"] += 1
        category = text(story.get("category")); angle = text(story.get("angle"))
        info["categories"][category] = info["categories"].get(category, 0) + 1
        info["angles"][angle] = info["angles"].get(angle, 0) + 1
        info["last_posted_at"] = iso(datetime.now(CONFIG.tz))
    state.setdefault("posts", []).append({
        "published_at": iso(datetime.now(CONFIG.tz)), "message_id": message_id, "format": text(story.get("format")),
        "category": text(story.get("category")), "angle": angle, "game_or_sport": text(story.get("game_or_sport")),
        "subject": subject, "claim": claim, "headline": text(story.get("headline")),
        "date_anchor": text(story.get("date_anchor")), "source_urls": list(story.get("sources", []))[:6],
        "canonical_url": text((story.get("candidate") or {}).get("canonical") or (story.get("candidate") or {}).get("url")),
    })
    state.setdefault("angle_history", []).append({"angle": angle, "at": iso(datetime.now(CONFIG.tz))})
    state.setdefault("category_history", []).append({"category": text(story.get("category")), "at": iso(datetime.now(CONFIG.tz))})
    state.setdefault("subject_history", []).append({"subject": subject, "at": iso(datetime.now(CONFIG.tz))})


def _publish(state: dict, story: dict, index: int) -> bool:
    rich = fit_rich_html(story)
    image = create_visual(story, index)
    result = send_story(image, rich)
    try:
        os.remove(image)
    except OSError:
        pass
    if not result.get("ok"):
        logger.error("Telegram publish failed: %s", result.get("description"))
        return False
    message = result.get("result", {})
    message_id = message.get("message_id") if isinstance(message, dict) else None
    candidate = story.get("candidate", {}) or {}
    if candidate.get("url"):
        remember_published_url(candidate["url"])
    _record_post(state, story, message_id)
    return True


def _daily_story(providers: Providers, state: dict, kind: str, target) -> dict | None:
    flag_kind = "next" if kind == "next" else "past"
    if daily_done(state, flag_kind, target.isoformat()):
        return None
    candidates = discover_next_sports(providers, target) if kind == "next" else discover_past_sports(providers, target)
    if not candidates:
        return None
    plan = build_daily_plan(providers, candidates, "NEXT" if kind == "next" else "PAST", target)
    if not plan:
        return None
    story = build_daily_story(plan)
    source_urls = []
    source_records = []
    for event in plan.get("events", []):
        for source_id in event.get("source_ids", []):
            if 1 <= int(source_id) <= len(candidates):
                c = candidates[int(source_id) - 1]
                if c.get("url"):
                    source_urls.append(c["url"])
                    source_records.append(c)
    story["sources"] = list(dict.fromkeys(source_urls))[:8]
    story["candidate"] = {"source_urls": story["sources"], "source_records": source_records, "excerpt": ""}
    story["source_text"] = "\n".join(text(x.get("excerpt")) for x in source_records)
    story["game_or_sport"] = "Sports"
    story["subject"] = plan["target_date"]
    story["claim"] = f"Sports calendar for {plan['target_date']}"
    story["category"] = "sports_daily_next" if kind == "next" else "sports_daily_past"
    story["angle"] = "calendar"
    return story


def _evergreen_candidates(providers: Providers, state: dict) -> list[dict]:
    now = datetime.now(CONFIG.tz)
    day_index = now.timetuple().tm_yday + now.year * 13
    raw = discover_evergreen(providers, day_index)
    raw += discover_historical_date(providers, now.date())[:20]
    # RSS improves current sports discovery without being mandatory.
    raw += fetch_rss(RSS_FEEDS, limit_per_feed=15)[:60]
    if not raw:
        return []

    filtered = []
    for item in raw[:MAX_CANDIDATES]:
        combined = " ".join(map(text, [item.get("title"), item.get("excerpt"), item.get("url")]))
        if not text(item.get("excerpt")):
            continue
        if is_video_game_contaminated(combined):
            continue
        if any(text(item.get("url")) == text(p.get("canonical_url")) for p in state.get("posts", [])[-200:]):
            continue
        filtered.append(item)
    if not filtered:
        return []

    classified = classify_candidates(providers, filtered, mode="evergreen + discovery + history")
    # Cheap ranking first. Only the strongest candidates consume verification AI calls.
    classified.sort(key=lambda c: preliminary_score(c, state), reverse=True)
    verify_pool = classified[:10]
    verified = []
    for candidate in verify_pool:
        if is_claim_duplicate(state, candidate):
            continue
        category = text(candidate.get("category"))
        if category in {"on_this_date", "century_ago"} and not text(candidate.get("date_anchor")):
            continue
        ok, verification = verify_candidate(providers, candidate)
        candidate["verification"] = verification
        if not ok:
            continue
        evidence_text, evidence_records = build_evidence_packet(candidate, max_sources=4)
        if not evidence_text:
            continue
        candidate["evidence_text"] = evidence_text
        candidate["evidence_records"] = evidence_records
        verified.append(candidate)
    return verified


def run_once() -> int:
    providers = Providers.from_env(require=True)
    state = load_state()
    prune_state(state)
    state["last_run_at"] = iso(datetime.now(CONFIG.tz))
    logger.info("%s v%s | channel=%s", APP_NAME, APP_VERSION, TELEGRAM_CHANNEL)

    current = datetime.now(CONFIG.tz)
    stories: list[dict] = []

    if current.hour >= NEXT_UP_AFTER_HOUR:
        target = current.date() + timedelta(days=1)
        story = _daily_story(providers, state, "next", target)
        if story:
            stories.append(story)

    if current.hour >= DAY_IN_SPORTS_AFTER_HOUR:
        target = current.date() - timedelta(days=1)
        story = _daily_story(providers, state, "past", target)
        if story:
            stories.append(story)

    # Discovery is independent and capped separately.
    from .state import discovery_posts_today
    if discovery_posts_today(state) < int(os.getenv("MAX_DISCOVERY_POSTS_PER_DAY", "4")):
        try:
            raw_candidates = _evergreen_candidates(providers, state)
        except Exception as exc:
            logger.warning("Evergreen pipeline failed: %s", exc)
            raw_candidates = []
        selected = select_candidates(raw_candidates, state)
        for candidate in selected:
            story = generate_story(providers, candidate)
            if story:
                stories.append(story)

    published = 0
    for index, story in enumerate(stories, start=1):
        if _publish(state, story, index):
            published += 1
            fmt = text(story.get("format"))
            if fmt == "daily_next":
                mark_daily(state, "next", text(story.get("date_anchor")))
            elif fmt == "daily_past":
                mark_daily(state, "past", text(story.get("date_anchor")))
            save_state(state)
            time.sleep(POST_DELAY_SECONDS)

    save_state(state)
    logger.info("Run complete. Published=%d | AI calls=%d", published, providers.ai_calls)
    return published
