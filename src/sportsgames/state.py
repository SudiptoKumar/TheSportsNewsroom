from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .config import CONFIG, STATE_RETENTION_DAYS
from .utils import canonical_url, normalize_text, parse_dt, sha, text

logger = logging.getLogger("sports-games-hub.state")


def now_iso() -> str:
    return datetime.now(CONFIG.tz).isoformat()


def default_state() -> dict:
    return {
        "schema_version": 3,
        "created_at": now_iso(),
        "last_run_at": "",
        "posts": [],
        "claims": {},
        "entities": {},
        "events": {},
        "historical_events": {},
        "daily_flags": {},
        "angle_history": [],
        "category_history": [],
        "subject_history": [],
        "source_health": {},
        "queue": {},
        "candidate_ledger": [],
        "run_history": [],
    }


def ensure_state_shape(state: dict) -> dict:
    merged = default_state()
    if isinstance(state, dict):
        merged.update(state)
    defaults = default_state()
    for key, value in defaults.items():
        if key not in merged or merged[key] is None:
            merged[key] = value
    return merged


def load_state() -> dict:
    path = CONFIG.state_file
    if not path.exists() or path.stat().st_size == 0:
        return default_state()
    try:
        return ensure_state_shape(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning("State load failed, using fresh state: %s", exc)
        return default_state()


def save_state(state: dict) -> None:
    path = CONFIG.state_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(
        json.dumps(ensure_state_shape(state), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_published_urls() -> set[str]:
    path = CONFIG.published_file
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def remember_published_url(url: str) -> None:
    canonical = canonical_url(url)
    if not canonical:
        return
    path = CONFIG.published_file
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_published_urls()
    if canonical in current:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical + "\n")


def record_candidate(
    state: dict,
    candidate: dict,
    outcome: str,
    reason: str = "",
    score: float | None = None,
) -> None:
    ledger = state.setdefault("candidate_ledger", [])
    ledger.append({
        "at": now_iso(),
        "candidate_id": text(candidate.get("candidate_id")),
        "title": text(candidate.get("title"))[:140],
        "family": text(candidate.get("query_family")),
        "category": text(candidate.get("category")),
        "angle": text(candidate.get("angle")),
        "subject": text(candidate.get("subject")),
        "outcome": outcome,
        "reason": reason[:160],
        "score": score,
    })
    state["candidate_ledger"] = ledger[-1000:]


def record_run_summary(state: dict, report_dict: dict) -> None:
    history = state.setdefault("run_history", [])
    history.append({
        "at": now_iso(),
        "total_seconds": report_dict.get("total_seconds", 0),
        "counts": report_dict.get("counts", {}),
        "reject_reasons": report_dict.get("reject_reasons", {}),
        "published": report_dict.get("published", []),
        "ai": report_dict.get("ai", {}),
    })
    state["run_history"] = history[-60:]


def update_source_health(state: dict, sources: list[dict]) -> None:
    health = state.setdefault("source_health", {})
    for item in sources:
        domain = text(item.get("domain"))
        if not domain:
            continue
        row = health.setdefault(domain, {
            "attempts": 0,
            "ok": 0,
            "fail": 0,
            "last_error": "",
            "updated_at": "",
        })
        row["attempts"] += 1
        if item.get("ok"):
            row["ok"] += 1
        else:
            row["fail"] += 1
            row["last_error"] = text(item.get("error"))[:160]
        row["updated_at"] = now_iso()


def prune_state(state: dict) -> None:
    cutoff = datetime.now(CONFIG.tz) - timedelta(days=STATE_RETENTION_DAYS)
    for key, item in list(state.get("queue", {}).items()):
        seen = parse_dt(item.get("first_seen_at")) if isinstance(item, dict) else None
        if seen and seen < cutoff:
            state["queue"].pop(key, None)
    state["posts"] = state.get("posts", [])[-10000:]
    state["angle_history"] = state.get("angle_history", [])[-2500:]
    state["category_history"] = state.get("category_history", [])[-2500:]
    state["subject_history"] = state.get("subject_history", [])[-2500:]
    state["candidate_ledger"] = state.get("candidate_ledger", [])[-1000:]
    state["run_history"] = state.get("run_history", [])[-60:]
    for domain, value in list(state.get("source_health", {}).items()):
        if not isinstance(value, dict):
            state["source_health"].pop(domain, None)
            continue
        last = parse_dt(value.get("updated_at"))
        if last and last < cutoff:
            state["source_health"].pop(domain, None)


def claim_key(subject: str, claim: str, angle: str = "") -> str:
    return sha(f"{normalize_text(subject)}|{normalize_text(claim)}|{normalize_text(angle)}", 28)


def core_claim_key(subject: str, claim: str) -> str:
    return sha(f"{normalize_text(subject)}|{normalize_text(claim)}", 28)


def event_key(sport: str, event: str, target_date: str) -> str:
    return sha(f"{normalize_text(sport)}|{normalize_text(event)}|{target_date}", 28)


def entity_key(name: str) -> str:
    return normalize_text(name)


def mark_daily(state: dict, kind: str, target_date: str, at: str | None = None) -> None:
    state.setdefault("daily_flags", {})[f"{kind}:{target_date}"] = at or now_iso()


def daily_done(state: dict, kind: str, target_date: str) -> bool:
    return bool(state.get("daily_flags", {}).get(f"{kind}:{target_date}"))


def published_posts_today(state: dict) -> int:
    today = datetime.now(CONFIG.tz).date().isoformat()
    return sum(1 for post in state.get("posts", []) if text(post.get("published_at")).startswith(today))


def discovery_posts_today(state: dict) -> int:
    today = datetime.now(CONFIG.tz).date().isoformat()
    return sum(
        1
        for post in state.get("posts", [])
        if text(post.get("published_at")).startswith(today)
        and post.get("category") not in {"sports_daily_next", "sports_daily_past"}
    )
