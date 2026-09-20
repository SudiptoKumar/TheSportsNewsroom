from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from .config import (
    AI_MAX_CALLS_PER_RUN,
    AI_MANDATORY_CALLS_PER_DAILY,
    AI_MAX_REPAIR_PER_STORY,
    APP_NAME,
    APP_VERSION,
    DAY_IN_SPORTS_AFTER_HOUR,
    MAX_CANDIDATES,
    MAX_DISCOVERY_POSTS_PER_DAY,
    NEXT_UP_AFTER_HOUR,
    POST_DELAY_SECONDS,
    TELEGRAM_CHANNEL,
)
from .content import build_daily_plan, generate_daily_story, generate_story
from .discovery import discover_evergreen, discover_historical_date, discover_next_sports, discover_past_sports
from .editorial import classify_candidates, select_candidates, verify_shortlist
from .media import create_visual
from .observability import AiBudget, PipelineReport
from .providers import Providers
from .state import (
    CONFIG,
    claim_key,
    core_claim_key,
    daily_done,
    discovery_posts_today,
    entity_key,
    load_state,
    mark_daily,
    prune_state,
    record_candidate,
    record_run_summary,
    remember_published_url,
    save_state,
    update_source_health,
)
from .telegram import fit_rich_html, send_photo_fallback, send_story
from .utils import iso, text
from .verification import verify_story_against_evidence

logger = logging.getLogger("sports-games-hub.pipeline")


def _record_post(state: dict, story: dict, message_id: object = None) -> None:
    subject = text(story.get("subject"))
    claim = text(story.get("claim"))
    angle = text(story.get("angle"))
    published_at = iso(datetime.now(CONFIG.tz))
    entry = {
        "subject": subject,
        "claim": claim,
        "angle": angle,
        "category": text(story.get("category")),
        "published_at": published_at,
        "sources": list(story.get("sources", []))[:6],
    }
    state.setdefault("claims", {})[claim_key(subject, claim, angle)] = entry
    state.setdefault("claims", {})[core_claim_key(subject, claim)] = entry

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
        category = text(story.get("category"))
        info["categories"][category] = info["categories"].get(category, 0) + 1
        info["angles"][angle] = info["angles"].get(angle, 0) + 1
        info["last_posted_at"] = published_at

    candidate = story.get("candidate", {}) or {}
    state.setdefault("posts", []).append({
        "published_at": published_at,
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
    state.setdefault("angle_history", []).append({"angle": angle, "at": published_at})
    state.setdefault("category_history", []).append({"category": text(story.get("category")), "at": published_at})
    state.setdefault("subject_history", []).append({"subject": subject, "at": published_at})


def _publish(state: dict, story: dict, index: int, report: PipelineReport) -> bool:
    with report.stage("publish"):
        rich = fit_rich_html(story)
        image = create_visual(story, index)
        try:
            result = send_story(image, rich)
            if not result.get("ok"):
                logger.warning("sendRichMessage failed; trying sendPhoto fallback: %s", result.get("description"))
                result = send_photo_fallback(image, rich)
        finally:
            try:
                os.remove(image)
            except OSError:
                pass

    lane = text(story.get("format"))
    if not result.get("ok"):
        report.publish(lane, text(story.get("headline")), False)
        logger.error("Telegram publish failed: %s", result.get("description"))
        return False

    message = result.get("result", {})
    message_id = message.get("message_id") if isinstance(message, dict) else None
    for url in story.get("sources", []):
        remember_published_url(url)
    _record_post(state, story, message_id)
    report.publish(lane, text(story.get("headline")), True)
    return True


def _daily_story(
    providers: Providers,
    state: dict,
    kind: str,
    target,
    report: PipelineReport,
) -> dict | None:
    flag_kind = "next" if kind == "next" else "past"
    if daily_done(state, flag_kind, target.isoformat()):
        report.count(f"daily.{flag_kind}_already_done")
        return None

    with report.stage(f"daily_{flag_kind}_discovery"):
        candidates = discover_next_sports(providers, target) if kind == "next" else discover_past_sports(providers, target)
    report.count(f"daily.{flag_kind}_candidates", len(candidates))
    if not candidates:
        report.reject("daily", f"{flag_kind}_no_candidates")
        return None

    with report.stage(f"daily_{flag_kind}_plan"):
        plan = build_daily_plan(providers, candidates, "NEXT" if kind == "next" else "PAST", target)
    if not plan:
        report.reject("daily", f"{flag_kind}_plan_failed")
        return None
    report.count(f"daily.{flag_kind}_events", len(plan["events"]))

    with report.stage(f"daily_{flag_kind}_generation"):
        story = generate_daily_story(providers, plan)
    if not story:
        report.reject("daily", f"{flag_kind}_story_failed")
        return None

    with report.stage(f"daily_{flag_kind}_grounding"):
        if not verify_story_against_evidence(providers, story, story.get("candidate", {}), lane="mandatory"):
            report.reject("daily", f"{flag_kind}_grounding_failed", text(story.get("headline")))
            return None
    return story


def _evergreen_candidates(providers: Providers, state: dict, report: PipelineReport) -> list[dict]:
    now = datetime.now(CONFIG.tz)
    day_index = now.timetuple().tm_yday + now.year * 13
    with report.stage("discovery_evergreen"):
        raw = discover_evergreen(providers, day_index)
    with report.stage("discovery_history"):
        raw.extend(discover_historical_date(providers, now.date(), day_index)[:25])
    report.count("candidate.raw", len(raw))
    if not raw:
        return []

    raw = sorted(raw, key=lambda x: float(x.get("prelim_score", 0)), reverse=True)[:MAX_CANDIDATES]
    report.count("candidate.pre_filtered", len(raw))

    classified = classify_candidates(providers, raw, mode="evergreen + discovery + history", report=report)
    report.count("candidate.classified", len(classified))
    verified = verify_shortlist(providers, classified, state, max_items=3, report=report)
    report.count("verification.attempted", min(3, len(classified)))
    report.count("verification.verified", len(verified))
    return verified


def run_once() -> int:
    report = PipelineReport()
    state = load_state()
    prune_state(state)
    current = datetime.now(CONFIG.tz)
    due_lanes = int(current.hour >= NEXT_UP_AFTER_HOUR) + int(current.hour >= DAY_IN_SPORTS_AFTER_HOUR)
    reserved = min(AI_MAX_CALLS_PER_RUN, due_lanes * max(0, AI_MANDATORY_CALLS_PER_DAILY))
    budget = AiBudget(AI_MAX_CALLS_PER_RUN, reserved, report)
    published = 0
    try:
        providers = Providers.from_env(require=True, report=report, ai_budget=budget)
        state["last_run_at"] = iso(current)
        logger.info(
            "%s v%s | channel=%s | mandatory_reserve=%d",
            APP_NAME, APP_VERSION, TELEGRAM_CHANNEL, reserved,
        )
        stories: list[dict] = []

        # Mandatory lane first. Its AI reserve cannot be consumed by discovery.
        if current.hour >= NEXT_UP_AFTER_HOUR:
            report.count("daily.next_due")
            target = current.date() + timedelta(days=1)
            story = _daily_story(providers, state, "next", target, report)
            if story:
                stories.append(story)
        if current.hour >= DAY_IN_SPORTS_AFTER_HOUR:
            report.count("daily.past_due")
            target = current.date() - timedelta(days=1)
            story = _daily_story(providers, state, "past", target, report)
            if story:
                stories.append(story)

        if discovery_posts_today(state) < MAX_DISCOVERY_POSTS_PER_DAY and budget.remaining("discovery") > 0:
            raw_candidates = _evergreen_candidates(providers, state, report)
            with report.stage("editorial_selection"):
                selected = select_candidates(providers, raw_candidates, state)
            report.count("editorial.selected", len(selected))
            for candidate in selected:
                with report.stage("generation"):
                    story = generate_story(providers, candidate, lane="discovery", max_repairs=AI_MAX_REPAIR_PER_STORY)
                if story:
                    stories.append(story)
                else:
                    record_candidate(state, candidate, "rejected", "story_generation_failed")
                    report.reject(
                        "generation",
                        "story_generation_failed",
                        text(candidate.get("title")),
                        candidate_id=text(candidate.get("candidate_id")),
                    )
        else:
            report.count("editorial.discovery_budget_or_quota_blocked")

        for index, story in enumerate(stories, start=1):
            if _publish(state, story, index, report):
                published += 1
                fmt = text(story.get("format"))
                if fmt == "daily_next":
                    mark_daily(state, "next", text(story.get("date_anchor")))
                elif fmt == "daily_past":
                    mark_daily(state, "past", text(story.get("date_anchor")))
                save_state(state)
                time.sleep(POST_DELAY_SECONDS)

        update_source_health(state, report.sources)
        report.count("published.total", published)
        record_run_summary(state, report.to_dict())
        save_state(state)
        report.save(CONFIG.run_report_file)
        return published
    except Exception as exc:
        report.error(f"fatal: {exc}")
        logger.exception("Production run failed")
        update_source_health(state, report.sources)
        record_run_summary(state, report.to_dict())
        save_state(state)
        report.save(CONFIG.run_report_file)
        raise
    finally:
        report.emit(logger)
