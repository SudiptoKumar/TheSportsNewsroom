from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .config import DAY_IN_SPORTS_AFTER_HOUR, MANDATORY_MAX_ATTEMPTS, NEXT_UP_AFTER_HOUR
from .state import daily_done
from .utils import iso, text

LANE_NEXT = "next"
LANE_PAST = "past"
TERMINAL_STATUSES = {"published", "skipped", "expired", "unknown", "sending", "gave_up"}
RETRYABLE_STATUSES = {"planned", "running", "failed", "no_data"}


def lane_key(lane: str, target_date: str) -> str:
    return f"{lane}:{target_date}"


def _dt_at(day: date, hour: int) -> datetime:
    # Planner comparisons use the timezone-aware current datetime supplied by the caller.
    # The returned naive value is only used for human-readable window metadata.
    return datetime.combine(day, time(hour=hour))


def window_for(lane: str, target: date) -> tuple[datetime, datetime]:
    if lane == LANE_NEXT:
        return _dt_at(target - timedelta(days=1), NEXT_UP_AFTER_HOUR), _dt_at(target, NEXT_UP_AFTER_HOUR)
    return _dt_at(target, DAY_IN_SPORTS_AFTER_HOUR), _dt_at(target + timedelta(days=1), NEXT_UP_AFTER_HOUR)


def _status(state: dict, lane: str, target: date) -> str:
    row = state.get("mandatory_lanes", {}).get(lane_key(lane, target.isoformat()), {})
    return text(row.get("status")) or ("published" if daily_done(state, lane, target.isoformat()) else "")


def _open_due(lane: str, now: datetime, target: date) -> bool:
    if lane == LANE_NEXT:
        # 00:00-05:59 catches up the NEXT UP post for the current date.
        if target == now.date() and now.hour < NEXT_UP_AFTER_HOUR:
            return True
        # From the daily opening time onward, the target is the following date.
        if target == now.date() + timedelta(days=1) and now.hour >= NEXT_UP_AFTER_HOUR:
            return True
        return False

    # 00:00-05:59 catches up the previous day's THE DAY IN SPORTS post.
    if target == now.date() - timedelta(days=1) and now.hour < NEXT_UP_AFTER_HOUR:
        return True
    # From the evening opening time onward, the target is the previous date.
    if target == now.date() - timedelta(days=1) and now.hour >= DAY_IN_SPORTS_AFTER_HOUR:
        return True
    return False


def _mark_gave_up(state: dict, lane: str, target: date, now: datetime, trace: list[dict]) -> None:
    key = lane_key(lane, target.isoformat())
    row = state.setdefault("mandatory_lanes", {}).setdefault(key, {
        "lane": lane,
        "target_date": target.isoformat(),
        "status": "",
        "attempts": 0,
        "updated_at": "",
        "last_error": "",
        "trace": [],
    })
    if row.get("status") in TERMINAL_STATUSES:
        return
    attempts = int(row.get("attempts", 0) or 0)
    row.update({
        "status": "gave_up",
        "updated_at": iso(now),
        "last_error": f"max_attempts:{MANDATORY_MAX_ATTEMPTS}",
    })
    row.setdefault("trace", []).append({
        "at": iso(now),
        "step": "give_up",
        "status": "gave_up",
        "reason": f"max_attempts:{MANDATORY_MAX_ATTEMPTS}",
        "attempts": attempts,
    })
    row["trace"] = row["trace"][-20:]
    trace.append({"step": "give_up", "target_date": target.isoformat(), "status": "gave_up", "attempts": attempts})


def _mark_expired(state: dict, lane: str, target: date, now: datetime, trace: list[dict]) -> None:
    key = lane_key(lane, target.isoformat())
    row = state.setdefault("mandatory_lanes", {}).setdefault(key, {
        "lane": lane,
        "target_date": target.isoformat(),
        "status": "",
        "attempts": 0,
        "updated_at": "",
        "last_error": "",
        "trace": [],
    })
    if row.get("status") in TERMINAL_STATUSES or row.get("status") == "published":
        return
    row.update({
        "status": "expired",
        "updated_at": iso(now),
        "last_error": "window_closed",
    })
    row.setdefault("trace", []).append({"at": iso(now), "step": "expire", "status": "expired", "reason": "window_closed"})
    row["trace"] = row["trace"][-20:]
    trace.append({"step": "expire", "target_date": target.isoformat(), "status": "expired"})


def plan_mandatory_lanes(state: dict, now: datetime, report: Any = None) -> list[dict]:
    """Return the currently due mandatory target-date jobs and expire stale catch-up windows.

    Window model:
      NEXT UP: previous-day opening at NEXT_UP_AFTER_HOUR through target-day opening.
      THE DAY IN SPORTS: target-day opening at DAY_IN_SPORTS_AFTER_HOUR through next-day
      catch-up cutoff at NEXT_UP_AFTER_HOUR.
    """
    state.setdefault("mandatory_lanes", {})
    changes: list[dict] = []

    # Bound repeated source/AI failures. An uncertain Telegram delivery is deliberately
    # excluded because it requires human reconciliation rather than an automatic retry.
    for key, row in list(state.get("mandatory_lanes", {}).items()):
        if not isinstance(row, dict) or row.get("status") not in RETRYABLE_STATUSES:
            continue
        attempts = int(row.get("attempts", 0) or 0)
        if attempts < MANDATORY_MAX_ATTEMPTS:
            continue
        try:
            lane, target_text = key.split(":", 1)
            target = date.fromisoformat(target_text)
        except (ValueError, TypeError):
            continue
        # Give-up is an in-window retry limit. Once the target's publication window has
        # closed, the normal expiry path owns the terminal state instead.
        if not _open_due(lane, now, target):
            continue
        _mark_gave_up(state, lane, target, now, changes)

    # A missed NEXT UP for the current day becomes stale once its 06:00-style cutoff passes.
    today = now.date()
    next_today_status = _status(state, LANE_NEXT, today)
    if now.hour >= NEXT_UP_AFTER_HOUR and next_today_status != "published":
        _mark_expired(state, LANE_NEXT, today, now, changes)

    # A missed PAST post is only useful through the next-day early-morning catch-up window.
    yesterday = today - timedelta(days=1)
    past_yesterday_status = _status(state, LANE_PAST, yesterday)
    if NEXT_UP_AFTER_HOUR <= now.hour < DAY_IN_SPORTS_AFTER_HOUR and past_yesterday_status != "published":
        _mark_expired(state, LANE_PAST, yesterday, now, changes)

    candidates = [
        (LANE_NEXT, today if now.hour < NEXT_UP_AFTER_HOUR else today + timedelta(days=1)),
        (LANE_PAST, yesterday) if (now.hour < NEXT_UP_AFTER_HOUR or now.hour >= DAY_IN_SPORTS_AFTER_HOUR) else None,
    ]

    jobs: list[dict] = []
    for item in candidates:
        if item is None:
            continue
        lane, target = item
        if not _open_due(lane, now, target):
            continue
        status = _status(state, lane, target)
        if status in TERMINAL_STATUSES or status == "published":
            if report:
                report.count(f"daily.{lane}_already_done")
            continue
        key = lane_key(lane, target.isoformat())
        window_open, window_close = window_for(lane, target)
        job = {
            "lane": lane,
            "target_date": target,
            "key": key,
            "prior_status": status,
            "attempt": int(state.get("mandatory_lanes", {}).get(key, {}).get("attempts", 0) or 0) + 1,
            "window_open": window_open.isoformat(),
            "window_close": window_close.isoformat(),
        }
        jobs.append(job)
        if report:
            report.count(f"daily.{lane}_due")

    if changes and report:
        report.count("daily.expired", len(changes))
    return jobs


def begin_lane_attempt(state: dict, job: dict, now: datetime) -> dict:
    key = job["key"]
    row = state.setdefault("mandatory_lanes", {}).setdefault(key, {
        "lane": job["lane"],
        "target_date": job["target_date"].isoformat(),
        "status": "",
        "attempts": 0,
        "updated_at": "",
        "last_error": "",
        "trace": [],
    })
    row["attempts"] = int(row.get("attempts", 0) or 0) + 1
    row["status"] = "running"
    row["updated_at"] = iso(now)
    row["last_error"] = ""
    row["trace"] = (row.get("trace") or [])[-19:]
    row["trace"].append({"at": iso(now), "step": "start", "status": "running"})
    return row


def update_lane_status(state: dict, job: dict, status: str, now: datetime, *, reason: str = "", step: str = "", detail: str = "") -> None:
    key = job["key"]
    row = state.setdefault("mandatory_lanes", {}).setdefault(key, {
        "lane": job["lane"],
        "target_date": job["target_date"].isoformat(),
        "status": "",
        "attempts": 0,
        "updated_at": "",
        "last_error": "",
        "trace": [],
    })
    row["status"] = status
    row["updated_at"] = iso(now)
    if reason:
        row["last_error"] = reason[:240]
    if step:
        row.setdefault("trace", []).append({
            "at": iso(now),
            "step": step,
            "status": status,
            "reason": reason[:160],
            "detail": detail[:240],
        })
        row["trace"] = row["trace"][-20:]


def lane_status(state: dict, lane: str, target: date) -> dict:
    key = lane_key(lane, target.isoformat())
    return dict(state.get("mandatory_lanes", {}).get(key, {}))
