from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .config import (
    APP_NAME, APP_VERSION, DAY_IN_SPORTS_AFTER_HOUR, NEXT_UP_AFTER_HOUR,
    POST_DELAY_SECONDS, MAX_CANDIDATES, MAX_DISCOVERY_POSTS_PER_DAY, TELEGRAM_CHANNEL,
)
from .content import build_daily_plan, generate_daily_story, generate_story
from .discovery import (
    discover_evergreen, discover_historical_date, discover_next_sports, discover_past_sports,
)
from .editorial import classify_candidates, select_candidates
from .media import create_visual
from .providers import Providers, fetch_rss
from .state import (
    CONFIG, claim_key, daily_done, entity_key, event_key, load_state, mark_daily,
    prune_state, remember_published_url, save_state,
)
from .telegram import fit_rich_html, send_photo_fallback, send_story
from .verification import is_claim_duplicate, verify_candidate
from .taxonomy import RSS_FEEDS
from .utils import iso, normalize_text, text

logger = logging.getLogger("sports-games-hub.pipeline")


def _record_post(state: dict, story: dict, message_id: object = None) -> None:
    candidate = story.get("candidate", {}) or {}
    subject = text(story.get("subject"))
    claim = text(story.get("claim"))
    angle = text(story.get("angle"))
    key = claim_key(subject, claim, angle)
    state.setdefault("claims", {})[key] = {
        "subject": subject,
        "claim": claim,
        "angle": angle,
        "category": text(story.get("category")),
        "published_at": iso(datetime.now(CONFIG.tz)),
        "sources": list(story.get("sources", []))[:6],
    }
    entity = entity_key(text(story.get("game_or_sport")))
    if entity:
        info = state.setdefault("entities", {}).setdefault(entity, {
            "name": text(story.get("game_or_sport")),
            "post_count": 0,
            "categories": {},
            "angles": {},
            "last_posted_at": "",
        })
        info["post_count"] += 1
        category = text(story.get("category")); angle = text(story.get("angle"))
        info["categories"][category] = info["categories"].get(category, 0) + 1
        info["angles"][angle] = info["angles"].get(angle, 0) + 1
        info["last_posted_at"] = iso(datetime.now(CONFIG.tz))

    state.setdefault("posts", []).append({
        "published_at": iso(datetime.now(CONFIG.tz)),
        "message_id": message_id,
        "format": text(story.get("format")),
        "category": text(story.get("category")),
        "angle": angle,
        "game_or_sport": text(story.get("game_or_sport")),
        "subject": subject,
        "claim": claim,
        "headline": text(story.get("headline")),
        "date_anchor": text(story.get("date_anchor")),
        "source_urls": list(story.get("sources", []))[:6],
        "canonical_url": text(candidate.get("canonical") or candidate.get("url")),
    })
    state.setdefault("angle_history", []).append({"angle": angle, "at": iso(datetime.now(CONFIG.tz))})
    state.setdefault("category_history", []).append({"category": text(story.get("category")), "at": iso(datetime.now(CONFIG.tz))})
    state.setdefault("subject_history", []).append({"subject": subject, "at": iso(datetime.now(CONFIG.tz))})


def _publish(state: dict, story: dict, index: int) -> bool:
    rich = fit_rich_html(story)
    image = create_visual(story, index)
    result = send_story(image, rich)
    if not result.get("ok"):
        logger.warning("sendRichMessage failed, trying sendPhoto fallback: %s", result.get("description"))
        result = send_photo_fallback(image, rich)
    try:
        import os
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
    story = generate_daily_story(providers, plan)
    if not story:
        return None
    # Final evidence grounding. This may fetch multiple official/news sources.
    from .verification import verify_story_against_evidence
    candidate = story.get("candidate", {})
    if not verify_story_against_evidence(providers, story, candidate):
        logger.warning("Daily story failed final evidence grounding: %s", story.get("headline"))
        return None
    return story


def _evergreen_candidates(providers: Providers, state: dict) -> list[dict]:
    day_index = datetime.now(CONFIG.tz).timetuple().tm_yday + datetime.now(CONFIG.tz).year * 13
    raw = discover_evergreen(providers, day_index)
    # Historical date discovery is separately included because its search pattern is
    # intentionally date-centric and should not be starved by general discovery.
    raw += discover_historical_date(providers, datetime.now(CONFIG.tz).date())[:20]
    if not raw:
        return []
    raw = raw[:MAX_CANDIDATES]
    classified = classify_candidates(providers, raw, mode="evergreen + discovery + history")
    verified = []
    for candidate in classified:
        if is_claim_duplicate(state, candidate):
            continue
        # Historical posts need a concrete date anchor. Never invent one.
        if candidate.get("category") in {"on_this_date", "century_ago"} and not text(candidate.get("date_anchor")):
            continue
        ok, verification = verify_candidate(providers, candidate)
        candidate["verification"] = verification
        if not ok:
            continue
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

    if len([p for p in state.get("posts", []) if text(p.get("published_at")).startswith(current.date().isoformat())]) < 2:
        # Even when daily anchors are successful, discovery remains independently capped.
        pass

    from .state import discovery_posts_today
    if discovery_posts_today(state) < MAX_DISCOVERY_POSTS_PER_DAY:
        raw_candidates = _evergreen_candidates(providers, state)
        selected = select_candidates(providers, raw_candidates, state)
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
    logger.info("Run complete. Published=%d", published)
    return published
