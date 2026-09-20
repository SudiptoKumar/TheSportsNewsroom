from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from .config import (
    AI_MAX_CALLS_PER_RUN,
    AI_MANDATORY_CALLS_PER_DAILY,
    AI_MAX_REPAIR_PER_STORY,
    APP_NAME,
    APP_VERSION,
    DAY_IN_SPORTS_AFTER_HOUR,
    MAX_CANDIDATES,
    MAX_VERIFICATION_CANDIDATES,
    MAX_DISCOVERY_POSTS_PER_DAY,
    NEXT_UP_AFTER_HOUR,
    POST_DELAY_SECONDS,
    TELEGRAM_CHANNEL,
)
from .content import build_daily_plan, generate_daily_story, generate_story
from .discovery import discover_evergreen, discover_historical_date, discover_next_sports, discover_past_sports
from .editorial import classify_candidates, select_candidates, verify_shortlist
from .lanes import begin_lane_attempt, plan_mandatory_lanes, update_lane_status
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
    publish_intent,
    set_publish_intent,
    save_state,
    update_source_health,
)
from .telegram import fit_rich_html, send_photo_fallback, send_story
from .utils import iso, sha, text
from .verification import verify_story_against_evidence

logger = logging.getLogger("sports-games-hub.pipeline")


def _record_post(
    state: dict,
    story: dict,
    message_id: object = None,
    publish_intent_key: str = "",
    story_digest: str = "",
) -> None:
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
        "publish_intent_key": publish_intent_key,
        "story_digest": story_digest,
    })
    state.setdefault("angle_history", []).append({"angle": angle, "at": published_at})
    state.setdefault("category_history", []).append({"category": text(story.get("category")), "at": published_at})
    state.setdefault("subject_history", []).append({"subject": subject, "at": published_at})


def _publish_intent_key(story: dict) -> str:
    fmt = text(story.get("format"))
    date_anchor = text(story.get("date_anchor"))
    if fmt == "daily_next" and date_anchor:
        return f"daily:next:{date_anchor}"
    if fmt == "daily_past" and date_anchor:
        return f"daily:past:{date_anchor}"
    candidate = story.get("candidate", {}) or {}
    candidate_id = text(candidate.get("candidate_id"))
    if candidate_id:
        return f"discovery:{candidate_id}"
    return "story:" + sha("|".join([
        fmt, date_anchor, text(story.get("headline")), text(story.get("claim")),
    ]), 28)


def _publish(state: dict, story: dict, index: int, report: PipelineReport) -> bool:
    """Publish with a persisted intent guard to prevent automatic duplicate sends after crashes."""
    intent_key = _publish_intent_key(story)
    existing = publish_intent(state, intent_key)
    existing_status = text(existing.get("status"))
    if existing_status == "published":
        # A crash can occur after the intent is durably marked published but before the
        # post metadata is persisted. Reconstruct that metadata on the next run, using
        # message-id or story digest to avoid duplicating an already recorded post.
        existing_message_id = existing.get("message_id")
        existing_digest = text(existing.get("story_digest"))
        posts = state.setdefault("posts", [])
        already_recorded = any(
            text(post.get("publish_intent_key")) == intent_key
            or (existing_message_id is not None and post.get("message_id") == existing_message_id)
            or (existing_digest and text(post.get("story_digest")) == existing_digest)
            for post in posts
        )
        if not already_recorded:
            _record_post(
                state,
                story,
                existing_message_id,
                publish_intent_key=intent_key,
                story_digest=existing_digest,
            )
            save_state(state)
            report.count("publish.recovered_metadata")
        report.publish(text(story.get("format")), text(story.get("headline")), True)
        report.count("publish.already_published")
        return True
    if existing_status in {"unknown", "sending"}:
        report.reject("publish", "delivery_uncertain", text(story.get("headline")), "manual reconciliation required before any resend", candidate_id=text((story.get("candidate") or {}).get("candidate_id")))
        return False

    attempt = int(existing.get("attempts", 0) or 0) + 1
    story_digest = sha(str({k: story.get(k) for k in ("format", "date_anchor", "headline", "dek", "body", "claim", "sources")}), 28)
    set_publish_intent(
        state,
        intent_key,
        status="sending",
        attempts=attempt,
        initiated_at=iso(datetime.now(CONFIG.tz)),
        updated_at=iso(datetime.now(CONFIG.tz)),
        story_digest=story_digest,
        lane=text(story.get("format")),
        target_date=text(story.get("date_anchor")),
    )
    # Persist the intent BEFORE touching Telegram. A process crash after Telegram accepts the
    # request can therefore never trigger a blind resend on the next scheduled run.
    save_state(state)

    with report.stage("publish"):
        rich = fit_rich_html(story)
        image = ""
        result = {"ok": False, "description": "publish failed"}
        phase = "media"
        try:
            image = create_visual(story, index)
            phase = "telegram"
            result = send_story(image, rich)
            if result.get("_delivery_uncertain"):
                # A timeout/5xx/network error can mean Telegram accepted the request even though
                # the client did not receive confirmation. Never auto-fallback in this case.
                result = {**result, "_publish_status": "unknown"}
            elif not result.get("ok"):
                logger.warning("sendRichMessage failed; trying sendPhoto fallback: %s", result.get("description"))
                result = send_photo_fallback(image, rich)
                if result.get("_delivery_uncertain"):
                    result = {**result, "_publish_status": "unknown"}
        except Exception as exc:
            logger.exception("Publish phase failed (%s)", phase)
            result = {
                "ok": False,
                "description": str(exc),
                "_publish_status": "unknown" if phase == "telegram" else "failed",
            }
        finally:
            if image:
                try:
                    os.remove(image)
                except OSError:
                    pass

    lane = text(story.get("format"))
    now = datetime.now(CONFIG.tz)
    if result.get("_publish_status") == "unknown":
        set_publish_intent(state, intent_key, status="unknown", updated_at=iso(now), last_error=text(result.get("description")))
        save_state(state)
        report.publish(lane, text(story.get("headline")), False)
        report.reject("publish", "delivery_uncertain", text(story.get("headline")), text(result.get("description")))
        return False
    if not result.get("ok"):
        set_publish_intent(state, intent_key, status="failed", updated_at=iso(now), last_error=text(result.get("description")))
        save_state(state)
        report.publish(lane, text(story.get("headline")), False)
        logger.error("Telegram publish failed: %s", result.get("description"))
        return False

    message = result.get("result", {})
    message_id = message.get("message_id") if isinstance(message, dict) else None
    set_publish_intent(
        state,
        intent_key,
        status="published",
        updated_at=iso(now),
        message_id=message_id,
        confirmed_at=iso(now),
    )
    for url in story.get("sources", []):
        remember_published_url(url)
    _record_post(
        state,
        story,
        message_id,
        publish_intent_key=intent_key,
        story_digest=story_digest,
    )
    # Persist the confirmed outcome before returning from the send boundary. The outer
    # pipeline also saves state, but this closes the smallest crash window between a
    # successful Telegram response and the next run.
    save_state(state)
    report.publish(lane, text(story.get("headline")), True)
    return True


def _run_daily_lane(
    providers: Providers,
    state: dict,
    job: dict,
    report: PipelineReport,
) -> tuple[dict | None, str, str]:
    """Execute one mandatory lane as collect -> plan -> draft -> validate -> ground."""
    lane = job["lane"]
    target = job["target_date"]
    row = begin_lane_attempt(state, job, datetime.now(CONFIG.tz))
    save_state(state)

    try:
        update_lane_status(state, job, "running", datetime.now(CONFIG.tz), step="collect")
        with report.stage(f"daily_{lane}_discovery"):
            candidates = discover_next_sports(providers, target) if lane == "next" else discover_past_sports(providers, target)
        report.count(f"daily.{lane}_candidates", len(candidates))
        if not candidates:
            update_lane_status(state, job, "no_data", datetime.now(CONFIG.tz), reason="no_candidates", step="collect")
            report.reject("daily", f"{lane}_no_candidates")
            save_state(state)
            return None, "no_data", "no_candidates"

        update_lane_status(state, job, "running", datetime.now(CONFIG.tz), step="plan", detail=f"candidates={len(candidates)}")
        with report.stage(f"daily_{lane}_plan"):
            plan = build_daily_plan(providers, candidates, "NEXT" if lane == "next" else "PAST", target)
        if not plan:
            update_lane_status(state, job, "failed", datetime.now(CONFIG.tz), reason="plan_failed", step="plan")
            report.reject("daily", f"{lane}_plan_failed")
            save_state(state)
            return None, "failed", "plan_failed"
        report.count(f"daily.{lane}_events", len(plan["events"]))

        update_lane_status(state, job, "running", datetime.now(CONFIG.tz), step="draft")
        with report.stage(f"daily_{lane}_generation"):
            story = generate_daily_story(providers, plan)
        if not story:
            update_lane_status(state, job, "failed", datetime.now(CONFIG.tz), reason="story_generation_failed", step="draft")
            report.reject("daily", f"{lane}_story_failed")
            save_state(state)
            return None, "failed", "story_generation_failed"

        update_lane_status(state, job, "running", datetime.now(CONFIG.tz), step="ground")
        with report.stage(f"daily_{lane}_grounding"):
            grounded = verify_story_against_evidence(providers, story, story.get("candidate", {}), lane="mandatory")
        if not grounded:
            update_lane_status(state, job, "failed", datetime.now(CONFIG.tz), reason="grounding_failed", step="ground")
            report.reject("daily", f"{lane}_grounding_failed", text(story.get("headline")))
            save_state(state)
            return None, "failed", "grounding_failed"

        update_lane_status(state, job, "ready", datetime.now(CONFIG.tz), step="ready")
        save_state(state)
        return story, "ready", ""
    except Exception as exc:
        update_lane_status(state, job, "failed", datetime.now(CONFIG.tz), reason=str(exc), step="exception")
        save_state(state)
        raise


def _evergreen_candidates(providers: Providers, state: dict, report: PipelineReport) -> tuple[list[dict], list[str]]:
    now = datetime.now(CONFIG.tz)
    day_index = now.timetuple().tm_yday + now.year * 13
    with report.stage("discovery_evergreen"):
        evergreen = discover_evergreen(providers, day_index)
    with report.stage("discovery_history"):
        historical = discover_historical_date(providers, now.date(), day_index)[:25]
    raw = list(evergreen) + list(historical)
    report.count("candidate.raw", len(raw))

    raw_ids = [text(c.get("candidate_id")) for c in raw if text(c.get("candidate_id"))]
    for candidate in raw:
        cid = text(candidate.get("candidate_id"))
        if cid:
            report.candidate("raw", cid, "available")

    if not raw:
        return [], []

    ordered = sorted(raw, key=lambda x: float(x.get("prelim_score", 0)), reverse=True)
    prefiltered = ordered[:MAX_CANDIDATES]
    overflow = ordered[MAX_CANDIDATES:]
    report.count("candidate.pre_filtered", len(prefiltered))
    report.count("candidate.capacity_rejected", len(overflow))
    for candidate in overflow:
        cid = text(candidate.get("candidate_id"))
        report.candidate("pre_filter", cid, "rejected", "candidate_capacity")
        report.candidate_terminal(cid, "rejected", "candidate_capacity")
        report.reject("pre_filter", "candidate_capacity", text(candidate.get("title")), candidate_id=cid)
        record_candidate(state, candidate, "rejected", "candidate_capacity")
    for candidate in prefiltered:
        cid = text(candidate.get("candidate_id"))
        report.candidate("pre_filter", cid, "accepted")

    # Classifier itself is responsible for explaining the 100 -> 36 narrowing.
    classified = classify_candidates(
        providers,
        prefiltered,
        mode="evergreen + discovery + history",
        state=state,
        report=report,
    )
    report.count("candidate.classified", len(classified))
    save_state(state)

    verified = verify_shortlist(providers, classified, state, max_items=MAX_VERIFICATION_CANDIDATES, report=report)
    report.count("verification.attempted", min(MAX_VERIFICATION_CANDIDATES, len(classified)))
    report.count("verification.verified", len(verified))
    save_state(state)
    return verified, raw_ids


def run_once() -> int:
    report = PipelineReport()
    state = load_state()
    prune_state(state)
    current = datetime.now(CONFIG.tz)
    mandatory_jobs = plan_mandatory_lanes(state, current, report)
    reserved = min(AI_MAX_CALLS_PER_RUN, len(mandatory_jobs) * max(0, AI_MANDATORY_CALLS_PER_DAILY))
    budget = AiBudget(AI_MAX_CALLS_PER_RUN, reserved, report)
    published = 0
    stories: list[tuple[dict, dict | None]] = []
    evergreen_candidate_ids: list[str] = []
    try:
        providers = Providers.from_env(require=True, report=report, ai_budget=budget)
        state["last_run_at"] = iso(current)
        logger.info(
            "%s v%s | channel=%s | mandatory_jobs=%d | mandatory_reserve=%d",
            APP_NAME, APP_VERSION, TELEGRAM_CHANNEL, len(mandatory_jobs), reserved,
        )

        # Mandatory target-date execution always runs first. A no-data/failure is persisted so
        # the next scheduled run can retry inside the same window instead of silently forgetting it.
        for job in mandatory_jobs:
            try:
                story, status, reason = _run_daily_lane(providers, state, job, report)
            except Exception as exc:
                report.count(f"daily.{job['lane']}_exception")
                logger.exception("Mandatory lane %s failed but run will continue: %s", job["lane"], exc)
                continue
            if story is not None and status == "ready":
                stories.append((story, job))
            elif status in {"no_data", "failed"}:
                report.count(f"daily.{job['lane']}_{status}")

        # Once all mandatory jobs for this run have had a chance to execute, unused reserved
        # budget becomes available to discovery.
        budget.release_unused_mandatory()

        if discovery_posts_today(state) < MAX_DISCOVERY_POSTS_PER_DAY and budget.remaining("discovery") > 0:
            raw_candidates, evergreen_candidate_ids = _evergreen_candidates(providers, state, report)
            with report.stage("editorial_selection"):
                selected = select_candidates(providers, raw_candidates, state, report=report)
            report.count("editorial.selected", len(selected))
            for candidate in selected:
                with report.stage("generation"):
                    story = generate_story(providers, candidate, lane="discovery", max_repairs=AI_MAX_REPAIR_PER_STORY)
                cid = text(candidate.get("candidate_id"))
                if story:
                    stories.append((story, None))
                else:
                    record_candidate(state, candidate, "rejected", "story_generation_failed")
                    report.reject(
                        "generation",
                        "story_generation_failed",
                        text(candidate.get("title")),
                        candidate_id=cid,
                    )
                    report.candidate("generation", cid, "rejected", "story_generation_failed")
                    report.candidate_terminal(cid, "rejected", "story_generation_failed")
        else:
            report.count("editorial.discovery_budget_or_quota_blocked")

        for index, (story, daily_job) in enumerate(stories, start=1):
            if daily_job is not None:
                report.count(f"daily.{daily_job['lane']}_publish_attempt")
            ok = _publish(state, story, index, report)
            cid = text((story.get("candidate") or {}).get("candidate_id"))
            if ok:
                published += 1
                if cid:
                    record_candidate(state, story.get("candidate") or {}, "published", "telegram_confirmed")
                    report.candidate("publish", cid, "published", "telegram_confirmed")
                    report.candidate_terminal(cid, "published", "telegram_confirmed")
                fmt = text(story.get("format"))
                if fmt == "daily_next":
                    target = text(story.get("date_anchor"))
                    mark_daily(state, "next", target)
                    if daily_job is not None:
                        update_lane_status(state, daily_job, "published", datetime.now(CONFIG.tz), step="publish")
                elif fmt == "daily_past":
                    target = text(story.get("date_anchor"))
                    mark_daily(state, "past", target)
                    if daily_job is not None:
                        update_lane_status(state, daily_job, "published", datetime.now(CONFIG.tz), step="publish")
                save_state(state)
                time.sleep(POST_DELAY_SECONDS)
            else:
                status = publish_intent(state, _publish_intent_key(story)).get("status")
                if cid:
                    reason = "delivery_uncertain" if status in {"unknown", "sending"} else "publish_failed"
                    record_candidate(state, story.get("candidate") or {}, "rejected", reason)
                    report.candidate("publish", cid, "rejected", reason)
                    report.candidate_terminal(cid, "rejected", reason)
                if daily_job is not None:
                    update_lane_status(
                        state,
                        daily_job,
                        "unknown" if status in {"unknown", "sending"} else "failed",
                        datetime.now(CONFIG.tz),
                        reason="delivery_uncertain" if status in {"unknown", "sending"} else "publish_failed",
                        step="publish",
                    )
                save_state(state)

        # Every evergreen candidate that entered the pre-filter must have a terminal outcome.
        if evergreen_candidate_ids:
            missing = report.finalize_candidates(evergreen_candidate_ids)
            if missing:
                for cid in missing:
                    report.reject("ledger", "unaccounted_candidate", candidate_id=cid)
            report.count("candidate.terminal_unaccounted", len(missing))
        report.assert_no_unaccounted()
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

