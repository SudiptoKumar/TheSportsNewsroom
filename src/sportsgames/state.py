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
    }


def ensure_state_shape(state: dict) -> dict:
    merged = default_state()
    merged.update(state if isinstance(state, dict) else {})
    if int(merged.get("schema_version", 0) or 0) < 3:
        merged["schema_version"] = 3
    for key in default_state():
        if key not in merged or merged[key] is None:
            merged[key] = default_state()[key]
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
    tmp.write_text(json.dumps(ensure_state_shape(state), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_published_urls() -> set[str]:
    path = CONFIG.published_file
    if not path.exists():
        return set()
    return {x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


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


def prune_state(state: dict) -> None:
    cutoff = datetime.now(CONFIG.tz) - timedelta(days=STATE_RETENTION_DAYS)
    for key, item in list(state.get("queue", {}).items()):
        seen = parse_dt(item.get("first_seen_at")) if isinstance(item, dict) else None
        if seen and seen < cutoff:
            state["queue"].pop(key, None)
    state["posts"] = state.get("posts", [])[-12000:]
    state["angle_history"] = state.get("angle_history", [])[-3000:]
    state["category_history"] = state.get("category_history", [])[-3000:]
    state["subject_history"] = state.get("subject_history", [])[-3000:]
    for key, value in list(state.get("source_health", {}).items()):
        if not isinstance(value, dict):
            state["source_health"].pop(key, None)
            continue
        last = parse_dt(value.get("updated_at"))
        if last and last < cutoff:
            state["source_health"].pop(key, None)


def claim_key(subject: str, claim: str, angle: str = "") -> str:
    return sha(f"{normalize_text(subject)}|{normalize_text(claim)}|{normalize_text(angle)}", 28)


def entity_key(name: str) -> str:
    return normalize_text(name)


def mark_daily(state: dict, kind: str, target_date: str, at: str | None = None) -> None:
    state.setdefault("daily_flags", {})[f"{kind}:{target_date}"] = at or now_iso()


def daily_done(state: dict, kind: str, target_date: str) -> bool:
    return bool(state.get("daily_flags", {}).get(f"{kind}:{target_date}"))


def published_posts_today(state: dict) -> int:
    today = datetime.now(CONFIG.tz).date().isoformat()
    return sum(1 for p in state.get("posts", []) if text(p.get("published_at")).startswith(today))


def discovery_posts_today(state: dict) -> int:
    today = datetime.now(CONFIG.tz).date().isoformat()
    return sum(
        1 for p in state.get("posts", [])
        if text(p.get("published_at")).startswith(today)
        and p.get("category") not in {"sports_daily_next", "sports_daily_past"}
    )
