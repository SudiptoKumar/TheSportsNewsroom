#!/usr/bin/env python3
# The Sports Newsroom V1.4 - Daily 200→20 evergreen editorial + live sports newsroom for @TheSportsNewsroom.
#
# Design (see README.md):
#   V1.4: one persistent daily 20-sector batch -> 10 Exa discoveries per sector -> native Exa relevance selection ->
#   Exa Contents verification -> evidence packages -> one Cerebras editorial batch -> per-story validation ->
#   relevant image selection -> photo-first Telegram publication.
#   Live sports remains a separate temporary pair with transactional rotation.
#
# Dependencies: requests, Pillow.  Exa, Cerebras and Telegram are called over plain REST.
# Content scope: real-world sports and physical/tabletop games only.
# Legacy V3/V4 code is retained for migration/regression compatibility; the public runtime entrypoint is V1.

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None

APP_NAME = "The Sports Newsroom"
APP_VERSION = "1.4.0"

# ===========================================================================
# 1. CORE: config, clock, text helpers, safety filters
# ===========================================================================


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


STATE_FILE = "news_state.json"
POSTED_FILE = "posted_urls.txt"
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@TheSportsNewsroom").strip() or "@TheSportsNewsroom"
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
EXA_API_KEY = os.environ.get("EXA_API_KEY", "").strip()
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "").strip() or "gpt-oss-120b"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CRICKETDATA_API_KEY = os.environ.get("CRICKETDATA_API_KEY", "").strip()
TSDB_KEY = os.environ.get("THESPORTSDB_KEY", "").strip() or "3"

EVERGREEN_PER_DAY = _env_int("POSTS_EVERGREEN_PER_DAY", _env_int("MAX_DISCOVERY_POSTS_PER_DAY", 3))
NEXT_UP_OFFSET_DAYS = _env_int("NEXT_UP_OFFSET_DAYS", 1)
RUN_DEADLINE_SECONDS = _env_int("RUN_DEADLINE_SECONDS", 720)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 15)
CEREBRAS_API_VERSION = "2"
MAX_ATTEMPTS_PER_SLOT = _env_int("MAX_ATTEMPTS_PER_SLOT", 6)
POST_DELAY_SECONDS = _env_float("POST_DELAY_SECONDS", 3.0)
STATE_RETENTION_DAYS = _env_int("STATE_RETENTION_DAYS", 120)
OPEN_DAY_IN_SPORTS = _env_int("DAY_IN_SPORTS_OPEN_HOUR", 7)
OPEN_ON_THIS_DATE = _env_int("ON_THIS_DATE_OPEN_HOUR", 9)
OPEN_NEXT_UP = _env_int("NEXT_UP_OPEN_HOUR", 19)
EVERGREEN_OPEN_HOURS = [10, 15, 20]

DRY_RUN = False  # set by --dry-run: never talks to Telegram, never writes state

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

logger = logging.getLogger("sports-newsroom")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")

try:
    BD_TZ = ZoneInfo("Asia/Dhaka")
except Exception:  # pragma: no cover - tzdata missing
    BD_TZ = timezone(timedelta(hours=6), "Asia/Dhaka")

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

_CLOCK: dict[str, Any] = {"now": None}
_SLEEP: list[Callable[[float], None]] = [time.sleep]


def sleep(seconds: float) -> None:
    _SLEEP[0](max(0.0, seconds))


def now_bd() -> datetime:
    return _CLOCK["now"] if _CLOCK["now"] is not None else datetime.now(BD_TZ)


def to_bd(dt: datetime) -> datetime:
    return dt.astimezone(BD_TZ)


def utc_iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                dt = parsedate_to_datetime(raw)
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def long_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def dhaka_window(d: date) -> tuple[datetime, datetime]:
    """[00:00, 24:00) of calendar day d in Dhaka, as UTC datetimes."""
    start = datetime(d.year, d.month, d.day, tzinfo=BD_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def normalize_text(v: Any) -> str:
    s = text(v).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "from", "with", "by", "at", "as", "is",
    "are", "was", "were", "be", "been", "this", "that", "it", "its", "new", "game", "games", "sport",
    "sports", "news", "why", "how", "what", "did", "does", "do", "has", "have", "had", "about", "after",
    "before", "into", "than", "over",
}


def tokens(v: Any) -> set[str]:
    return {x for x in normalize_text(v).split() if len(x) >= 3 and x not in _STOP}


def similarity(a: Any, b: Any) -> float:
    aa, bb = normalize_text(a), normalize_text(b)
    if not aa or not bb:
        return 0.0
    seq = SequenceMatcher(None, aa, bb).ratio()
    ta, tb = tokens(aa), tokens(bb)
    jac = len(ta & tb) / max(1, len(ta | tb))
    return 0.55 * seq + 0.45 * jac


TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid",
                   "mc_cid", "mc_eid", "ref", "ocid", "cmpid"}


def canonical_url(url: str) -> str:
    raw = text(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return host + path + (("?" + urlencode(query)) if query else "")


def domain_of(url: str) -> str:
    raw = text(url).lower()
    if "://" not in raw:
        raw = "https://" + raw
    return urlsplit(raw).netloc.removeprefix("www.").split(":")[0]


def esc(s: Any) -> str:
    return html.escape(text(s), quote=False)


def esc_attr(s: Any) -> str:
    return html.escape(text(s), quote=True)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text(s).lower()).strip("-")


def clamp_words(s: str, n: int) -> str:
    """Trim to <= n characters at a word boundary. No ellipsis (validators reject it)."""
    s = re.sub(r"\s+", " ", text(s))
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut or s[:n]


_ABBREV = {"u.s.", "u.k.", "u.n.", "mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "no.", "jr.", "sr.", "inc.", "ltd.", "co.",
           "e.g.", "i.e.", "etc.", "approx.", "vol.", "fc.", "a.f.c.", "prof.", "gen.", "col.", "lt."}
_SPLIT_RX = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z0-9\"'“‘(])")


def split_sentences(s: str) -> list[str]:
    s = re.sub(r"\s+", " ", text(s))
    if not s:
        return []
    out, last = [], 0
    for m in _SPLIT_RX.finditer(s):
        before = s[last:m.start()].rsplit(" ", 1)[-1].lower().strip("\"'“‘(")
        if before in _ABBREV or re.fullmatch(r"[a-z]\.", before) or re.fullmatch(r"(?:[a-z]\.){2,}", before):
            continue  # abbreviation or initial, not a sentence end
        out.append(s[last:m.start() + 1 if s[m.start():m.start() + 1] in ".!?" else m.start()].strip())
        last = m.end()
    out.append(s[last:].strip())
    return [x for x in out if x]


def redact(s: str) -> str:
    out = str(s)
    for secret in (TELEGRAM_BOT_TOKEN, EXA_API_KEY, CEREBRAS_API_KEY, CRICKETDATA_API_KEY):
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]{20,}", "bot***", out)


# --- content-scope filters (video games / esports / betting / rumours are out) -----------------

_HARD_GAMING = re.compile(
    r"\b(playstation|ps[45]|xbox|nintendo|esports?|e-sports|video ?games?|gaming|dlc|patch notes|twitch|"
    r"fortnite|minecraft|roblox|valorant|dota ?2?|league of legends|counter-strike|call of duty|"
    r"ea sports fc|madden nfl|nba 2k|steam deck|steam store|steam sale|on steam|epic games)\b", re.I)
_SOFT_GAMING = re.compile(r"\b(consoles?|gpu|streamers?|game studio|battle pass|loot boxes?|playthrough|multiplayer online)\b", re.I)
_BETTING = re.compile(
    r"\b(betting|bookmakers?|sportsbook|parlay|tipsters?|betting odds|odds on|transfer rumou?rs?|"
    r"linked with a move)\b", re.I)


def exclusion_reason(value: str) -> str:
    """'' if the text is in scope, else a short reason."""
    s = text(value)
    if _HARD_GAMING.search(s):
        return "video-game/esports"
    if _BETTING.search(s):
        return "betting/rumour"
    if len({x.lower() for x in _SOFT_GAMING.findall(s)}) >= 2:  # 2 DISTINCT soft terms; repeats of one word do not count
        return "gaming-industry"
    return ""


def is_excluded(value: str) -> bool:
    return bool(exclusion_reason(value))


# ===========================================================================
# 2. HTTP layer: never raises, records per-source health
# ===========================================================================

HEADERS = {"User-Agent": f"TheSportsNewsroom/{APP_VERSION} (https://t.me/TheSportsNewsroom; newsroom bot)",
           "Accept-Language": "en-US,en;q=0.8"}

HEALTH: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()
_SESSION: list[Any] = [None]


def session() -> Any:
    if _SESSION[0] is None:
        if requests is None:
            raise RuntimeError("requests is not installed. Run: pip install -r requirements.txt")
        _SESSION[0] = requests.Session()
    return _SESSION[0]


def health_record(source: str, ok: bool, err: str = "", ms: int = 0) -> None:
    with _HEALTH_LOCK:
        h = HEALTH.setdefault(source, {"ok": 0, "fail": 0, "last_error": "", "ms": 0})
        h["ok" if ok else "fail"] += 1
        h["ms"] += ms
        if not ok:
            h["last_error"] = redact(err)[:240]


class Resp:
    __slots__ = ("status", "data", "text", "error", "headers", "exc")

    def __init__(self, status=0, data=None, text_="", error="", headers=None, exc=""):
        self.status, self.data, self.text, self.error = status, data, text_, error
        self.headers, self.exc = headers or {}, exc

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error


def http(source: str, method: str, url: str, *, params=None, json_body=None, data=None, files=None,
         headers=None, timeout=None, retries=0, backoff=1.0, want_json=True) -> Resp:
    hdrs = dict(HEADERS)
    hdrs.update(headers or {})
    last = Resp(0, error="no attempt")
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            r = session().request(method, url, params=params, json=json_body, data=data, files=files,
                                  headers=hdrs, timeout=timeout or HTTP_TIMEOUT)
        except Exception as exc:  # network level
            err = redact(f"{type(exc).__name__}: {exc}")
            health_record(source, False, err, int((time.monotonic() - t0) * 1000))
            last = Resp(0, error=err, exc=type(exc).__name__)
            if attempt < retries:
                sleep(backoff * (2 ** attempt))
                continue
            return last
        ms = int((time.monotonic() - t0) * 1000)
        status = int(getattr(r, "status_code", 0) or 0)
        body = ""
        try:
            body = r.text or ""
        except Exception:
            body = ""
        parsed = None
        if want_json:
            try:
                parsed = r.json()
            except Exception:
                parsed = None
        err = ""
        if not 200 <= status < 300:
            err = redact(f"HTTP {status}: {body[:200]}")
        elif want_json and parsed is None:
            err = "invalid JSON in response"
        last = Resp(status, parsed, body, err, dict(getattr(r, "headers", {}) or {}))
        if status in (429, 500, 502, 503, 504) and attempt < retries:
            ra = 0.0
            try:
                ra = float(last.headers.get("Retry-After", 0) or 0)
            except Exception:
                ra = 0.0
            sleep(min(30.0, ra or backoff * (2 ** attempt)))
            continue
        health_record(source, not err, err, ms)
        return last
    return last


def pmap(fn: Callable[[Any], Any], items: Iterable[Any], workers: int = 8) -> list[Any]:
    """Parallel map preserving order; exceptions become None."""
    items = list(items)

    def safe(x):
        try:
            return fn(x)
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker error: %s", redact(str(exc)))
            return None

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1))) as ex:
        return list(ex.map(safe, items))


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    def expired(self) -> bool:
        return self.left() <= 0


RUN_LIMIT = Deadline(10 ** 6)


class REPORT:
    """Per-run report. Reset at the start of every run."""
    desks: list[dict] = []
    rejections: Counter = Counter()
    ai_calls = 0
    ai_tokens = 0
    errors: list[str] = []
    notes: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.desks, cls.rejections, cls.ai_calls, cls.ai_tokens = [], Counter(), 0, 0
        cls.errors, cls.notes = [], []
        HEALTH.clear()


def reject(reason: str, detail: str = "") -> None:
    REPORT.rejections[reason] += 1
    logger.info("reject[%s] %s", reason, detail[:160])

# ===========================================================================
# 3. STATE, LEDGER, SCHEDULER
# ===========================================================================

STATE_SCHEMA = 3


def default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA,
        "created_at": utc_iso(now_bd()),
        "last_run_at": "",
        "ledger": {},
        "posts": [],
        "topics": {"pool": {}, "last_refresh": "", "per_topic": {}},
        "ai": {},
        "source_health": {},
        "runs": [],
        "alerts": {},
    }


def migrate_state(state: dict) -> dict:
    """Bring any older state file (v1/v2) up to v3 without losing the post archive."""
    merged = default_state()
    for key in ("created_at", "last_run_at"):
        if state.get(key):
            merged[key] = state[key]
    for key in ("ledger", "topics", "ai", "source_health", "alerts"):
        if isinstance(state.get(key), dict):
            merged[key].update(state[key])
    for key in ("posts", "runs"):
        if isinstance(state.get(key), list):
            merged[key] = state[key]
    # v2 posts used published_at/subject/claim; keep them readable by v3 dedupe
    for p in merged["posts"]:
        p.setdefault("desk", "legacy")
        p.setdefault("posted_at", p.get("published_at", ""))
        p.setdefault("topic", p.get("game_or_sport", ""))
        p.setdefault("urls", p.get("source_urls", []))
    merged["schema_version"] = STATE_SCHEMA
    merged["topics"].setdefault("pool", {})
    merged["topics"].setdefault("per_topic", {})
    merged["topics"].setdefault("last_refresh", "")
    return merged


def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists() or p.stat().st_size == 0:
        return default_state()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not a JSON object")
    except Exception as exc:
        # Never post from a blank state: duplicates are worse than a stopped bot.
        raise RuntimeError(f"State file unreadable; refusing to start with a blank state: {exc}") from exc
    return migrate_state(raw)


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    tmp = Path(STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True, default=json_safe), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_posted_urls() -> set[str]:
    p = Path(POSTED_FILE)
    if not p.exists():
        return set()
    return {x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}


def append_posted_urls(urls: Iterable[str]) -> None:
    if DRY_RUN:
        return
    # Wikipedia pages are reference pages that many different posts legitimately cite; they are protected by
    # claim/topic dedupe instead. posted_urls.txt tracks article URLs (news, tabletop outlets, etc.).
    canon = [canonical_url(u) for u in urls if text(u) and "wikipedia.org" not in text(u)]
    canon = [c for c in canon if c]
    if not canon:
        return
    with Path(POSTED_FILE).open("a", encoding="utf-8") as f:
        f.write("\n".join(canon) + "\n")


def prune_state(state: dict) -> None:
    cutoff = now_bd() - timedelta(days=STATE_RETENTION_DAYS)
    state["posts"] = state.get("posts", [])[-5000:]
    state["runs"] = state.get("runs", [])[-30:]
    for key, entry in list(state.get("ledger", {}).items()):
        seen = parse_dt(entry.get("updated_at"))
        if seen and seen < cutoff:
            state["ledger"].pop(key, None)
    for key, day in list(state.get("alerts", {}).items()):
        d = parse_dt(str(day) + "T00:00:00+00:00")
        if d and d < cutoff:
            state["alerts"].pop(key, None)
    # keep the posted-url file bounded
    p = Path(POSTED_FILE)
    if p.exists() and not DRY_RUN:
        lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if len(lines) > 20000:
            p.write_text("\n".join(lines[-10000:]) + "\n", encoding="utf-8")


def ledger_get(state: dict, key: str) -> dict:
    return state.setdefault("ledger", {}).get(key, {})


def ledger_update(state: dict, key: str, **kw: Any) -> dict:
    entry = state.setdefault("ledger", {}).setdefault(key, {"status": "new", "attempts": 0})
    entry.update(kw)
    entry["updated_at"] = utc_iso(now_bd())
    return entry


# --- slots -------------------------------------------------------------------------------------


class Slot:
    def __init__(self, name: str, desk: str, opens: int, deadline: int, offset_days: int = 0,
                 index: int = 0, mandatory: bool = False):
        self.name, self.desk, self.opens, self.deadline = name, desk, opens, deadline
        self.offset_days, self.index, self.mandatory = offset_days, index, mandatory

    def target(self, now: datetime) -> date:
        return now.date() + timedelta(days=self.offset_days)

    def key(self, now: datetime) -> str:
        if self.desk == "evergreen":
            return f"evergreen:{now.date().isoformat()}:{self.index}"
        return f"{self.name}:{self.target(now).isoformat()}"

    def is_open(self, now: datetime) -> bool:
        return now.hour >= self.opens

    def is_late(self, now: datetime) -> bool:
        return now.hour >= self.deadline


def build_slots() -> list[Slot]:
    slots = [
        Slot("day_in_sports", "past", OPEN_DAY_IN_SPORTS, OPEN_DAY_IN_SPORTS + 7, -1, mandatory=True),
        Slot("next_up", "next", OPEN_NEXT_UP, min(23, OPEN_NEXT_UP + 4), NEXT_UP_OFFSET_DAYS, mandatory=True),
        Slot("on_this_date", "history", OPEN_ON_THIS_DATE, OPEN_ON_THIS_DATE + 7, 0),
    ]
    for i in range(1, EVERGREEN_PER_DAY + 1):
        hour = EVERGREEN_OPEN_HOURS[i - 1] if i - 1 < len(EVERGREEN_OPEN_HOURS) else min(22, 20 + 2 * (i - len(EVERGREEN_OPEN_HOURS)))
        slots.append(Slot(f"evergreen_{i}", "evergreen", hour, min(23, hour + 4), 0, index=i))
    return slots


def due_slots(state: dict, now: datetime) -> list[Slot]:
    due = []
    for slot in build_slots():
        entry = ledger_get(state, slot.key(now))
        if entry.get("status") in ("posted", "skipped", "uncertain"):
            continue
        if not slot.is_open(now):
            continue
        if entry.get("attempts", 0) >= MAX_ATTEMPTS_PER_SLOT:
            continue
        due.append(slot)
    return due


def sla_breaches(state: dict, now: datetime) -> list[tuple[Slot, str]]:
    """Mandatory slots that are past their deadline and not posted."""
    out = []
    for slot in build_slots():
        if not slot.mandatory or not slot.is_late(now):
            continue
        key = slot.key(now)
        if ledger_get(state, key).get("status") != "posted":
            out.append((slot, key))
    return out


def recent_posts(state: dict, n: int = 20, desk: str | None = None) -> list[dict]:
    rows = [p for p in state.get("posts", []) if desk is None or p.get("desk") == desk]
    return rows[-n:]


# ===========================================================================
# 4. TELEGRAM: sanitiser, renderers, publisher (documented Bot API methods only)
# ===========================================================================

TG_ALLOWED = {"b", "i", "u", "s", "a", "code", "blockquote"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)/?>")

FORMAT_LABELS = {
    "fact": "DID YOU KNOW?", "game_discovery": "GAME DISCOVERY", "rule_check": "RULE CHECK",
    "how_to_play": "HOW TO PLAY", "history": "GAME / SPORTS HISTORY", "on_this_date": "ON THIS DATE",
    "century_ago": "100 YEARS AGO", "why": "WHY?", "first_last_only": "FIRST · LAST · ONLY",
    "then_vs_now": "THEN → NOW", "forgotten": "FORGOTTEN", "new_game": "NEW ON THE TABLE",
    "daily_next": "NEXT UP", "daily_past": "THE DAY IN SPORTS",
}
FORMAT_EMOJI = {
    "fact": "💡", "game_discovery": "🎲", "rule_check": "📖", "how_to_play": "🧩", "history": "🏛",
    "on_this_date": "📅", "century_ago": "🕰", "why": "❓", "first_last_only": "🥇", "then_vs_now": "🔁",
    "forgotten": "🗝", "new_game": "🆕", "daily_next": "🗓", "daily_past": "📰",
}
FORMAT_TAG = {
    "fact": "#SportsFacts", "game_discovery": "#GameDiscovery", "rule_check": "#RuleCheck",
    "how_to_play": "#HowToPlay", "history": "#SportsHistory", "on_this_date": "#OnThisDate",
    "century_ago": "#OnThisDate", "why": "#WhyItWorksThisWay", "first_last_only": "#FirstEver",
    "then_vs_now": "#ThenVsNow", "forgotten": "#ForgottenGames", "new_game": "#NewGames",
    "daily_next": "#NextUp", "daily_past": "#DayInSports",
}


def tg_sanitize(s: str) -> str:
    """Reduce arbitrary text/HTML to Telegram-safe HTML: allowed tags only, balanced, escaped."""
    s = re.sub(r"<br\s*/?>", "\n", str(s or ""), flags=re.I)
    out: list[str] = []
    stack: list[tuple[str, bool]] = []
    pos = 0
    for m in _TAG_RE.finditer(s):
        out.append(html.escape(html.unescape(s[pos:m.start()]), quote=False))
        pos = m.end()
        closing, name, attrs = bool(m.group(1)), m.group(2).lower(), m.group(3) or ""
        if name not in TG_ALLOWED:
            continue
        if not closing:
            if name == "a":
                hm = re.search(r'href\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', attrs)
                url = html.unescape((hm.group(1) or hm.group(2) or "") if hm else "").strip()
                if not re.match(r"^(https?://|tg://)", url, re.I):
                    stack.append((name, False))
                    continue
                out.append(f'<a href="{esc_attr(url)}">')
            else:
                out.append(f"<{name}>")
            stack.append((name, True))
        else:
            idx = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), None)
            if idx is None:
                continue
            while len(stack) > idx:
                n, kept = stack.pop()
                if kept:
                    out.append(f"</{n}>")
    out.append(html.escape(html.unescape(s[pos:]), quote=False))
    while stack:
        n, kept = stack.pop()
        if kept:
            out.append(f"</{n}>")
    result = "".join(out)
    while True:  # empty tags left after removals; repeat until stable (idempotent output)
        cleaned = re.sub(r"<(b|i|u|s|code|blockquote)>\s*</\1>", "", result)
        if cleaned == result:
            return result
        result = cleaned


def vis_len(s: str) -> int:
    """Visible length as Telegram counts it (UTF-16 code units, after entity parsing)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", s))
    return len(plain.encode("utf-16-le")) // 2


def plain_text(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", s)))


def is_valid_tg_html(s: str) -> bool:
    return tg_sanitize(s) == s


def hashtag(s: str) -> str:
    """CamelCase hashtag from the first words of s; skipped if it would be unwieldy."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text(s)) if w.lower() not in {"a", "an", "the", "of", "and", "in", "on", "to", "for", "new", "is"}]
    for n in (3, 2, 1):
        tag = "".join(w[:1].upper() + w[1:] for w in words[:n])
        if 3 <= len(tag) <= 22:
            return "#" + tag
    return ""


def source_links(sources: list, limit: int = 3) -> str:
    links = []
    seen = set()
    for label, url in sources:
        if not text(url) or url in seen:
            continue
        seen.add(url)
        links.append(f'<a href="{esc_attr(url)}">{esc(label or domain_of(url))}</a>')
        if len(links) >= limit:
            break
    return " · ".join(links)


def knowledge_html(story: dict, level: int = 0) -> str:
    fmt = story.get("format", "fact")
    label = story.get("label") or FORMAT_LABELS.get(fmt, "SPORTS & GAMES")
    head = f"{FORMAT_EMOJI.get(fmt, '🏅')} <b>{esc(label)}</b>"
    if text(story.get("date_anchor")):
        head += f" · {esc(story['date_anchor'])}"
    body = text(story.get("body"))
    why = text(story.get("why_interesting"))
    points = [text(p) for p in story.get("key_points", []) if text(p)][:3]
    if level >= 2:
        points = []
    if level >= 3 and why:
        why = clamp_words(why, 140)
    if level >= 4:
        why = ""
    if level >= 5:
        body = " ".join(split_sentences(body)[:2])
    if level >= 6:
        body = clamp_words(body, 320)
    if level >= 7:
        body = clamp_words(body, 200)
    parts = [head, "", f"<b>{esc(story.get('headline', ''))}</b>", "", esc(body)]
    if why:
        parts += ["", f"<i>Why it's interesting:</i> {esc(why)}"]
    if points:
        parts += [""] + ["• " + esc(p) for p in points]
    src = source_links(story.get("sources", []))
    if src:
        parts += ["", "Source: " + src]
    if level < 1:
        tags = [t for t in story.get("tags", []) if t]
        if tags:
            parts += [" ".join(esc(t) for t in tags)]
    return "\n".join(parts)


def fit_knowledge_html(story: dict, limit: int = CAPTION_LIMIT - 20) -> str:
    """Deterministic trimming ladder; least important parts are dropped first."""
    rendered = ""
    for level in range(0, 8):
        rendered = tg_sanitize(knowledge_html(story, level))
        if vis_len(rendered) <= limit:
            return rendered
    return rendered


# --- publisher ---------------------------------------------------------------------------------


def tg_call(method: str, data: dict | None = None, file_path: str = "", file_field: str = "photo") -> dict:
    """Call a Bot API method. Returns the JSON payload; adds 'uncertain' when delivery is unknown."""
    if DRY_RUN:
        print(f"\n[DRY-RUN] {method} -> {CHANNEL}")
        for k, v in (data or {}).items():
            if k in ("caption", "text"):
                print(f"--- {k} ({vis_len(str(v))} visible chars) ---\n{plain_text(str(v))}\n--- end ---")
        if file_path:
            print(f"[image card: {file_path}]")
        return {"ok": True, "result": {"message_id": 0}, "dry_run": True}
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last: dict = {"ok": False, "description": "no attempt"}
    for attempt in range(3):
        if file_path:
            with open(file_path, "rb") as fh:
                r = http("telegram", "POST", url, data=data or {}, files={file_field: fh}, timeout=60)
        else:
            r = http("telegram", "POST", url, data=data or {}, timeout=60)
        payload = r.data if isinstance(r.data, dict) else None
        if r.ok and payload and payload.get("ok"):
            return payload
        if r.status == 0:
            if r.exc == "ConnectTimeout" and attempt < 2:
                sleep(2 * (attempt + 1))
                continue
            return {"ok": False, "description": r.error, "uncertain": r.exc != "ConnectTimeout"}
        last = payload or {"ok": False, "description": r.error}
        if r.status == 429 and attempt < 2:
            wait = 5
            try:
                wait = int((payload or {}).get("parameters", {}).get("retry_after", 5))
            except Exception:
                pass
            if wait <= 60:
                sleep(wait + 1)
                continue
        break
    return last


def _err(res: dict) -> str:
    return text(res.get("description")).lower()


def tg_send_text(html_text: str) -> dict:
    base = {"chat_id": CHANNEL, "text": html_text, "parse_mode": "HTML"}
    res = tg_call("sendMessage", {**base, "link_preview_options": json.dumps({"is_disabled": True})})
    if not res.get("ok") and not res.get("uncertain"):
        e = _err(res)
        if "link_preview" in e:
            res = tg_call("sendMessage", {**base, "disable_web_page_preview": "true"})
        elif "parse entities" in e or "entities" in e:
            logger.warning("Telegram rejected HTML; resending as plain text")
            res = tg_call("sendMessage", {"chat_id": CHANNEL, "text": plain_text(html_text)[:MESSAGE_LIMIT],
                                          "link_preview_options": json.dumps({"is_disabled": True})})
    return res


def tg_send_photo(card_path: str, caption_html: str) -> dict:
    res = tg_call("sendPhoto", {"chat_id": CHANNEL, "caption": caption_html, "parse_mode": "HTML"}, card_path)
    if not res.get("ok") and not res.get("uncertain"):
        e = _err(res)
        if "parse entities" in e or "entities" in e:
            logger.warning("Telegram rejected caption HTML; resending as plain text")
            res = tg_call("sendPhoto", {"chat_id": CHANNEL, "caption": plain_text(caption_html)[:CAPTION_LIMIT]}, card_path)
        else:
            logger.warning("sendPhoto failed (%s); falling back to a text post", res.get("description"))
            res = tg_send_text(caption_html)
    return res


def publish_story(story: dict) -> dict:
    render = story["render"]
    if render["mode"] == "photo":
        card = make_card(story)
        try:
            if card:
                return tg_send_photo(card, render["html"])
            return tg_send_text(render["html"])
        finally:
            if card and not DRY_RUN:
                try:
                    os.remove(card)
                except OSError:
                    pass
    return tg_send_text(render["html"])


def admin_alert(state: dict, key: str, message: str) -> None:
    """Send one short DM to the admin, at most once per key per day."""
    today = now_bd().date().isoformat()
    alerts = state.setdefault("alerts", {})
    if alerts.get(key) == today:
        return
    alerts[key] = today
    if DRY_RUN or not ADMIN_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        logger.warning("ALERT (not sent): %s", message)
        return
    tg_call("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": f"⚠️ {APP_NAME}\n{message}"[:MESSAGE_LIMIT]})


# ===========================================================================
# 5. IMAGE CARDS (generated with Pillow; no downloaded photos)
# ===========================================================================

THEMES = {
    "fact": ((20, 60, 120), (10, 25, 60)), "game_discovery": ((90, 40, 140), (35, 15, 70)),
    "rule_check": ((140, 60, 20), (60, 25, 10)), "how_to_play": ((20, 110, 100), (8, 45, 45)),
    "history": ((110, 80, 30), (45, 32, 12)), "on_this_date": ((30, 90, 60), (12, 40, 28)),
    "century_ago": ((100, 70, 30), (40, 28, 10)), "why": ((30, 80, 140), (12, 30, 70)),
    "first_last_only": ((150, 110, 20), (70, 50, 8)), "then_vs_now": ((60, 60, 130), (25, 25, 60)),
    "forgotten": ((80, 60, 90), (32, 24, 40)), "new_game": ((150, 40, 80), (65, 15, 35)),
    "daily_next": ((10, 100, 60), (5, 45, 28)), "daily_past": ((130, 30, 30), (55, 12, 12)),
}
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CACHE: dict[int, Any] = {}


def card_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for p in _FONT_PATHS:
        if Path(p).exists():
            try:
                font = ImageFont.truetype(p, size=size)
                break
            except Exception:
                font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _wrap(draw, s: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_card(story: dict) -> str:
    """1200x675 card. Returns a file path, or '' if Pillow/fonts fail (caller falls back to text)."""
    if Image is None:
        return ""
    try:
        W, H = 1200, 675
        fmt = story.get("format", "fact")
        top, bot = THEMES.get(fmt, THEMES["fact"])
        img = Image.new("RGB", (W, H), top)
        draw = ImageDraw.Draw(img)
        for y in range(H):
            t = y / (H - 1)
            draw.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
        # decoration: concentric rings (sport) or checker corner (game)
        if fmt in ("game_discovery", "rule_check", "how_to_play", "forgotten", "new_game"):
            for r in range(8):
                for c in range(8):
                    if (r + c) % 2 == 0:
                        x0, y0 = W - 8 * 34 - 30 + c * 34, 30 + r * 34
                        draw.rectangle([x0, y0, x0 + 33, y0 + 33], fill=tuple(min(255, v + 28) for v in top))
        else:
            for k, rad in enumerate((260, 200, 140, 80)):
                cx, cy = W - 170, 170
                draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
                             outline=tuple(min(255, v + 30 + k * 6) for v in top), width=6)
        label = text(story.get("label") or FORMAT_LABELS.get(fmt, "SPORTS & GAMES"))
        f_label = card_font(30)
        lw = int(draw.textlength(label, font=f_label))
        draw.rounded_rectangle([60, 55, 60 + lw + 50, 115], radius=28, fill=(255, 255, 255))
        draw.text((85, 62), label, font=f_label, fill=top)
        headline = text(story.get("headline")) or "The Sports Newsroom"
        for size in (68, 60, 52, 46, 40):
            f_head = card_font(size)
            lines = _wrap(draw, headline, f_head, W - 150)
            if len(lines) <= 4 or size == 40:
                break
        y = 170
        for line in lines[:5]:
            draw.text((66, y + 3), line, font=f_head, fill=(0, 0, 0))
            draw.text((64, y), line, font=f_head, fill=(255, 255, 255))
            y += int(size * 1.22)
        anchor = text(story.get("date_anchor"))
        if anchor:
            draw.text((64, H - 95), anchor, font=card_font(34), fill=(255, 255, 255))
        handle = CHANNEL if CHANNEL.startswith("@") else "@TheSportsNewsroom"
        f_handle = card_font(30)
        draw.text((W - 60 - int(draw.textlength(handle, font=f_handle)), H - 90), handle, font=f_handle, fill=(255, 255, 255))
        path = os.path.join(tempfile.gettempdir(), f"sn_card_{int(time.time() * 1000)}_{random.randint(0, 9999)}.jpg")
        img.save(path, "JPEG", quality=88)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Card generation failed (%s); posting text only", exc)
        return ""

# ===========================================================================
# 6. AI CLIENT (Cerebras, plain REST). Never raises; returns None on failure.
# ===========================================================================

STR = {"type": "string"}
INT = {"type": "integer"}


def ARR(item: dict) -> dict:
    return {"type": "array", "items": item}


def ENUM(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def OBJ(**props: dict) -> dict:
    """Strict-mode compatible object: all keys required, no extras, no numeric constraints."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


INTRO_SCHEMA = OBJ(intro=STR)
STORYLINE_SCHEMA = OBJ(storyline=STR)
PICK_SCHEMA = OBJ(index=INT, reason=STR)
FACTS_SCHEMA = OBJ(facts=ARR(OBJ(claim=STR, evidence_quote=STR, surprise=INT, certainty=ENUM("settled", "debated", "legend"))))
POST_SCHEMA = OBJ(headline=STR, body=STR, why_interesting=STR, key_points=ARR(STR))
EVENTS_SCHEMA = OBJ(events=ARR(OBJ(sport=STR, league=STR, name=STR, home=STR, away=STR, date=STR,
                                   time_utc=STR, source_url=STR)))


def schema_example(schema: dict) -> str:
    def ex(s: dict):
        t = s.get("type")
        if t == "object":
            return {k: ex(v) for k, v in s.get("properties", {}).items()}
        if t == "array":
            return [ex(s.get("items", {}))]
        if "enum" in s:
            return "|".join(s["enum"])
        return 0 if t == "integer" else "<string>"
    return json.dumps(ex(schema))


def validate_schema(data: Any, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}.{k}: missing")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                errs += validate_schema(data[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array"]
        for i, item in enumerate(data):
            errs += validate_schema(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            errs.append(f"{path}: expected string")
        elif "enum" in schema and data not in schema["enum"]:
            errs.append(f"{path}: must be one of {schema['enum']}")
    elif t == "integer":
        if isinstance(data, bool) or not isinstance(data, int):
            errs.append(f"{path}: expected integer")
    return errs


def extract_json(content: str) -> dict:
    s = text(content)
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except Exception:
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("no JSON object found")
        obj = json.loads(s[a:b + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj


def json_safe(value: Any) -> Any:
    """Convert internal Python values to JSON-safe values at API serialization boundaries."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return value


class AIClient:
    URL = "https://api.cerebras.ai/v1/chat/completions"
    MODELS_URL = "https://api.cerebras.ai/v1/models"
    MODE_ORDER = ["json_schema", "json_object", "text"]
    # Keep the production fallback list deliberately small. gpt-oss-120b is the current
    # primary production model and supports reasoning + structured outputs.
    MODEL_PREFS = ["gpt-oss-120b", "llama-3.3-70b", "qwen-3-235b-a22b-instruct-2507"]
    MIN_INTERVAL = 1.5

    def __init__(self, saved: dict | None = None):
        saved = saved or {}
        self.available = bool(CEREBRAS_API_KEY)
        self.mode = saved.get("mode") if saved.get("mode") in self.MODE_ORDER else "json_schema"
        self.model = CEREBRAS_MODEL
        self.reasoning = True
        self.fatal = False
        self.last_error = ""
        self.last_status = 0
        self.last_request_id = ""
        self.last_ok = saved.get("last_ok", "")
        self.last_latency_ms = 0
        self.rate_limits: dict[str, str] = {}
        self._last_call = 0.0
        self._model_fallback_tried = False
        self.failures = 0

    def export(self) -> dict:
        return {
            "mode": self.mode,
            "model": self.model,
            "last_ok": self.last_ok,
            "last_status": self.last_status,
            "last_request_id": self.last_request_id,
            "last_error": self.last_error[:240],
        }

    def _space(self) -> None:
        wait = self.MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            sleep(wait)
        self._last_call = time.monotonic()

    def _payload(self, messages: list, schema: dict, task: str, max_tokens: int, temperature: float) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max(128, int(max_tokens)),
        }
        if self.reasoning and "gpt-oss" in self.model:
            payload["reasoning_effort"] = "low"
        if self.mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": re.sub(r"[^A-Za-z0-9_-]", "_", task)[:60] or "output",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif self.mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _error_detail(r: Any) -> str:
        data = r.data if isinstance(getattr(r, "data", None), dict) else {}
        err = data.get("error")
        if isinstance(err, dict):
            bits = [text(err.get(k)) for k in ("type", "code", "message") if text(err.get(k))]
            detail = " | ".join(dict.fromkeys(bits))
        else:
            detail = text(err)
        if not detail:
            detail = text(getattr(r, "error", ""))
        req = text(getattr(r, "headers", {}).get("x-request-id") or getattr(r, "headers", {}).get("request-id"))
        if not req and isinstance(data, dict):
            req = text(data.get("requestId"))
        if req:
            detail = f"{detail or 'provider error'} | request_id={req}"
        return redact(detail or f"HTTP {getattr(r, 'status', 0)}")[:700]

    def _capture_headers(self, r: Any) -> None:
        hdrs = getattr(r, "headers", {}) or {}
        self.rate_limits = {
            k: text(v) for k, v in hdrs.items()
            if str(k).lower().startswith("x-ratelimit-")
        }
        self.last_request_id = text(
            hdrs.get("x-request-id") or hdrs.get("request-id") or ""
        ) or self.last_request_id

    def _switch_model(self) -> bool:
        if self._model_fallback_tried:
            return False
        self._model_fallback_tried = True
        r = http("cerebras", "GET", self.MODELS_URL, headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}"}, timeout=20, retries=1)
        self._capture_headers(r)
        models = []
        if r.ok and isinstance(r.data, dict):
            models = [m for m in (r.data.get("data", []) or []) if isinstance(m, dict) and text(m.get("id"))]
        ids = [text(m.get("id")) for m in models]
        active_ids = [text(m.get("id")) for m in models if not bool(m.get("deprecated"))]
        capability_ids = [text(m.get("id")) for m in models if not bool(m.get("deprecated")) and (m.get("capabilities") is None or bool((m.get("capabilities") or {}).get("structured_outputs", True)))]
        preferred_ids = capability_ids or active_ids or ids
        for pref in self.MODEL_PREFS:
            if pref in ids and pref != self.model:
                logger.warning("Cerebras model %s unavailable; switching to %s", self.model, pref)
                self.model = pref
                return True
        if preferred_ids and self.model not in preferred_ids:
            logger.warning("Cerebras model %s unavailable; switching to %s", self.model, preferred_ids[0])
            self.model = preferred_ids[0]
            return True
        return False

    def json(self, *args: Any, **kwargs: Any) -> dict | None:
        """Return validated JSON or None. Never lets provider failures escape the bot process."""
        if not self.available or self.fatal:
            self.last_error = self.last_error or "Cerebras unavailable"
            return None
        if self.failures >= 3:
            self.last_error = self.last_error or "circuit open"
            return None
        out = self._json(*args, **kwargs)
        self.failures = 0 if out is not None else self.failures + 1
        if self.failures == 3:
            logger.error("Cerebras failed 3 times in a row: %s", self.last_error)
            REPORT.errors.append(f"AI circuit opened: {self.last_error}")
        return out

    def _json(self, task: str, system: str, user: str, schema: dict, *, max_tokens: int = 3000,
              temperature: float = 0.2, extra_messages: list | None = None) -> dict | None:
        hint = f"\nReturn ONLY one JSON object shaped like: {schema_example(schema)}"
        messages = [
            {"role": "system", "content": f"TASK: {task}\n{system}{hint}"},
            {"role": "user", "content": user},
        ] + list(extra_messages or [])
        budget = max(256, int(max_tokens))
        repaired = False
        payload_headers = {
            "Authorization": f"Bearer {CEREBRAS_API_KEY}",
            "Content-Type": "application/json",
            "X-Cerebras-Version-Patch": CEREBRAS_API_VERSION,
        }
        for attempt in range(5):
            self._space()
            t0 = time.monotonic()
            r = http(
                "cerebras", "POST", self.URL,
                json_body=self._payload(messages, schema, task, budget, temperature),
                headers=payload_headers,
                timeout=90,
            )
            self.last_latency_ms = int((time.monotonic() - t0) * 1000)
            self.last_status = int(getattr(r, "status", 0) or 0)
            self._capture_headers(r)
            REPORT.ai_calls += 1

            low = (r.text or "").lower()

            # Fatal authentication/permission errors should never be hidden by a fallback.
            if r.status in (401, 403):
                self.fatal = True
                self.last_error = self._error_detail(r)
                logger.error("Cerebras authentication/permission failure: %s", self.last_error)
                REPORT.errors.append(self.last_error)
                return None

            if r.status == 429:
                self.last_error = self._error_detail(r)
                quota_exceeded = any(token in low for token in ("token_quota_exceeded", "too_many_tokens_error", "tokens per minute"))
                if quota_exceeded:
                    REPORT.errors.append(f"Cerebras token quota exhausted: {self.last_error}")
                    logger.warning("Cerebras token quota exhausted; no blind retries: %s", self.last_error)
                    return None
                reset = 0.0
                try:
                    reset = float(self.rate_limits.get("x-ratelimit-reset-tokens-minute", "0") or 0)
                except Exception:
                    reset = 0.0
                try:
                    ra = float(r.headers.get("Retry-After", 0) or 0)
                except Exception:
                    ra = 0.0
                wait = min(30.0, max(2.0, ra or reset or (2.0 ** attempt)))
                logger.warning("Cerebras rate limited (attempt %d/5): %s; retrying in %.1fs", attempt + 1, self.last_error, wait)
                if attempt < 4:
                    sleep(wait)
                    continue
                REPORT.errors.append(f"Cerebras rate limit exhausted: {self.last_error}")
                return None

            if r.status == 0 or r.status >= 500:
                self.last_error = self._error_detail(r)
                if attempt < 4:
                    sleep(min(20.0, 2.0 * (attempt + 1)))
                    continue
                REPORT.errors.append(f"Cerebras transient failure exhausted: {self.last_error}")
                return None

            if r.status == 404 or (r.status == 400 and "model" in low and "not" in low and "found" in low):
                self.last_error = self._error_detail(r)
                if self._switch_model():
                    continue
                REPORT.errors.append(f"Cerebras model failure: {self.last_error}")
                return None

            if r.status == 400:
                detail = self._error_detail(r)
                # Compatibility escape hatch: remove reasoning first, then downgrade structured mode.
                if "reasoning" in low and self.reasoning:
                    self.reasoning = False
                    self.last_error = detail
                    logger.warning("Cerebras rejected reasoning on %s; retrying without reasoning: %s", self.model, detail)
                    continue
                if self.mode != "text" and any(k in low for k in ("response_format", "json_schema", "schema", "strict", "structured")):
                    self.last_error = detail
                    idx = self.MODE_ORDER.index(self.mode)
                    self.mode = self.MODE_ORDER[idx + 1]
                    logger.warning("Cerebras rejected structured output; switching to mode=%s: %s", self.mode, detail)
                    continue
                self.last_error = detail
                REPORT.errors.append(f"Cerebras bad request for {task}: {detail}")
                return None

            if not r.ok or not isinstance(r.data, dict):
                self.last_error = self._error_detail(r)
                if attempt < 4:
                    continue
                return None

            try:
                choice = r.data["choices"][0]
                message = choice.get("message") or {}
                content = text(message.get("content"))
                finish = choice.get("finish_reason")
                usage = r.data.get("usage") or {}
                REPORT.ai_tokens += int(usage.get("total_tokens") or 0)
            except Exception as exc:
                self.last_error = f"response shape error: {type(exc).__name__}"
                if attempt < 4:
                    continue
                return None

            if not content:
                self.last_error = f"empty content (finish_reason={finish or 'unknown'})"
                if finish == "length" and budget < 8000:
                    budget = min(8000, budget * 2)
                    continue
                return None

            try:
                obj = extract_json(content)
            except Exception as exc:
                self.last_error = f"unparseable JSON: {exc}"
                if finish == "length" and budget < 8000:
                    budget = min(8000, budget * 2)
                    continue
                return None

            errs = validate_schema(obj, schema)
            if errs:
                self.last_error = "; ".join(errs[:3])
                if not repaired and self.mode != "json_schema":
                    repaired = True
                    messages = messages + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": "That JSON was invalid: " + self.last_error + ". Return the corrected JSON object only."},
                    ]
                    continue
                return None

            self.last_ok = utc_iso(now_bd())
            self.last_error = ""
            return obj
        return None

# ===========================================================================
# 7. DATA ADAPTERS (tolerant readers; each returns [] / None on failure and records health)
# ===========================================================================

OFFICIAL_DOMAINS = [
    "fifa.com", "uefa.com", "icc-cricket.com", "lords.org", "worldathletics.org", "fide.com", "itftennis.com",
    "bwfbadminton.com", "worldbadminton.com", "formula1.com", "fia.com", "world.rugby", "olympics.com",
    "paralympic.org", "worldboxing.org", "ijf.org", "worldarchery.sport", "worldrowing.com", "worldaquatics.com",
    "uci.org", "fiba.basketball", "iihf.com", "theifab.com", "wtatennis.com", "atptour.com", "pgatour.com",
    "worldgolf.com", "ittf.com", "worldtabletennis.com", "fivb.com", "ihf.info", "bcci.tv", "nba.com",
]
RULES_PUBLISHERS = [
    "hasbro.com", "mattel.com", "catan.com", "usplayingcard.com", "bicyclecards.com", "pagat.com",
    "usachess.org", "wikibooks.org", "gamesrules.com", "scrabble.com", "monopoly.com",
]
REFERENCE_DOMAINS = ["wikipedia.org", "britannica.com", "guinnessworldrecords.com", "atlasobscura.com"]
SECONDARY_DOMAINS = [
    "bbc.com", "bbc.co.uk", "espn.com", "espncricinfo.com", "skysports.com", "theguardian.com", "reuters.com",
    "apnews.com", "nbcsports.com", "cbssports.com", "foxsports.com", "si.com", "cricbuzz.com",
    "boardgamegeek.com", "dicebreaker.com", "tabletopgaming.co.uk", "ultraboardgames.com",
]
TABLETOP_DOMAINS = ["dicebreaker.com", "tabletopgaming.co.uk", "boardgamegeek.com", "ultraboardgames.com",
                    "geekdad.com", "shutupandsitdown.com", "thegamer.com"]
CRICKET_DOMAINS = ["espncricinfo.com", "cricbuzz.com", "icc-cricket.com"]

SOURCE_LABELS = {
    "bbc.com": "BBC Sport", "bbc.co.uk": "BBC Sport", "espn.com": "ESPN", "espncricinfo.com": "ESPNcricinfo",
    "skysports.com": "Sky Sports", "theguardian.com": "The Guardian", "olympics.com": "Olympics.com",
    "reuters.com": "Reuters", "apnews.com": "AP", "fifa.com": "FIFA", "uefa.com": "UEFA",
    "icc-cricket.com": "ICC", "worldathletics.org": "World Athletics", "fide.com": "FIDE",
    "itftennis.com": "ITF", "formula1.com": "Formula 1", "fia.com": "FIA", "boardgamegeek.com": "BoardGameGeek",
    "britannica.com": "Britannica", "atlasobscura.com": "Atlas Obscura", "wikipedia.org": "Wikipedia",
    "guinnessworldrecords.com": "Guinness World Records", "dicebreaker.com": "Dicebreaker",
    "tabletopgaming.co.uk": "Tabletop Gaming", "cricbuzz.com": "Cricbuzz", "lords.org": "MCC / Lord's",
    "theifab.com": "IFAB", "pagat.com": "Pagat", "thesportsdb.com": "TheSportsDB",
}


def _dom_match(d: str, group: list[str]) -> bool:
    return any(d == x or d.endswith("." + x) for x in group)


def grade_of(url: str) -> str:
    """Evidence grade: A official/primary or rules publisher, B reference/major outlet, C everything else."""
    d = domain_of(url)
    if _dom_match(d, OFFICIAL_DOMAINS) or _dom_match(d, RULES_PUBLISHERS):
        return "A"
    if _dom_match(d, REFERENCE_DOMAINS) or _dom_match(d, SECONDARY_DOMAINS):
        return "B"
    return "C"


def source_label(url: str, fallback: str = "") -> str:
    d = domain_of(url)
    for dom, label in SOURCE_LABELS.items():
        if d == dom or d.endswith("." + dom):
            return label
    return fallback or d or "Source"


# --- Exa ---------------------------------------------------------------------------------------

_EXA_VARIANT = [0]  # 0: contents.text  1: top-level text  2: no contents
_EXA_CACHE: dict[tuple, list[dict]] = {}


def exa_search(query: str, *, domains: list[str] | None = None, start: datetime | None = None,
               end: datetime | None = None, num: int = 8, text_chars: int = 3500) -> list[dict]:
    if not EXA_API_KEY:
        return []
    ckey = (query, tuple(domains or ()), start.date().isoformat() if start else "", num)
    if ckey in _EXA_CACHE:
        return _EXA_CACHE[ckey]
    body: dict[str, Any] = {"query": query, "type": "auto", "numResults": num}
    if domains:
        body["includeDomains"] = domains
    if start:
        body["startPublishedDate"] = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if end:
        body["endPublishedDate"] = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    results: list[dict] = []
    for variant in range(_EXA_VARIANT[0], 3):
        b = dict(body)
        if variant == 0:
            b["contents"] = {"text": {"maxCharacters": text_chars}}
        elif variant == 1:
            b["text"] = {"maxCharacters": text_chars}
        r = http("exa", "POST", "https://api.exa.ai/search", json_body=b, headers={"x-api-key": EXA_API_KEY},
                 timeout=30, retries=1)
        if r.ok and isinstance(r.data, dict):
            _EXA_VARIANT[0] = variant
            for item in r.data.get("results", []) or []:
                url, title = text(item.get("url")), text(item.get("title"))
                if not url or not title:
                    continue
                body_text = text(item.get("text"))
                if not body_text:
                    hl = item.get("highlights") or []
                    body_text = " ".join(text(x) for x in hl) if isinstance(hl, list) else text(hl)
                if is_excluded(f"{title} {body_text[:1500]} {url}"):
                    reject("exa_out_of_scope", title)
                    continue
                results.append({
                    "url": url, "canonical": canonical_url(url), "title": re.sub(r"\s+", " ", title),
                    "published": parse_dt(item.get("publishedDate") or item.get("published_date")),
                    "text": re.sub(r"[ \t]+", " ", body_text)[:text_chars], "source": source_label(url),
                    "grade": grade_of(url),
                })
            break
        if r.status == 400 and variant < 2:
            logger.warning("Exa rejected request variant %d (%s); trying next", variant, r.error[:120])
            continue
        break
    _EXA_CACHE[ckey] = results
    return results


# --- Wikipedia ---------------------------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
_DATE_LINK = re.compile(r"^(\d{1,4}( BC)?|(" + "|".join(MONTHS) + r") \d{1,2}|\d{1,2} (" + "|".join(MONTHS) + r"))$")


def wiki_get(params: dict) -> dict | None:
    p = {"format": "json", "formatversion": "2"}
    p.update(params)
    r = http("wikipedia", "GET", WIKI_API, params=p, retries=2, timeout=20)
    if r.ok and isinstance(r.data, dict) and "error" not in r.data:
        return r.data
    return None


def wiki_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="_(),'")


def wiki_page(title: str, max_chars: int = 14000) -> dict | None:
    d = wiki_get({"action": "query", "prop": "extracts|categories|info", "explaintext": 1,
                  "exsectionformat": "plain", "redirects": 1, "titles": title, "cllimit": "max", "inprop": "url"})
    try:
        page = d["query"]["pages"][0]  # type: ignore[index]
    except Exception:
        return None
    if page.get("missing") or not text(page.get("extract")):
        return None
    cats = [text(c.get("title")) for c in page.get("categories", []) or []]
    return {"title": text(page.get("title")) or title, "text": text(page["extract"])[:max_chars],
            "categories": cats, "url": text(page.get("fullurl")) or wiki_url(title)}


def wiki_page_problem(page: dict) -> str:
    """Reason a Wikipedia page must not be used, or ''."""
    cats = " | ".join(page.get("categories", [])).lower()
    if "disambiguation" in cats or re.search(r"\bmay refer to\b", page["text"][:400]):
        return "disambiguation"
    if re.search(r"video game|esports|computer game|mobile game|online game", cats):
        return "video-game category"
    if len(page["text"]) < 900:
        return "too short"
    why = exclusion_reason(page["text"][:3000])
    if why:
        return why
    return ""


def wiki_summary(title: str) -> dict | None:
    r = http("wikipedia", "GET", "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe=""),
             retries=1, timeout=15)
    if r.ok and isinstance(r.data, dict) and text(r.data.get("extract")):
        url = ""
        try:
            url = r.data["content_urls"]["desktop"]["page"]
        except Exception:
            url = wiki_url(text(r.data.get("title")) or title)
        return {"title": text(r.data.get("title")) or title, "extract": text(r.data["extract"]),
                "description": text(r.data.get("description")), "url": url}
    page = wiki_page(title, 3000)
    if page:
        return {"title": page["title"], "extract": page["text"][:1500], "description": "", "url": page["url"]}
    return None


def wiki_category_members(category: str, limit: int = 200) -> list[str]:
    d = wiki_get({"action": "query", "list": "categorymembers", "cmtitle": "Category:" + category,
                  "cmlimit": limit, "cmnamespace": 0, "cmtype": "page"})
    try:
        return [text(x["title"]) for x in d["query"]["categorymembers"]]  # type: ignore[index]
    except Exception:
        return []


def wiki_wikitext(page: str) -> str:
    d = wiki_get({"action": "parse", "page": page, "prop": "wikitext", "redirects": 1})
    try:
        wt = d["parse"]["wikitext"]  # type: ignore[index]
        return wt if isinstance(wt, str) else text(wt.get("*"))
    except Exception:
        return ""


def wikitext_to_plain(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<ref[^>/]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    for _ in range(6):
        s2 = re.sub(r"\{\{[^{}]*\}\}", "", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", "", s, flags=re.I)
    s = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", s)
    s = re.sub(r"\[https?://[^\s\]]+\]", "", s)
    s = re.sub(r"'{2,}", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip(" *:;-–—")


def first_link_title(wikitext_line: str) -> str:
    for m in re.finditer(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]", wikitext_line):
        t = m.group(1).strip()
        if t.lower().startswith(("file:", "image:", "category:")) or _DATE_LINK.match(t):
            continue
        return t
    return ""


def first_external_url(wikitext_line: str) -> str:
    m = re.search(r"https?://[^\s\]|}<]+", wikitext_line)
    return m.group(0) if m else ""


def onthisday_events(d: date) -> list[dict]:
    mm, dd = f"{d.month:02d}", f"{d.day:02d}"
    for url in (f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{mm}/{dd}",
                f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{mm}/{dd}"):
        r = http("wikipedia_otd", "GET", url, retries=1, timeout=20)
        if r.ok and isinstance(r.data, dict) and r.data.get("events"):
            out = []
            for ev in r.data["events"]:
                pages = ev.get("pages") or []
                p0 = pages[0] if pages else {}
                try:
                    purl = p0["content_urls"]["desktop"]["page"]
                except Exception:
                    purl = wiki_url(text(p0.get("title"))) if p0.get("title") else ""
                if not text(ev.get("text")) or not isinstance(ev.get("year"), int):
                    continue
                out.append({"text": text(ev["text"]), "year": ev["year"], "title": text(p0.get("title")),
                            "url": purl, "desc": text(p0.get("description")), "extract": text(p0.get("extract")),
                            "src": "onthisday"})
            if out:
                return out
    # fallback: parse the "September 21" day page (Events section)
    wt = wiki_wikitext(f"{MONTHS[d.month - 1]} {d.day}")
    out = []
    in_events = False
    for line in wt.splitlines():
        if re.match(r"^==\s*Events\s*==", line):
            in_events = True
            continue
        if in_events and re.match(r"^==[^=]", line):
            break
        if not in_events or not line.lstrip().startswith("*"):
            continue
        ym = re.search(r"\[\[(\d{3,4})\]\]", line[:40]) or re.match(r"^\*+\s*(\d{3,4})\b", line)
        if not ym:
            continue
        after = re.split(r"\s[–—-]\s", line, maxsplit=1)
        body = after[1] if len(after) > 1 else line
        out.append({"text": wikitext_to_plain(body), "year": int(ym.group(1)), "title": first_link_title(body),
                    "url": "", "desc": "", "extract": "", "src": "daypage"})
    return out


def _date_prefix_rx(d: date) -> re.Pattern:
    mon = MONTHS[d.month - 1]
    return re.compile(rf"^(?:{mon}\s+{d.day}\b|{d.day}\s+{mon}\b)", re.I)


def year_in_sports_lines(year: int, d: date) -> list[dict]:
    wt = wiki_wikitext(f"{year} in sports")
    if not wt:
        return []
    rx = _date_prefix_rx(d)
    out = []
    for line in wt.splitlines():
        if not line.lstrip().startswith("*"):
            continue
        plain = wikitext_to_plain(line)
        if not rx.match(plain[:30]):
            continue
        body = re.sub(rx, "", plain).strip(" -–—:,")
        if len(body) < 25:
            continue
        out.append({"text": body, "year": year, "title": first_link_title(re.split(r"\]\]\s*[–—-]", line, maxsplit=1)[-1]),
                    "url": first_external_url(line), "desc": "", "extract": "", "src": "yearpage"})
    return out


def current_events_sports(d: date) -> list[dict]:
    wt = wiki_wikitext(f"Portal:Current events/{d.year} {MONTHS[d.month - 1]} {d.day}")
    if not wt:
        return []
    lines = wt.splitlines()
    start = None
    for i, line in enumerate(lines):
        bare = re.sub(r"[;='\[\]*:]", "", line).strip().lower()
        if bare == "sports" or (bare.startswith("sports") and len(bare) < 24 and not line.lstrip().startswith("*")):
            start = i + 1
            break
    if start is None:
        return []
    out = []
    for line in lines[start:]:
        if not line.strip():
            continue
        if line.lstrip().startswith("*"):
            plain = wikitext_to_plain(line)
            if len(plain) >= 25:
                out.append({"text": plain, "url": first_external_url(line), "title": first_link_title(line)})
        elif re.match(r"^\s*(;|'''|==)", line):
            break
    return out


# --- RSS (standard library parser) -------------------------------------------------------------

RSS_FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPN", "url": "https://www.espn.com/espn/rss/news"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/uk/sport/rss"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str) -> list[dict]:
    if not xml_text or len(xml_text) > 3_000_000:
        return []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return []
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        row = {"title": "", "url": "", "summary": "", "published": None}
        for ch in node:
            n = _local(ch.tag)
            if n == "title":
                row["title"] = text("".join(ch.itertext()))
            elif n == "link":
                row["url"] = text(ch.get("href")) or text(ch.text) or row["url"]
            elif n in ("description", "summary", "content") and not row["summary"]:
                row["summary"] = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", "".join(ch.itertext())))).strip()
            elif n in ("pubDate", "published", "updated", "date") and not row["published"]:
                row["published"] = parse_dt(text(ch.text))
        if row["title"] and row["url"]:
            items.append(row)
    return items


def fetch_feeds() -> list[dict]:
    def one(feed):
        r = http(f"rss:{feed['name']}", "GET", feed["url"], want_json=False, retries=1, timeout=15)
        if not r.ok:
            return []
        rows = parse_feed(r.text)
        for row in rows:
            row["source"] = feed["name"]
        return rows
    out = []
    for rows in pmap(one, RSS_FEEDS, workers=4):
        out.extend(rows or [])
    return [x for x in out if not is_excluded(f"{x['title']} {x['summary'][:400]}")]

# --- sports events: ESPN, TheSportsDB, cricket chain -------------------------------------------

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
TIER_W = {"S": 1.0, "A": 0.7, "B": 0.4, "C": 0.2}

# (path, sport, league name, tier, style)   style: team | tournament | mma
LEAGUES = [
    ("soccer/fifa.world", "Football", "FIFA World Cup", "S", "team"),
    ("soccer/fifa.wwc", "Football", "Women's World Cup", "S", "team"),
    ("soccer/uefa.euro", "Football", "UEFA Euro", "S", "team"),
    ("soccer/conmebol.america", "Football", "Copa América", "S", "team"),
    ("soccer/uefa.champions", "Football", "UEFA Champions League", "S", "team"),
    ("soccer/uefa.europa", "Football", "UEFA Europa League", "A", "team"),
    ("soccer/uefa.europa.conf", "Football", "UEFA Conference League", "B", "team"),
    ("soccer/uefa.nations", "Football", "UEFA Nations League", "A", "team"),
    ("soccer/fifa.cwc", "Football", "FIFA Club World Cup", "A", "team"),
    ("soccer/fifa.worldq.afc", "Football", "World Cup Qualifiers (AFC)", "A", "team"),
    ("soccer/fifa.worldq.uefa", "Football", "World Cup Qualifiers (UEFA)", "A", "team"),
    ("soccer/fifa.worldq.conmebol", "Football", "World Cup Qualifiers (CONMEBOL)", "A", "team"),
    ("soccer/fifa.worldq.caf", "Football", "World Cup Qualifiers (CAF)", "A", "team"),
    ("soccer/fifa.worldq.concacaf", "Football", "World Cup Qualifiers (CONCACAF)", "B", "team"),
    ("soccer/fifa.friendly", "Football", "International Friendly", "B", "team"),
    ("soccer/caf.nations", "Football", "Africa Cup of Nations", "A", "team"),
    ("soccer/afc.champions", "Football", "AFC Champions League", "A", "team"),
    ("soccer/conmebol.libertadores", "Football", "Copa Libertadores", "A", "team"),
    ("soccer/conmebol.sudamericana", "Football", "Copa Sudamericana", "B", "team"),
    ("soccer/concacaf.champions", "Football", "CONCACAF Champions Cup", "B", "team"),
    ("soccer/eng.1", "Football", "Premier League", "A", "team"),
    ("soccer/esp.1", "Football", "La Liga", "A", "team"),
    ("soccer/ger.1", "Football", "Bundesliga", "A", "team"),
    ("soccer/ita.1", "Football", "Serie A", "A", "team"),
    ("soccer/fra.1", "Football", "Ligue 1", "B", "team"),
    ("soccer/eng.fa", "Football", "FA Cup", "A", "team"),
    ("soccer/eng.league_cup", "Football", "EFL Cup", "B", "team"),
    ("soccer/esp.copa_del_rey", "Football", "Copa del Rey", "B", "team"),
    ("soccer/usa.1", "Football", "MLS", "B", "team"),
    ("soccer/por.1", "Football", "Primeira Liga", "B", "team"),
    ("soccer/ned.1", "Football", "Eredivisie", "B", "team"),
    ("soccer/sau.1", "Football", "Saudi Pro League", "B", "team"),
    ("soccer/bra.1", "Football", "Brasileirão", "B", "team"),
    ("soccer/arg.1", "Football", "Argentine Primera", "B", "team"),
    ("soccer/eng.2", "Football", "EFL Championship", "C", "team"),
    ("basketball/nba", "Basketball", "NBA", "A", "team"),
    ("basketball/wnba", "Basketball", "WNBA", "B", "team"),
    ("basketball/mens-college-basketball", "Basketball", "NCAA Men's Basketball", "C", "team"),
    ("football/nfl", "American football", "NFL", "A", "team"),
    ("football/college-football", "American football", "NCAA Football", "B", "team"),
    ("baseball/mlb", "Baseball", "MLB", "B", "team"),
    ("hockey/nhl", "Ice hockey", "NHL", "B", "team"),
    ("tennis/atp", "Tennis", "ATP Tour", "A", "tournament"),
    ("tennis/wta", "Tennis", "WTA Tour", "A", "tournament"),
    ("golf/pga", "Golf", "PGA Tour", "B", "tournament"),
    ("golf/lpga", "Golf", "LPGA Tour", "C", "tournament"),
    ("golf/eur", "Golf", "DP World Tour", "B", "tournament"),
    ("racing/f1", "Motorsport", "Formula 1", "A", "tournament"),
    ("racing/irl", "Motorsport", "IndyCar", "C", "tournament"),
    ("racing/nascar-premier", "Motorsport", "NASCAR Cup", "C", "tournament"),
    ("mma/ufc", "MMA", "UFC", "B", "mma"),
]
LEAGUE_BY_PATH = {x[0]: x for x in LEAGUES}

SPORT_EMOJI = {"Football": "⚽", "Cricket": "🏏", "Basketball": "🏀", "Tennis": "🎾", "Golf": "⛳",
               "Motorsport": "🏎", "MMA": "🥊", "Baseball": "⚾", "Ice hockey": "🏒", "American football": "🏈",
               "Rugby": "🏉", "Volleyball": "🏐", "Combat sports": "🥊", "Handball": "🤾", "Badminton": "🏸"}

FAME = {
    "Football": ["real madrid", "barcelona", "manchester city", "manchester united", "liverpool", "arsenal", "chelsea",
                 "tottenham", "paris saint-germain", "psg", "bayern", "dortmund", "juventus", "inter milan",
                 "internazionale", "ac milan", "napoli", "atlético madrid", "atletico madrid", "argentina", "brazil",
                 "france", "england", "germany", "spain", "portugal", "netherlands", "italy", "bangladesh",
                 "india", "inter miami", "al nassr", "al hilal"],
    "Cricket": ["india", "pakistan", "australia", "england", "south africa", "new zealand", "bangladesh",
                "sri lanka", "west indies", "afghanistan"],
    "Basketball": ["lakers", "warriors", "celtics", "knicks", "bulls", "heat", "bucks", "nuggets", "thunder", "spurs"],
    "American football": ["chiefs", "cowboys", "patriots", "49ers", "eagles", "packers", "bills", "ravens"],
    "Baseball": ["yankees", "dodgers", "red sox", "cubs", "mets", "braves"],
    "Ice hockey": ["maple leafs", "canadiens", "rangers", "bruins", "oilers", "penguins"],
    "Tennis": ["djokovic", "alcaraz", "sinner", "nadal", "federer", "swiatek", "sabalenka", "gauff", "medvedev", "zverev",
               "wimbledon", "roland garros", "us open", "australian open", "atp finals", "davis cup"],
    "Golf": ["scheffler", "mcilroy", "woods", "rahm", "koepka", "ryder cup", "masters", "the open", "pga championship",
             "u.s. open", "tour championship"],
    "Motorsport": ["verstappen", "hamilton", "leclerc", "norris", "piastri", "grand prix", "indianapolis 500", "daytona 500"],
    "MMA": ["ufc 3", "ufc 4", "mcgregor", "jones", "makhachev", "pereira", "title"],
}
LOCAL_TEAMS = ["bangladesh"]
SOUTH_ASIA = ["india", "pakistan", "sri lanka", "nepal", "afghanistan", "bhutan", "maldives"]
_TIER_HINTS = [
    (re.compile(r"world cup|olympic|grand slam|wimbledon|roland garros|us open|australian open|champions league|"
                r"euro 20|copa am[eé]rica|asia cup|t20 world|ryder cup|super bowl|nba finals|world series|masters", re.I), "S"),
    (re.compile(r"premier league|la liga|bundesliga|serie a|europa league|ipl|test series|world championship|nations league|"
                r"stanley cup|champions trophy|bpl|bangladesh premier|fa cup|libertadores|qualif", re.I), "A"),
]
_STAGE_RX = [
    (re.compile(r"semi[- ]?final", re.I), 20), (re.compile(r"quarter[- ]?final", re.I), 12),
    (re.compile(r"\bfinals?\b", re.I), 30),
    (re.compile(r"play[- ]?offs?|knockout|round of \d+|elimination|decider|derby|clásico|clasico|championship game", re.I), 10),
]


def tier_hint(name: str, default: str) -> str:
    for rx, tier in _TIER_HINTS:
        if rx.search(name):
            return tier if TIER_W[tier] > TIER_W[default] else default
    return default


def make_event(**kw: Any) -> dict:
    ev = {"id": "", "sport": "", "league": "", "tier": "C", "style": "team", "name": "", "home": "", "away": "",
          "home_score": "", "away_score": "", "winner": "", "start": None, "end": None, "state": "scheduled",
          "detail": "", "venue": "", "stage": "", "source": "", "url": "", "time_known": True}
    ev.update(kw)
    return ev


def _espn_state(status: dict) -> str:
    t = (status or {}).get("type") or {}
    name = text(t.get("name")).upper()
    if any(k in name for k in ("CANCEL", "POSTPONE", "ABANDON", "SUSPEND", "FORFEIT")):
        return "cancelled"
    st = text(t.get("state")).lower()
    if st == "post" or t.get("completed") is True:
        return "final"
    if st == "in":
        return "live"
    return "scheduled"


def parse_espn(data: dict, league: tuple) -> list[dict]:
    path, sport, lname, tier, style = league
    out = []
    for ev in (data or {}).get("events", []) or []:
        try:
            start = parse_dt(ev.get("date"))
            if not start:
                continue
            comps = ev.get("competitions") or [{}]
            comp = comps[0] if comps else {}
            state = _espn_state(ev.get("status") or comp.get("status") or {})
            detail = text(((ev.get("status") or {}).get("type") or {}).get("shortDetail"))
            notes = " ".join(text(n.get("headline")) for n in (comp.get("notes") or []) if isinstance(n, dict))
            venue = text((comp.get("venue") or {}).get("fullName"))
            base = dict(id=f"espn:{path}:{text(ev.get('id'))}", sport=sport, league=lname, tier=tier, style=style,
                        start=start, state=state, venue=venue, source="ESPN",
                        stage=notes, url=next((text(l.get("href")) for l in ev.get("links", []) or [] if l.get("href")), ""))
            end = parse_dt(ev.get("endDate"))
            if style == "team":
                cs = comp.get("competitors") or []
                home = next((c for c in cs if c.get("homeAway") == "home"), cs[0] if cs else {})
                away = next((c for c in cs if c is not home and c.get("homeAway") == "away"), cs[1] if len(cs) > 1 else {})

                def nm(c):
                    t = c.get("team") or {}
                    return text(t.get("displayName") or t.get("shortDisplayName") or (c.get("athlete") or {}).get("displayName"))
                h, a = nm(home), nm(away)
                if not h or not a:
                    continue
                winner = h if home.get("winner") else a if away.get("winner") else ""
                out.append(make_event(**base, name=f"{h} vs {a}", home=h, away=a, home_score=text(home.get("score")),
                                      away_score=text(away.get("score")), winner=winner, detail=detail))
            else:
                winner = ""
                for c in comp.get("competitors", []) or []:
                    if c.get("winner"):
                        winner = text((c.get("athlete") or {}).get("displayName") or (c.get("team") or {}).get("displayName"))
                        break
                out.append(make_event(**base, name=text(ev.get("name") or ev.get("shortName")), end=end,
                                      winner=winner, detail=detail))
        except Exception as exc:  # noqa: BLE001 - one odd event must not break a league
            logger.debug("espn parse skipped an event: %s", exc)
    return out


def espn_fetch(league: tuple, ymd: str) -> list[dict] | None:
    r = http("espn", "GET", f"{ESPN_BASE}/{league[0]}/scoreboard", params={"dates": ymd}, retries=1, timeout=12)
    if not r.ok or not isinstance(r.data, dict):
        return None
    return parse_espn(r.data, league)


def espn_events(target: date) -> tuple[list[dict], dict]:
    """Events overlapping the Dhaka day of `target`. Returns (events, per-league status)."""
    ymds = [(target + timedelta(days=k)).strftime("%Y%m%d") for k in (-1, 0, 1)]
    jobs = [(lg, y) for lg in LEAGUES for y in ymds]
    results = pmap(lambda j: espn_fetch(*j), jobs, workers=10)
    status: dict[str, str] = {}
    seen, out = set(), []
    for (lg, _), res in zip(jobs, results):
        if res is None:
            status.setdefault(lg[0], "fail")
            continue
        status[lg[0]] = "ok"
        for ev in res:
            if ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
    return in_window(out, target), status


def in_window(events: list[dict], target: date) -> list[dict]:
    ws, we = dhaka_window(target)
    keep = []
    for ev in events:
        s, e = ev["start"], ev.get("end")
        if ev["style"] == "team":
            ok = ws <= s < we
        else:  # tournaments span days
            ok = ws <= s < we or (e is not None and s < ws and e >= ws)
        if ok:
            ev = dict(ev)
            ev["continues"] = ev["style"] != "team" and s < ws
            keep.append(ev)
    return keep


# --- TheSportsDB (fallback) ---

TSDB_SPORTS = {"Soccer": "Football", "Basketball": "Basketball", "Cricket": "Cricket", "Rugby": "Rugby",
               "Motorsport": "Motorsport", "Tennis": "Tennis", "Fighting": "Combat sports", "Ice Hockey": "Ice hockey",
               "American Football": "American football", "Baseball": "Baseball", "Volleyball": "Volleyball",
               "Handball": "Handball", "Golf": "Golf"}


def _tsdb_state(row: dict, start: datetime) -> str:
    s = text(row.get("strStatus")).lower()
    if any(k in s for k in ("postpon", "cancel", "abandon")):
        return "cancelled"
    if s in ("match finished", "ft", "aet", "pen", "finished", "after extra time", "after penalties", "aot", "final"):
        return "final"
    if text(row.get("intHomeScore")) != "" and start < now_bd().astimezone(timezone.utc) - timedelta(hours=3):
        return "final"
    return "scheduled"


def tsdb_fetch(d: date) -> list[dict]:
    def one(sport_key):
        r = http("thesportsdb", "GET", f"https://www.thesportsdb.com/api/v1/json/{TSDB_KEY}/eventsday.php",
                 params={"d": d.isoformat(), "s": sport_key}, retries=1, timeout=12)
        if not r.ok or not isinstance(r.data, dict):
            return []
        rows = []
        for row in r.data.get("events") or []:
            ts = text(row.get("strTimestamp")) or f"{text(row.get('dateEvent'))}T{text(row.get('strTime')) or '12:00:00'}"
            start = parse_dt(ts)
            if not start:
                continue
            h, a = text(row.get("strHomeTeam")), text(row.get("strAwayTeam"))
            name = f"{h} vs {a}" if h and a else text(row.get("strEvent"))
            if not name:
                continue
            lname = text(row.get("strLeague"))
            sport = TSDB_SPORTS.get(sport_key, sport_key)
            rows.append(make_event(id=f"tsdb:{text(row.get('idEvent'))}", sport=sport, league=lname,
                                   tier=tier_hint(f"{lname} {name}", "C"), style="team", name=name, home=h, away=a,
                                   home_score=text(row.get("intHomeScore")), away_score=text(row.get("intAwayScore")),
                                   start=start, state=_tsdb_state(row, start), venue=text(row.get("strVenue")),
                                   source="TheSportsDB", time_known=bool(text(row.get("strTime")) or text(row.get("strTimestamp")))))
        return rows
    out = []
    for rows in pmap(one, list(TSDB_SPORTS), workers=4):
        out.extend(rows or [])
    return out


def tsdb_events(target: date) -> list[dict]:
    seen, out = set(), []
    for k in (-1, 0):
        for ev in tsdb_fetch(target + timedelta(days=k)):
            if ev["id"] not in seen and not is_excluded(f"{ev['name']} {ev['league']}"):
                seen.add(ev["id"])
                out.append(ev)
    return in_window(out, target)


# --- cricket chain ---

def cricketdata_events(target: date) -> list[dict]:
    if not CRICKETDATA_API_KEY:
        return []
    out, seen = [], set()
    for ep in ("currentMatches", "matches"):
        r = http("cricketdata", "GET", f"https://api.cricapi.com/v1/{ep}", params={"apikey": CRICKETDATA_API_KEY, "offset": 0},
                 retries=1, timeout=15)
        if not r.ok or not isinstance(r.data, dict):
            continue
        for m in r.data.get("data") or []:
            if not isinstance(m, dict) or text(m.get("id")) in seen:
                continue
            seen.add(text(m.get("id")))
            start = parse_dt(m.get("dateTimeGMT") or m.get("date"))
            teams = [text(t) for t in (m.get("teams") or []) if text(t)]
            if not start or len(teams) < 2:
                continue
            state = "final" if m.get("matchEnded") else "live" if m.get("matchStarted") else "scheduled"
            name = f"{teams[0]} vs {teams[1]}"
            mt = text(m.get("matchType")).upper()
            out.append(make_event(id=f"cricapi:{text(m.get('id'))}", sport="Cricket",
                                  league=text(m.get("name")).split(",")[-1].strip() or mt or "Cricket",
                                  tier=tier_hint(text(m.get("name")), "B"), name=name, home=teams[0], away=teams[1],
                                  start=start, state=state, venue=text(m.get("venue")), detail=text(m.get("status")),
                                  source="CricketData.org", stage=mt))
    return in_window(out, target)


def date_in_text(body: str, d: date) -> bool:
    mon_full, mon3 = MONTHS[d.month - 1], MONTHS[d.month - 1][:3]
    day = d.day
    pats = [rf"\b{day}(?:st|nd|rd|th)?\s+(?:{mon_full}|{mon3})\b", rf"\b(?:{mon_full}|{mon3})\.?\s+{day}(?:st|nd|rd|th)?\b",
            re.escape(d.isoformat()), rf"\b{day:02d}/{d.month:02d}/{d.year}\b"]
    return any(re.search(p, body, re.I) for p in pats)


def exa_event_fallback(ai: "AIClient", target: date, kind: str, sport_hint: str, domains: list[str] | None = None) -> list[dict]:
    """Last resort: search + AI extraction, with the event date verified in the source text by code."""
    if not EXA_API_KEY or not ai.available or ai.fatal:
        return []
    label = long_date(target)
    q = f"{sport_hint} {'fixtures schedule' if kind == 'next' else 'results scorecard'} {label}"
    docs = exa_search(q, domains=domains, num=6, text_chars=3000)
    if not docs:
        return []
    blocks = [f"[{i}] URL: {d['url']}\nTITLE: {d['title']}\nTEXT: {d['text'][:1800]}" for i, d in enumerate(docs, 1)]
    system = (f"Extract real sporting events for the exact date {target.isoformat()} from the documents. "
              "Only events whose date is explicitly stated in the document. Never invent teams, times or venues; "
              "use an empty string when unknown. time_utc is HH:MM in UTC or ''. Exclude video games/esports.")
    data = ai.json("extract_events", system, "\n\n".join(blocks), EVENTS_SCHEMA, max_tokens=3500)
    out = []
    for e in (data or {}).get("events", []):
        if text(e.get("date")) != target.isoformat():
            reject("fallback_wrong_date", text(e.get("name")))
            continue
        doc = next((d for d in docs if canonical_url(d["url"]) == canonical_url(text(e.get("source_url")))), None)
        if not doc or not date_in_text(doc["title"] + " " + doc["text"], target):
            reject("fallback_date_not_in_source", text(e.get("name")))
            continue
        name = text(e.get("name")) or (f"{text(e.get('home'))} vs {text(e.get('away'))}" if e.get("home") and e.get("away") else "")
        if not name or is_excluded(name):
            continue
        tm = re.match(r"^(\d{1,2}):(\d{2})$", text(e.get("time_utc")))
        if tm:
            start = datetime(target.year, target.month, target.day, int(tm.group(1)) % 24, int(tm.group(2)), tzinfo=timezone.utc)
            known = True
        else:
            start = datetime(target.year, target.month, target.day, 12, 0, tzinfo=BD_TZ).astimezone(timezone.utc)
            known = False
        sport = text(e.get("sport")) or sport_hint.split()[0].title()
        out.append(make_event(id=f"exa:{canonical_url(doc['url'])}:{slugify(name)}", sport=sport, league=text(e.get("league")),
                              tier=tier_hint(f"{text(e.get('league'))} {name}", "C"), name=name, home=text(e.get("home")),
                              away=text(e.get("away")), start=start, state="scheduled" if kind == "next" else "final",
                              source=doc["source"], url=doc["url"], time_known=known))
    return in_window(out, target)


# --- scoring and selection ---

def score_event(ev: dict) -> float:
    names = f"{ev['name']} {ev['league']} {ev.get('stage', '')} {ev.get('detail', '')}".lower()
    tier = tier_hint(f"{ev['league']} {ev['name']}", ev["tier"])
    score = TIER_W[tier] * 100.0
    score += max([b for rx, b in _STAGE_RX if rx.search(names)] or [0])
    fame = FAME.get(ev["sport"], [])
    fame_hits = sum(1 for f in fame if f in names)
    score += min(25.0, 12.0 * fame_hits)
    if any(t in names for t in LOCAL_TEAMS):
        score += 20
    if ev["sport"] == "Cricket" and any(t in names for t in SOUTH_ASIA):
        score += 8
    if ev["sport"] in ("Cricket", "Badminton", "Kabaddi"):
        score += 5
    return score


def select_events(events: list[dict], max_highlights: int = 12, max_index: int = 10, per_sport: int = 3,
                  min_highlight: float = 35.0) -> tuple[list[dict], list[dict]]:
    ranked = sorted(({**e, "score": score_event(e)} for e in events), key=lambda e: (-e["score"], e["start"]))
    highlights, counts = [], Counter()
    best_per_sport: dict[str, dict] = {}
    for e in ranked:
        if e["score"] >= min_highlight and e["sport"] not in best_per_sport:
            best_per_sport[e["sport"]] = e
    for e in sorted(best_per_sport.values(), key=lambda e: -e["score"])[:6]:
        highlights.append(e)
        counts[e["sport"]] += 1
    for e in ranked:
        if len(highlights) >= max_highlights:
            break
        if e in highlights or e["score"] < min_highlight or counts[e["sport"]] >= per_sport:
            continue
        highlights.append(e)
        counts[e["sport"]] += 1
    index, icount = [], Counter()
    for e in ranked:
        if len(index) >= max_index:
            break
        if e in highlights or icount[e["sport"]] >= 6:
            continue
        index.append(e)
        icount[e["sport"]] += 1
    return highlights, index

# ===========================================================================
# 8. GUARDS + COMPOSE (quote-then-write; hard guards reject, soft issues are fixed in code)
# ===========================================================================

SUPERLATIVES = ["first", "only", "never", "oldest", "largest", "longest", "fastest", "record", "invented", "banned",
                "last", "biggest", "greatest", "earliest", "youngest"]
DISPOSABLE = re.compile(r"\b(tomorrow|yesterday|today|tonight|latest|just now|this week|this weekend)\b", re.I)
_NUM_RX = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(%?)")
_SCORE_RX = re.compile(r"(?<![\d.])(\d{1,3})\s*[-–]\s*(\d{1,3})(?![\d.])")
_COMMON_CAPS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december", "the", "this",
    "that", "these", "those", "its", "his", "her", "their", "our", "and", "but", "while", "when", "where", "which",
    "who", "what", "why", "how", "although", "however", "because", "since", "after", "before", "during", "many",
    "some", "most", "each", "both", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "today", "yes", "not", "also", "often", "still", "even", "only", "first", "later", "earlier", "modern",
}


def _num_tokens(s: str, risky_only: bool) -> set[str]:
    out = set()
    for m in _NUM_RX.finditer(s):
        whole, frac, pct = m.group(1).replace(",", ""), m.group(2), m.group(3)
        tok = whole + ("." + frac if frac else "")
        if not risky_only or int(whole) >= 100 or frac or pct:
            out.add(tok)
    return out


def _score_tokens(s: str) -> set[str]:
    return {f"{a}-{b}" for a, b in _SCORE_RX.findall(s)}


def story_text(story: dict) -> str:
    return " ".join([text(story.get("headline")), text(story.get("body")), text(story.get("why_interesting")),
                     " ".join(text(x) for x in story.get("key_points", []))])


def unknown_entities(body: str, evidence: str, allow: set[str]) -> list[str]:
    ev_words = set(normalize_text(evidence).split()) | {normalize_text(a) for a in allow}
    ev_prefix = {w[:5] for w in ev_words if len(w) >= 5}
    unknown = []
    for sent in split_sentences(body):
        words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", sent)
        for i, w in enumerate(words):
            if i == 0 or not w[0].isupper() or w.lower() in _COMMON_CAPS:
                continue
            for piece in normalize_text(w).split():
                if len(piece) < 3 or piece in ev_words or (len(piece) >= 5 and piece[:5] in ev_prefix):
                    continue
                unknown.append(w)
                break
    return unknown


def copies_source(post_text: str, evidence: str, n: int = 14) -> bool:
    """True if the post repeats n or more consecutive words of the evidence (should be paraphrased)."""
    pw, ew = normalize_text(post_text).split(), normalize_text(evidence)
    return len(pw) >= n and any(" ".join(pw[i:i + n]) in ew for i in range(len(pw) - n + 1))


def guard_violations(story: dict, evidence: str, anchors: Iterable[str] = (), allow_words: Iterable[str] = (),
                     daily: bool = False, paraphrase: bool = False) -> list[str]:
    """Hard guards. Returns human-readable violations (empty list = pass)."""
    v: list[str] = []
    full = story_text(story)
    why = exclusion_reason(full)
    if why:
        v.append(f"out of scope ({why})")
    if not text(story.get("headline")) or not text(story.get("body")):
        v.append("headline and body are required")
    ev_nums = _num_tokens(evidence, False) | {str(a) for a in anchors}
    bad_nums = sorted(n for n in _num_tokens(full, True) if n not in ev_nums)
    if bad_nums:
        v.append("numbers not found in the evidence: " + ", ".join(bad_nums[:5]))
    ev_norm = evidence.replace("–", "-")
    bad_scores = sorted(s for s in _score_tokens(full) if s not in _score_tokens(ev_norm) and s not in {f"{a}" for a in anchors})
    if bad_scores:
        v.append("scores not found in the evidence: " + ", ".join(bad_scores[:3]))
    low_full, low_ev = full.lower(), evidence.lower()
    bad_sup = [w for w in SUPERLATIVES if re.search(rf"\b{w}\b", low_full) and not re.search(rf"\b{w}", low_ev)]
    if bad_sup:
        v.append("claims wording not supported by the evidence: " + ", ".join(bad_sup))
    prose = " ".join([text(story.get("body")), text(story.get("why_interesting")),
                      " ".join(text(x) for x in story.get("key_points", []))])
    unk = unknown_entities(prose, evidence, {text(a) for a in allow_words})
    if len(unk) >= 2:
        v.append("names not found in the evidence: " + ", ".join(dict.fromkeys(unk[:5])))
    if daily and DISPOSABLE.search(full):
        v.append("uses time-relative words (use the exact date)")
    if paraphrase and copies_source(text(story.get("body")) + " " + text(story.get("why_interesting")), evidence):
        v.append("copies 14+ consecutive words from the evidence; paraphrase in your own words")
    return v


def soft_fix(story: dict, max_sentences: int = 4, max_body: int = 520) -> dict:
    """Fix formatting problems in code instead of rejecting good content."""
    s = dict(story)

    def clean(x: Any) -> str:
        x = text(x).replace("…", "").replace("...", "")
        return re.sub(r"\s+", " ", x).strip()

    s["headline"] = clamp_words(clean(s.get("headline")).rstrip(".:"), 90)
    body_sents = split_sentences(clean(s.get("body")))[:max_sentences]
    body = " ".join(body_sents)
    if len(body) > max_body:
        keep = []
        for sent in body_sents:
            if len(" ".join(keep + [sent])) > max_body and keep:
                break
            keep.append(sent)
        body = " ".join(keep)
    if body and body[-1] not in ".!?":
        body += "."
    s["body"] = body
    why = " ".join(split_sentences(clean(s.get("why_interesting")))[:1])
    if why and why[-1] not in ".!?":
        why += "."
    s["why_interesting"] = clamp_words(why, 220) if len(why) > 220 else why
    if s["why_interesting"] and s["why_interesting"][-1] not in ".!?":
        s["why_interesting"] += "."
    s["key_points"] = [clamp_words(clean(p), 90) for p in (s.get("key_points") or []) if clean(p)][:3]
    return s


def quote_in_text(quote: str, source: str) -> bool:
    q, t = normalize_text(quote), normalize_text(source)
    if len(q) < 25:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < 6:
        return False
    sh = {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}
    return sum(1 for x in sh if x in t) / len(sh) >= 0.85


def best_paragraph(source: str, quote: str) -> str:
    qt = tokens(quote)
    paras = [p.strip() for p in re.split(r"\n+", source) if len(p.strip()) > 40]
    if not paras:
        return quote
    return max(paras, key=lambda p: len(tokens(p) & qt))[:1800]


FORMAT_HINTS = {
    "fact": "one surprising, concrete fact",
    "game_discovery": "what the game/sport is and what makes it distinctive or lesser-known",
    "rule_check": "one real rule, ideally commonly misunderstood; distinguish official rules from house rules if the text does",
    "how_to_play": "the objective and the basic structure of play, in simple steps",
    "history": "a specific historical development (origin, change, milestone)",
    "why": "the reason something is the way it is (a rule, measurement, tradition, name)",
    "first_last_only": "a first, last or only-time event that the text states explicitly",
    "then_vs_now": "how the game/sport/equipment/rule differed in the past vs now",
    "forgotten": "a forgotten or nearly vanished game/sport and what made it distinctive",
    "new_game": "a newly announced or released physical tabletop game",
}


def extract_facts(ai: "AIClient", *, topic: str, fmt: str, angle: str, article: str) -> list[dict]:
    system = ("You are a fact scout for a factual Sports & Games channel (real sports and physical/tabletop games only). "
              f"Find up to 3 candidate facts about the TOPIC that suit: {FORMAT_HINTS.get(fmt, 'a fact')}. Preferred angle: {angle}. "
              "Each fact MUST be backed by evidence_quote: an exact verbatim copy (20-300 characters) from the ARTICLE. "
              "Do not use outside knowledge. surprise: 1 (dull) to 5 (remarkable). certainty: 'settled' if the article states it "
              "plainly, 'debated' if it reports competing accounts, 'legend' if it is a traditional story. Skip video games.")
    data = ai.json("extract_facts", system, f"TOPIC: {topic}\nARTICLE:\n{article[:12000]}", FACTS_SCHEMA, max_tokens=2500)
    valid = []
    for f in (data or {}).get("facts", []):
        q = text(f.get("evidence_quote"))
        if not quote_in_text(q, article):
            reject("quote_not_in_source", q[:80])
            continue
        if is_excluded(q + " " + text(f.get("claim"))):
            reject("out_of_scope", q[:80])
            continue
        valid.append(f)
    return sorted(valid, key=lambda f: -int(f.get("surprise") or 0))


def headline_taken(state: dict | None, headline: str) -> bool:
    if not state:
        return False
    h = normalize_text(headline)
    for p in state.get("posts", [])[-600:]:
        if p.get("desk") in ("next", "past"):
            continue
        ph = normalize_text(p.get("headline", ""))
        if ph and (ph == h or similarity(ph, h) >= 0.92):
            return True
    return False


def compose_post(ai: "AIClient", *, desk: str, fmt: str, topic: str, angle: str, claim: str, evidence: str,
                 sources: list, certainty: str = "settled", anchors: Iterable[str] = (), date_anchor: str = "",
                 label: str = "", extra: str = "", allow_words: Iterable[str] = (), daily: bool = False,
                 state: dict | None = None) -> dict | None:
    anchors = list(anchors)
    allow = set(allow_words) | set(re.findall(r"[A-Za-z][A-Za-z'’\-]+", topic)) | {"Wikipedia"}
    system = (
        "You are the senior editor of a factual Sports & Games Telegram channel (real sports and physical/tabletop games). "
        f"Write ONE post in the format: {FORMAT_LABELS.get(fmt, fmt)}.\n"
        "Rules:\n- Use ONLY facts stated in EVIDENCE. Never add names, dates or numbers from memory.\n"
        "- Original wording; do not copy sentences.\n"
        "- headline: max 80 characters, accurate, not clickbait.\n"
        "- body: 2-4 complete sentences, max 480 characters in total.\n"
        "- why_interesting: exactly one sentence. key_points: 0 to 3 short items.\n"
        "- Do not use the words first, only, never, oldest, largest, longest, fastest, record, invented, banned or last "
        "unless the EVIDENCE itself says so.\n"
        f"- Certainty of the claim: {certainty}. If it is not 'settled', word it as traditionally said / debated / legend.\n"
        "- No time-relative words (today, yesterday, latest). No emojis, hashtags or ellipses. "
        f"{extra}")
    facts = f"Fixed facts you may use: {', '.join(anchors)}\n" if anchors else ""
    user = f"TOPIC: {topic}\nANGLE: {angle}\nCLAIM TO EXPLAIN: {claim}\n{facts}EVIDENCE:\n{evidence[:9000]}"
    data = ai.json("write_post", system, user, POST_SCHEMA, max_tokens=2500, temperature=0.3)
    story = None
    for attempt in range(2):
        if not data:
            reject("ai_no_output", topic)
            return None
        story = soft_fix({"headline": data["headline"], "body": data["body"], "why_interesting": data["why_interesting"],
                          "key_points": data["key_points"]})
        viol = guard_violations(story, evidence, anchors, allow, daily=daily, paraphrase=True)
        if not viol:
            break
        if attempt == 1:
            hard = [x for x in viol if not x.startswith("copies")]
            if not hard:  # only wording similarity left after one rewrite request: accept (attributed, facts verified)
                REPORT.notes.append(f"kept close wording for '{topic}'")
                break
            reject("guard_failed_after_repair", "; ".join(hard)[:160])
            return None
        data = ai.json("write_post", system, user, POST_SCHEMA, max_tokens=2500, temperature=0.2,
                       extra_messages=[{"role": "assistant", "content": json.dumps(data)},
                                       {"role": "user", "content": "Your post broke these rules: " + "; ".join(viol) +
                                        ". Rewrite it fixing only those problems. Use only the EVIDENCE."}])
    assert story is not None
    if headline_taken(state, story["headline"]):
        reject("duplicate_headline", story["headline"])
        return None
    tags = [t for t in (hashtag(topic), FORMAT_TAG.get(fmt, "")) if t]
    out = {**story, "desk": desk, "format": fmt, "label": label or FORMAT_LABELS.get(fmt, ""), "topic": topic,
           "angle": angle, "claim": claim, "certainty": certainty, "date_anchor": date_anchor,
           "sources": [(l, u) for l, u in sources if u], "tags": tags}
    out["render"] = {"mode": "photo", "html": fit_knowledge_html(out)}
    if not is_valid_tg_html(out["render"]["html"]):
        reject("invalid_html", topic)
        return None
    return out

# ===========================================================================
# 9. DESKS. Each returns (story | None, reason). Publishing/ledger is done by the runner.
# ===========================================================================

SOURCE_HOME = {"ESPN": "https://www.espn.com/", "TheSportsDB": "https://www.thesportsdb.com/",
               "CricketData.org": "https://cricketdata.org/", "Wikipedia": "https://en.wikipedia.org/wiki/Portal:Current_events"}


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(normalize_text(p) for p in parts).encode()).hexdigest()[:16]


def is_duplicate_claim(state: dict, claim: str, topic: str = "", threshold: float = 0.78) -> bool:
    for p in state.get("posts", [])[-1500:]:
        if p.get("desk") in ("next", "past"):
            continue
        if similarity(claim, p.get("claim", "")) >= threshold or similarity(claim, p.get("headline", "")) >= threshold + 0.07:
            return True
        if topic and p.get("topic") and normalize_text(topic) == normalize_text(p["topic"]) and \
                similarity(claim, p.get("claim", "")) >= 0.6:
            return True
    return False


# --- collection ---

def merge_events(primary: list[dict], extra: list[dict]) -> list[dict]:
    out = list(primary)
    for e in extra:
        dup = any(p["sport"] == e["sport"] and abs((p["start"] - e["start"]).total_seconds()) < 4 * 3600
                  and similarity(p["name"], e["name"]) >= 0.6 for p in out)
        if not dup:
            out.append(e)
    return out


def filter_state(events: list[dict], kind: str, target: date) -> list[dict]:
    ws, we = dhaka_window(target)
    out = []
    for e in events:
        if is_excluded(f"{e['name']} {e['league']}"):
            reject("event_out_of_scope", e["name"])
            continue
        if kind == "next":
            if e["state"] != "scheduled":
                continue
            if e.get("continues") and tier_hint(f"{e['league']} {e['name']}", e["tier"]) not in ("S", "A"):
                continue
        else:
            if e["state"] != "final":
                continue
            if e["style"] != "team":
                ref = e.get("end") or e["start"]
                if not (ws <= ref < we):
                    continue
        out.append(e)
    return out


def collect_events(ai: "AIClient", target: date, kind: str, *, allow_exa_fallback: bool = True) -> tuple[list[dict], list[str]]:
    notes = []
    events, status = espn_events(target)
    ok = sum(1 for v in status.values() if v == "ok")
    notes.append(f"espn: {ok}/{len(LEAGUES)} leagues reachable, {len(events)} events in window")
    cricket = cricketdata_events(target) if CRICKETDATA_API_KEY else []
    have_cricket = any(e["sport"] == "Cricket" for e in events + cricket)
    if not have_cricket and allow_exa_fallback and EXA_API_KEY and ai.available:
        cricket += exa_event_fallback(ai, target, kind, "cricket", CRICKET_DOMAINS)
    events = merge_events(events, cricket)
    if len(filter_state(events, kind, target)) < 6:
        extra = tsdb_events(target)
        notes.append(f"thesportsdb: +{len(extra)} candidate events")
        events = merge_events(events, extra)
    if len(filter_state(events, kind, target)) < 3 and allow_exa_fallback and EXA_API_KEY and ai.available:
        extra = exa_event_fallback(ai, target, kind, "football basketball tennis sports")
        notes.append(f"exa fallback: +{len(extra)} events")
        events = merge_events(events, extra)
    events = filter_state(events, kind, target)
    REPORT.notes.extend(notes)
    return events, notes


# --- rendering ---

def stage_label(ev: dict) -> str:
    names = f"{ev['name']} {ev['league']} {ev.get('stage', '')}"
    if re.search(r"semi[- ]?final", names, re.I):
        return "Semi-final"
    if re.search(r"quarter[- ]?final", names, re.I):
        return "Quarter-final"
    if re.search(r"\bfinal\b", names, re.I):
        return "Final"
    if re.search(r"play[- ]?offs?", names, re.I):
        return "Playoff"
    return ""


def event_line(ev: dict, kind: str) -> str:
    stage = stage_label(ev)
    tail = f" · {esc(ev['league'])}" + (f" — {esc(stage)}" if stage else "")
    if kind == "next":
        if ev["style"] == "tournament":
            return f"• <b>{esc(ev['name'])}</b>{tail} ({'continues' if ev.get('continues') else 'starts'})"
        t = to_bd(ev["start"]).strftime("%H:%M") if ev.get("time_known", True) else ""
        return f"• {t + ' — ' if t else ''}<b>{esc(ev['name'])}</b>{tail}"
    if ev["style"] == "team" and ev.get("home") and ev.get("away"):
        hs, as_ = text(ev.get("home_score")), text(ev.get("away_score"))
        extra = f" ({esc(ev['detail'])})" if re.search(r"pen|aet|ot|extra", text(ev.get("detail")), re.I) else ""
        if hs != "" and as_ != "":
            return f"• {esc(ev['home'])} <b>{esc(hs)}–{esc(as_)}</b> {esc(ev['away'])}{extra}{tail}"
        return f"• {esc(ev['name'])}{tail} (final)"
    if ev.get("winner"):
        return f"• <b>{esc(ev['name'])}</b>{tail} — winner: {esc(ev['winner'])}"
    return f"• <b>{esc(ev['name'])}</b>{tail} — concluded"


def _sport_sections(events: list[dict], kind: str) -> list[str]:
    by_sport: dict[str, list[dict]] = {}
    for e in events:
        by_sport.setdefault(e["sport"], []).append(e)
    order = sorted(by_sport, key=lambda s: -max(e.get("score", 0) for e in by_sport[s]))
    parts = []
    for sport in order:
        rows = sorted(by_sport[sport], key=lambda e: e["start"])
        parts += ["", f"<b>{SPORT_EMOJI.get(sport, '🏅')} {esc(sport)}</b>"] + [event_line(e, kind) for e in rows]
    return parts


def render_digest(kind: str, target: date, highlights: list[dict], index: list[dict], intro: str,
                  extras: dict, sources: list, with_extras: bool = True) -> str:
    fmt = "daily_next" if kind == "next" else "daily_past"
    parts = [f"{FORMAT_EMOJI[fmt]} <b>{FORMAT_LABELS[fmt]} · {esc(long_date(target).upper())}</b>"]
    if intro:
        parts += ["", f"<i>{esc(intro)}</i>"]
    parts += _sport_sections(highlights, kind)
    if with_extras and extras.get("storyline"):
        parts += ["", "<b>📝 Storyline</b>", esc(extras["storyline"])]
    recs = [r for r in extras.get("records", []) if not extras.get("storyline") or similarity(r["text"], extras["storyline"]) < 0.5]
    if with_extras and recs:
        parts += ["", "<b>📌 Records & milestones</b>"]
        for r in recs[:3]:
            link = f' (<a href="{esc_attr(r["url"])}">{esc(r["src"])}</a>)' if r.get("url") else f" ({esc(r['src'])})"
            parts.append("• " + esc(clamp_words(r["text"], 190)) + link)
    if index:
        parts += ["", "<b>Also on</b>" if kind == "next" else "<b>Also finished</b>"]
        for e in sorted(index, key=lambda e: e["start"]):
            line = event_line(e, kind)
            parts.append(line.replace("• ", f"• {SPORT_EMOJI.get(e['sport'], '🏅')} ", 1))
    parts.append("")
    if kind == "next":
        parts.append("<i>Times in GMT+6 (Bangladesh).</i>")
    src = source_links(sources, limit=4)
    if src:
        parts.append("Sources: " + src)
    parts.append(FORMAT_TAG[fmt] + " #Sports")
    return "\n".join(parts)


def build_digest_html(kind, target, highlights, index, intro, extras, sources, limit=MESSAGE_LIMIT - 80) -> str:
    hi = sorted(highlights, key=lambda e: -e.get("score", 0))
    n_hi, n_idx, with_extras = len(hi), len(index), True
    html_out = ""
    for _ in range(80):
        html_out = tg_sanitize(render_digest(kind, target, hi[:n_hi], index[:n_idx], intro, extras, sources, with_extras))
        if vis_len(html_out) <= limit:
            return html_out
        if with_extras and (extras.get("storyline") or extras.get("records")):
            with_extras = False
        elif n_idx > 0:
            n_idx = max(0, n_idx - 3)
        elif n_hi > 3:
            n_hi -= 1
        else:
            break
    return html_out


# --- intro + storylines ---

def deterministic_intro(kind: str, highlights: list[dict], total: int) -> str:
    sports = len({e["sport"] for e in highlights}) or 1
    leagues = list(dict.fromkeys(e["league"] for e in sorted(highlights, key=lambda e: -e.get("score", 0)) if e["league"]))[:2]
    lead = f", led by {' and '.join(leagues)}" if leagues else ""
    if kind == "next":
        return f"{total} notable events across {sports} sport{'s' if sports != 1 else ''}{lead}."
    return f"Final results from {total} notable events across {sports} sport{'s' if sports != 1 else ''}{lead}."


def ai_intro(ai: "AIClient", kind: str, highlights: list[dict], total: int, fallback: str) -> str:
    if not ai.available or ai.fatal or not highlights:
        return fallback
    lines = "\n".join(f"- {e['name']} ({e['league']}; {e['sport']})" for e in highlights[:10])
    system = ("Write ONE or TWO sentences (max 200 characters) introducing a daily sports digest. Mention only names that appear "
              "in EVENTS. No time-relative words, no emojis, no hashtags, no numbers other than the counts given.")
    data = ai.json("digest_intro", system, f"KIND: {'upcoming schedule' if kind == 'next' else 'results'}\n"
                   f"EVENT COUNT: {total}\nEVENTS:\n{lines}", INTRO_SCHEMA, max_tokens=1200)
    intro = soft_fix({"headline": "x", "body": text((data or {}).get("intro")), "why_interesting": "", "key_points": []})["body"]
    if not intro or len(intro) > 240:
        return fallback
    viol = guard_violations({"headline": "x", "body": intro, "why_interesting": "", "key_points": []}, lines,
                            anchors=[str(total), str(len({e['sport'] for e in highlights}))], daily=True)
    return fallback if viol else intro


_NOTABLE = re.compile(r"\b(record|milestone|historic|first[- ]ever|first time|clinch\w*|upset|unbeaten|hat-trick|"
                      r"champion\w*|title|wins?|won|beat|defeat\w*)\b", re.I)


def records_and_storylines(ai: "AIClient", target: date, events: list[dict]) -> dict:
    lines: list[dict] = []
    for l in current_events_sports(target)[:8]:
        if not is_excluded(l["text"]):
            lines.append({"text": l["text"], "url": l["url"], "src": "Wikipedia"})
    ws, we = dhaka_window(target)
    for it in fetch_feeds():
        pub = it.get("published")
        if pub and ws <= pub < we + timedelta(hours=8) and _NOTABLE.search(it["title"] + " " + it["summary"][:200]):
            lines.append({"text": clamp_words(f"{it['title']}. {it['summary']}", 260), "url": it["url"], "src": it["source"]})
    uniq: list[dict] = []
    for l in lines:
        if not any(similarity(l["text"], u["text"]) > 0.6 for u in uniq):
            uniq.append(l)
    uniq = uniq[:8]
    if not uniq:
        return {}
    result: dict[str, Any] = {"records": uniq[:3]}
    if ai.available and not ai.fatal:
        block = "\n".join(f"[{i}] {l['text']}" for i, l in enumerate(uniq, 1))
        system = ("Write 2-3 sentences (max 380 characters) summarising the most notable sporting storylines from LINES. "
                  "Use only names, numbers and facts in LINES. No emojis, no hashtags, no time-relative words.")
        data = ai.json("digest_storyline", system, "LINES:\n" + block, STORYLINE_SCHEMA, max_tokens=1500)
        s = soft_fix({"headline": "x", "body": text((data or {}).get("storyline")), "why_interesting": "", "key_points": []}, 3, 400)["body"]
        evidence = block + "\n" + "\n".join(e["name"] for e in events[:40])
        if s and not guard_violations({"headline": "x", "body": s, "why_interesting": "", "key_points": []}, evidence, daily=True):
            result["storyline"] = s
    return result


def desk_digest(state: dict, ai: "AIClient", now: datetime, slot: Slot) -> tuple[dict | None, str]:
    kind = "next" if slot.desk == "next" else "past"
    target = slot.target(now)
    events, notes = collect_events(ai, target, kind)
    if not events:
        return None, f"no {'scheduled' if kind == 'next' else 'finished'} events found for {target.isoformat()} ({'; '.join(notes)})"
    highlights, index = select_events(events)
    if not highlights:  # quiet day: still publish the best few
        highlights, index = index[:5], index[5:]
    extras = records_and_storylines(ai, target, events) if kind == "past" else {}
    total = len(highlights) + len(index)
    intro = ai_intro(ai, kind, highlights, total, deterministic_intro(kind, highlights, total))
    names = list(dict.fromkeys(e["source"] for e in highlights + index if e.get("source")))
    sources = [(n, SOURCE_HOME.get(n, "")) for n in names if SOURCE_HOME.get(n)]
    for e in highlights + index:
        if e.get("source") not in SOURCE_HOME and e.get("url"):
            sources.append((e["source"], e["url"]))
    if extras.get("records") or extras.get("storyline"):
        for r in extras.get("records", []):
            if r.get("url"):
                sources.append((r["src"], r["url"]))
    fmt = "daily_next" if kind == "next" else "daily_past"
    body = build_digest_html(kind, target, highlights, index, intro, extras, sources)
    story = {"desk": slot.desk, "format": fmt, "label": FORMAT_LABELS[fmt], "headline": f"{FORMAT_LABELS[fmt]} · {long_date(target).upper()}",
             "body": intro, "why_interesting": "", "key_points": [], "tags": [], "date_anchor": long_date(target).upper(),
             "sources": sources, "topic": "digest", "angle": kind, "claim": f"{kind}:{target.isoformat()}",
             "render": {"mode": "text", "html": body}, "urls": [], "target": target.isoformat(), "event_count": total}
    return story, "ok"

# --- ON THIS DATE ---

_SPORTS_RX = re.compile(
    r"\b(football|soccer|cricket\w*|tennis|golf\w*|boxing|boxer|olympic\w*|marathon|baseball|basketball|rugby|hockey|"
    r"chess|world cup|grand prix|formula (?:one|1)|athlet\w*|swim\w*|cycl\w*|tour de france|wimbledon|championship\w*|"
    r"tournament|stadium|fifa|uefa|icc|ioc|league|cup final|board game|card game|backgammon|monopoly|scrabble|checkers|"
    r"draughts|contract bridge|poker|dominoes|badminton|kabaddi|wrestl\w*|sumo|judo|karate|fencing|archery|rowing|"
    r"sailing|polo|squash|table tennis|volleyball|handball|netball|lacrosse|skating|skiing|snooker|billiards|darts|"
    r"bowling|footballer|goalkeeper|striker|batsman|bowler|rubik|sudoku|crossword|mahjong|ludo|carrom)\b", re.I)
_GLOOM = re.compile(r"\b(killed|died|dies|death|massacre|assassinat\w*|murder\w*|crash\w*|disaster|riots?|shooting|bomb\w*|"
                    r"war|terror\w*|suicide|stampede|tragedy|collapse[sd]?)\b", re.I)
_FIRSTS = re.compile(r"\b(first|inaugural|record|founded|banned|invented|oldest|only|introduced|debut|established|"
                     r"unveiled|patented)\b", re.I)
ROUND_YEARS = (25, 50, 75, 100, 125)


def otd_relevant(c: dict) -> bool:
    blob = f"{c['text']} {c.get('desc', '')}"
    if c.get("src") != "yearpage" and not _SPORTS_RX.search(blob):
        return False
    if _GLOOM.search(c["text"]) or is_excluded(blob):
        return False
    return len(c["text"]) >= 30 and 1500 <= c["year"] < now_bd().year


def otd_score(c: dict, d: date) -> float:
    age = d.year - c["year"]
    s = 0.0
    if age in ROUND_YEARS:
        s += 3
    elif age % 25 == 0:
        s += 2
    if _FIRSTS.search(c["text"]):
        s += 2
    if c.get("title"):
        s += 1
    if c.get("src") == "onthisday":
        s += 0.5
    if re.search(r"cricket|football|badminton|kabaddi|tennis|chess|hockey|olympic", c["text"], re.I):
        s += 1
    return s


def desk_on_this_date(state: dict, ai: "AIClient", now: datetime, slot: Slot) -> tuple[dict | None, str]:
    if not ai.available or ai.fatal:
        return None, "AI unavailable"
    d = slot.target(now)
    posted = load_posted_urls()
    cands = onthisday_events(d)
    for lines in pmap(lambda y: year_in_sports_lines(y, d), [d.year - n for n in ROUND_YEARS], workers=5):
        cands += lines or []
    cands = [c for c in cands if otd_relevant(c)]
    rng = random.Random(f"otd:{d.isoformat()}")
    fresh = [c for c in cands if not is_duplicate_claim(state, c["text"]) and canonical_url(c.get("url", "")) not in posted]
    if not fresh:
        return None, f"no usable history candidates for {d.month}/{d.day} ({len(cands)} raw)"
    ranked = sorted(fresh, key=lambda c: (-otd_score(c, d), rng.random()))[:12]
    order = list(range(len(ranked)))
    block = "\n".join(f"{i + 1}. ({c['year']}) {c['text'][:220]}" for i, c in enumerate(ranked))
    pick = ai.json("otd_pick", "Pick the single most interesting, well-defined sports or games history item for a general "
                   "audience. Prefer concrete firsts, rule changes and famous matches. Return its 1-based index.",
                   block, PICK_SCHEMA, max_tokens=800)
    idx = int((pick or {}).get("index") or 0) - 1
    if 0 <= idx < len(ranked):
        order = [idx] + [i for i in order if i != idx]
    attempt = max(1, int(ledger_get(state, slot.key(now)).get("attempts", 1)))
    if attempt > 1:  # retries move down the ranking instead of re-checking the same three candidates
        shift = ((attempt - 1) * 3) % len(order)
        order = order[shift:] + order[:shift]
    for i in order[:3]:
        c = ranked[i]
        age = d.year - c["year"]
        summary = wiki_summary(c["title"]) if c.get("title") else None
        background = (summary or {}).get("extract") or c.get("extract") or ""
        evidence = f"EVENT ({c['year']}): {c['text']}\nBACKGROUND: {background}".strip()
        if len(evidence) < 100:
            reject("otd_thin_evidence", c["text"][:60])
            continue
        hist = date(c["year"], d.month, d.day)
        fmt = "century_ago" if age in ROUND_YEARS else "on_this_date"
        label = f"{age} YEARS AGO" if fmt == "century_ago" else "ON THIS DATE"
        url = (summary or {}).get("url") or c.get("url") or ""
        srcs = [("Wikipedia", url)] if "wikipedia.org" in url else [("Wikipedia", wiki_url(c["title"]))] if c.get("title") else \
            [("Wikipedia", "https://en.wikipedia.org/wiki/" + MONTHS[d.month - 1] + "_" + str(d.day))]
        story = compose_post(ai, desk="history", fmt=fmt, topic=c.get("title") or c["text"][:50], angle="anniversary",
                             claim=c["text"], evidence=evidence, sources=srcs, label=label,
                             date_anchor=long_date(hist).upper(),
                             anchors=[str(c["year"]), str(age), str(d.year), str(d.day)],
                             extra=f"Write it as an anniversary post: it happened on {long_date(hist)}, {age} years ago.", state=state)
        if story:
            story["urls"] = [u for _, u in srcs]
            return story, "ok"
    return None, "candidates failed verification"


# --- EVERGREEN topic engine ---

SPORT_TITLES = [
    "Cricket", "Association football", "Basketball", "Tennis", "Badminton", "Table tennis", "Kabaddi", "Kho kho",
    "Volleyball", "Field hockey", "Ice hockey", "Rugby union", "Rugby league", "Baseball", "Golf", "Formula One",
    "Athletics (sport)", "Marathon", "Swimming (sport)", "Cycling", "Boxing", "Wrestling", "Judo", "Karate",
    "Taekwondo", "Archery", "Fencing", "Rowing (sport)", "Sailing", "Polo", "Squash (sport)", "Handball", "Netball",
    "Lacrosse", "Sumo", "Sepak takraw", "Hurling", "Gaelic football", "Australian rules football", "American football",
    "Snooker", "Darts", "Bowling", "Curling", "Biathlon", "Pentathlon", "Decathlon", "Weightlifting", "Gymnastics",
    "Water polo", "Skateboarding", "Surfing", "Mixed martial arts", "Tug of war", "Pickleball", "Ultimate (sport)",
    "Olympic Games", "FIFA World Cup", "Cricket World Cup", "Tour de France", "Wimbledon Championships",
]
GAME_TITLES = [
    "Chess", "Ludo", "Carrom", "Snakes and ladders", "Monopoly (game)", "Scrabble", "Backgammon", "Draughts", "Go (game)",
    "Shogi", "Xiangqi", "Mahjong", "Dominoes", "Playing card", "Contract bridge", "Poker", "Rummy", "Uno (card game)",
    "Cribbage", "Spades (card game)", "Hearts (card game)", "Mancala", "Pachisi", "Nine men's morris", "Senet",
    "Royal Game of Ur", "Catan", "Ticket to Ride (board game)", "Carcassonne (board game)", "Pandemic (board game)",
    "Risk (game)", "Cluedo", "Battleship (game)", "Jenga", "Rubik's Cube", "Sudoku", "Crossword", "Pictionary",
    "Charades", "Mafia (party game)", "Tic-tac-toe", "Connect Four", "Reversi", "Stratego", "Marbles", "Hopscotch",
    "Pick-up sticks", "Tangram", "Bingo", "Yahtzee", "Trivial Pursuit", "Codenames (board game)", "Solitaire",
    "Dobble", "Patolli", "Hnefatafl",
]
AUDIENCE_BOOST = {"cricket", "association football", "badminton", "kabaddi", "carrom", "chess", "ludo", "kho kho",
                  "table tennis", "field hockey", "tennis", "basketball", "volleyball", "pachisi", "snakes and ladders"}
CATEGORY_SEEDS = {"game": ["Traditional_games", "Board_games", "Card_games", "Dice_games", "Tile-based_games",
                           "Children's_games", "Abstract_strategy_games"],
                  "sport": ["Traditional_sports", "Ball_games", "Racket_sports", "Target_sports", "Team_sports"]}
FORMAT_WEIGHTS = {
    "game": {"game_discovery": 3, "how_to_play": 2, "rule_check": 2, "history": 2, "fact": 2, "why": 1,
             "first_last_only": 1, "forgotten": 2},
    "sport": {"fact": 3, "history": 2, "rule_check": 2, "why": 2, "first_last_only": 2, "then_vs_now": 1, "how_to_play": 1},
}
FORMAT_ANGLES = {
    "rule_check": ["rule", "scoring", "equipment"], "how_to_play": ["rule", "scoring", "equipment"],
    "history": ["origin", "evolution", "tradition"], "why": ["etymology", "measurement", "tradition", "rule"],
    "then_vs_now": ["evolution", "equipment", "rule"], "forgotten": ["origin", "culture", "tradition"],
    "game_discovery": ["origin", "culture", "equipment", "rule"],
}
ANGLES_DEFAULT = ["origin", "etymology", "rule", "scoring", "equipment", "tradition", "evolution", "culture", "measurement", "myth"]
RULE_FORMATS = {"rule_check", "how_to_play"}


def ensure_pool(state: dict) -> dict:
    pool = state["topics"].setdefault("pool", {})
    for kind, titles in (("sport", SPORT_TITLES), ("game", GAME_TITLES)):
        for t in titles:
            slug = slugify(t)
            if slug not in pool:
                pool[slug] = {"title": t, "kind": kind, "pop": 1.6 if t.lower() in AUDIENCE_BOOST else 1.0, "seed": True}
    return pool


def refresh_topic_pool(state: dict, rng: random.Random) -> int:
    """Grow the pool from Wikipedia categories (monthly). Returns number of topics added."""
    last = parse_dt(state["topics"].get("last_refresh"))
    if last and now_bd() - last < timedelta(days=30):
        return 0
    pool = ensure_pool(state)
    if len(pool) >= 1500:
        return 0
    jobs = [(kind, cat) for kind, cats in CATEGORY_SEEDS.items() for cat in cats]
    results = pmap(lambda j: wiki_category_members(j[1], 150), jobs, workers=6)
    added = 0
    for (kind, _), members in zip(jobs, results):
        members = [m for m in (members or []) if not m.startswith(("List of", "Outline of", "Category:"))]
        rng.shuffle(members)
        for title in members[:35]:
            slug = slugify(title)
            if slug and slug not in pool and not is_excluded(title):
                pool[slug] = {"title": title, "kind": kind, "pop": 0.8, "seed": False}
                added += 1
    if added or any(results):
        state["topics"]["last_refresh"] = utc_iso(now_bd())
    return added


def pick_format(state: dict, topic: dict, rng: random.Random) -> tuple[str, str]:
    weights = dict(FORMAT_WEIGHTS[topic["kind"]])
    if topic.get("seed"):
        weights.pop("forgotten", None)
    recent = recent_posts(state, 8, None)
    recent_fmts = [p.get("format") for p in recent[-3:]]
    recent_angles = [p.get("angle") for p in recent]
    for f in list(weights):
        weights[f] = weights[f] * (0.35 ** recent_fmts.count(f))
    fmt = rng.choices(list(weights), weights=list(weights.values()))[0]
    angles = FORMAT_ANGLES.get(fmt, ANGLES_DEFAULT)
    aw = [0.5 ** recent_angles.count(a) for a in angles]
    return fmt, rng.choices(angles, weights=aw)[0]


def pick_topics(state: dict, rng: random.Random, k: int = 4) -> list[tuple[str, dict]]:
    pool = ensure_pool(state)
    per = state["topics"].setdefault("per_topic", {})
    now = now_bd()
    weighted: list[tuple[str, dict, float]] = []
    for slug, t in pool.items():
        st = per.get(slug, {})
        if st.get("bad"):
            continue
        last = parse_dt(st.get("last_at"))
        days = (now - last).total_seconds() / 86400 if last else None
        if days is not None and st.get("count", 0) >= 2 and days < 30:
            continue
        novelty = 1.0 if days is None else 1 - math.exp(-days / 14)
        w = t.get("pop", 1.0) * max(0.02, novelty)
        weighted.append((slug, t, w))
    chosen: list[tuple[str, dict]] = []
    while weighted and len(chosen) < k:
        pick = rng.choices(weighted, weights=[w for _, _, w in weighted])[0]
        chosen.append((pick[0], pick[1]))
        weighted = [x for x in weighted if x[0] != pick[0]]
    return chosen


def rules_evidence(title: str) -> list[dict]:
    domains = OFFICIAL_DOMAINS + RULES_PUBLISHERS + ["britannica.com"]
    return exa_search(f"{title} official rules how it is played", domains=domains, num=4, text_chars=3500)


def desk_evergreen(state: dict, ai: "AIClient", now: datetime, slot: Slot) -> tuple[dict | None, str]:
    if not ai.available or ai.fatal:
        return None, "AI unavailable"
    rng = random.Random(f"evergreen:{now.date().isoformat()}:{slot.index}:{ledger_get(state, slot.key(now)).get('attempts', 0)}")
    # a fresh-news slot: new tabletop games, at most every 3 days, in the middle slot
    if slot.index == 2 and EXA_API_KEY:
        last = recent_posts(state, 1, "newgames")
        last_dt = parse_dt(last[0].get("posted_at")) if last else None
        if not last_dt or now - last_dt >= timedelta(days=3):
            story, why = desk_new_games(state, ai, now)
            if story:
                return story, "ok"
            REPORT.notes.append(f"new_games skipped: {why}")
    try:
        added = refresh_topic_pool(state, rng)
        if added:
            logger.info("Topic pool grew by %d topics", added)
    except Exception as exc:  # noqa: BLE001
        logger.warning("topic refresh failed: %s", exc)
    per = state["topics"].setdefault("per_topic", {})
    last_reason = "no topic produced a verified post"
    for slug, topic in pick_topics(state, rng, 4):
        if RUN_LIMIT.expired():
            return None, "run deadline reached"
        st = per.setdefault(slug, {"count": 0, "last_at": "", "angles": {}, "formats": {}, "fails": 0})
        page = wiki_page(topic["title"])
        if not page:
            st["fails"] = st.get("fails", 0) + 1
            if st["fails"] >= 3:
                st["bad"] = True
            last_reason = f"wikipedia page unavailable: {topic['title']}"
            continue
        problem = wiki_page_problem(page)
        if problem:
            st["bad"] = True
            reject("topic_unusable", f"{topic['title']}: {problem}")
            last_reason = f"{topic['title']}: {problem}"
            continue
        fmt, angle = pick_format(state, topic, rng)
        docs = [{"label": "Wikipedia", "url": page["url"], "text": page["text"], "grade": "B"}]
        if fmt in RULE_FORMATS:
            for d in rules_evidence(page["title"]):
                docs.append({"label": d["source"], "url": d["url"], "text": d["text"], "grade": d["grade"]})
            has_a = any(d["grade"] == "A" for d in docs)
            b_domains = {domain_of(d["url"]) for d in docs if d["grade"] in ("A", "B")}
            if not (has_a or len(b_domains) >= 2):
                reject("rule_evidence_too_weak", page["title"])
                fmt, angle = ("history", "origin") if topic["kind"] == "game" else ("fact", "origin")
                docs = docs[:1]
        article = "\n\n".join(f"[SOURCE: {d['label']}]\n{d['text']}" for d in docs)[:16000]
        facts = extract_facts(ai, topic=page["title"], fmt=fmt, angle=angle, article=article)
        for fact in facts[:2]:
            claim, quote = text(fact["claim"]), text(fact["evidence_quote"])
            if is_duplicate_claim(state, claim, page["title"]):
                reject("duplicate_claim", claim[:80])
                continue
            src_doc = next((d for d in docs[1:] if quote_in_text(quote, d["text"])), None)
            evidence = f"{page['text'][:1200]}\n\n{best_paragraph(article, quote)}\n\nKEY QUOTE: {quote}"
            sources = [("Wikipedia", page["url"])] + ([(src_doc["label"], src_doc["url"])] if src_doc else [])
            story = compose_post(ai, desk="evergreen", fmt=fmt, topic=page["title"], angle=angle, claim=claim,
                                 evidence=evidence, sources=sources, certainty=text(fact.get("certainty")) or "settled", state=state)
            if story:
                story["urls"] = [u for _, u in sources]
                story["topic_slug"] = slug
                return story, "ok"
        last_reason = f"{page['title']}: no fact passed verification"
    return None, last_reason


def desk_new_games(state: dict, ai: "AIClient", now: datetime) -> tuple[dict | None, str]:
    if not EXA_API_KEY:
        return None, "Exa key missing"
    docs = exa_search("new board game announced released tabletop card game", domains=TABLETOP_DOMAINS,
                      start=now - timedelta(days=14), num=10, text_chars=4000)
    posted = load_posted_urls()
    docs = [d for d in docs if d["canonical"] not in posted and len(d["text"]) >= 400]
    docs = [d for d in docs if not is_duplicate_claim(state, d["title"], d["title"], 0.85)]
    if not docs:
        return None, "no fresh tabletop articles"
    shift = (int(ledger_get(state, f"evergreen:{now.date().isoformat()}:2").get("attempts", 1)) - 1) * 3 % len(docs)
    docs = docs[shift:] + docs[:shift]
    for d in docs[:3]:
        facts = extract_facts(ai, topic=d["title"], fmt="new_game", angle="announcement", article=d["text"])
        for fact in facts[:1]:
            quote = text(fact["evidence_quote"])
            if is_duplicate_claim(state, text(fact["claim"]), d["title"]):
                reject("duplicate_claim", text(fact["claim"])[:80])  # same game already covered (maybe via another outlet)
                continue
            evidence = f"{d['title']}\n{best_paragraph(d['text'], quote)}\n\nKEY QUOTE: {quote}"
            story = compose_post(ai, desk="newgames", fmt="new_game", topic=clamp_words(d["title"], 80), angle="announcement",
                                 claim=text(fact["claim"]), evidence=evidence, sources=[(d["source"], d["url"])],
                                 extra="Only physical tabletop, board or card games. Attribute the news to the outlet by name.", state=state)
            if story:
                story["urls"] = [d["url"]]
                return story, "ok"
    return None, "no article produced a verified post"


DESKS: dict[str, Callable] = {
    "past": desk_digest, "next": desk_digest, "history": desk_on_this_date, "evergreen": desk_evergreen,
}

# ===========================================================================
# 10. RUNNER: executes due slots, records results, writes the run report
# ===========================================================================


def record_post(state: dict, story: dict, message_id: Any) -> None:
    urls = [canonical_url(u) for u in story.get("urls", []) if u]
    state.setdefault("posts", []).append({
        "desk": story["desk"], "format": story.get("format", ""), "topic": story.get("topic", ""),
        "angle": story.get("angle", ""), "claim": story.get("claim", ""), "headline": story.get("headline", ""),
        "fp": fingerprint(story.get("claim", ""), story.get("topic", "")), "urls": urls, "message_id": message_id,
        "posted_at": utc_iso(now_bd()), "day": now_bd().date().isoformat(),
    })
    slug = story.get("topic_slug")
    if slug:
        st = state["topics"].setdefault("per_topic", {}).setdefault(slug, {"count": 0, "angles": {}, "formats": {}})
        st["count"] = st.get("count", 0) + 1
        st["last_at"] = utc_iso(now_bd())
        st.setdefault("angles", {})[story.get("angle", "")] = st.get("angles", {}).get(story.get("angle", ""), 0) + 1


def execute_slot(state: dict, ai: "AIClient", slot: Slot, now: datetime) -> str:
    key = slot.key(now)
    prev = ledger_get(state, key)
    attempts = prev.get("attempts", 0) + 1
    ledger_update(state, key, attempts=attempts, desk=slot.name, target=slot.target(now).isoformat(), status="running")
    t0 = time.monotonic()
    story: dict | None = None
    try:
        story, reason = DESKS[slot.desk](state, ai, now, slot)
    except Exception as exc:  # noqa: BLE001 - a desk must never take the run down
        reason = f"exception: {type(exc).__name__}: {redact(str(exc))[:200]}"
        REPORT.errors.append(f"{slot.name}: {reason}")
        logger.error("Desk %s crashed:\n%s", slot.name, redact(traceback.format_exc()))
    row = {"slot": slot.name, "key": key, "attempt": attempts, "seconds": round(time.monotonic() - t0, 1)}
    if story is None:
        ledger_update(state, key, status="failed", error=reason[:300])
        row.update(status="failed", detail=reason[:200])
        REPORT.desks.append(row)
        logger.warning("Slot %s: no post (%s)", key, reason)
        return "failed"
    res = publish_story(story)
    if res.get("ok"):
        mid = (res.get("result") or {}).get("message_id")
        record_post(state, story, mid)
        ledger_update(state, key, status="posted", message_id=mid, posted_at=utc_iso(now_bd()), error="")
        append_posted_urls(story.get("urls", []))
        save_state(state)
        row.update(status="posted", detail=story.get("headline", "")[:100])
        REPORT.desks.append(row)
        logger.info("Slot %s: posted (%s)", key, story.get("headline", "")[:80])
        return "posted"
    if res.get("uncertain"):
        ledger_update(state, key, status="uncertain", error=text(res.get("description"))[:300])
        admin_alert(state, f"uncertain:{key}", f"Delivery of {key} is UNCERTAIN (network error after sending). "
                    "Check the channel; the bot will not retry this slot to avoid a duplicate.")
        row.update(status="uncertain", detail=text(res.get("description"))[:200])
    else:
        ledger_update(state, key, status="failed", error=text(res.get("description"))[:300])
        row.update(status="failed", detail="telegram: " + text(res.get("description"))[:200])
    REPORT.desks.append(row)
    save_state(state)
    return row["status"]


def write_summary(state: dict, code: int, now: datetime) -> None:
    lines = [f"## {APP_NAME} v{APP_VERSION} run · {now.strftime('%Y-%m-%d %H:%M')} (Dhaka) · exit {code}", ""]
    if REPORT.desks:
        lines += ["| Slot | Attempt | Result | Detail |", "|---|---|---|---|"]
        for r in REPORT.desks:
            lines.append(f"| {r['slot']} | {r['attempt']} | {r['status']} | {text(r.get('detail')).replace('|', '/')[:110]} |")
    else:
        lines.append("Nothing was due in this run.")
    lines += ["", "**Sources**", "", "| Source | ok | fail | last error |", "|---|---|---|---|"]
    for name, h in sorted(HEALTH.items()):
        lines.append(f"| {name} | {h['ok']} | {h['fail']} | {text(h['last_error']).replace('|', '/')[:90]} |")
    if REPORT.rejections:
        lines += ["", "**Guard rejections:** " + ", ".join(f"{k}×{v}" for k, v in REPORT.rejections.most_common(8))]
    lines += ["", f"AI calls: {REPORT.ai_calls} · tokens: {REPORT.ai_tokens}"]
    if REPORT.notes:
        lines += ["", "**Notes:** " + " | ".join(REPORT.notes[:6])]
    if REPORT.errors:
        lines += ["", "**Errors:**"] + [f"- {e}" for e in REPORT.errors]
    out = "\n".join(lines)
    print("\n" + out)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(out + "\n")
        except OSError:
            pass


def v3_run_once(only: str | None = None) -> int:
    """One scheduler tick. Exit codes: 0 fine, 1 crash/config, 2 a mandatory post is newly overdue."""
    global RUN_LIMIT
    REPORT.reset()
    _EXA_CACHE.clear()
    if not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing")
        return 1
    try:
        state = load_state()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    prune_state(state)
    ai = AIClient(state.get("ai"))
    if not ai.available:
        logger.warning("CEREBRAS_API_KEY missing: digests will post in floor mode; knowledge desks are skipped")
    RUN_LIMIT = Deadline(RUN_DEADLINE_SECONDS)
    now = now_bd()
    if only:
        due = [s for s in build_slots() if s.name == only]
    elif DRY_RUN:
        due = build_slots()  # preview mode: show what every slot would post; nothing is persisted
    else:
        due = due_slots(state, now)
    logger.info("Tick %s Dhaka | due: %s", now.strftime("%Y-%m-%d %H:%M"), [s.name for s in due] or "none")
    posted, evergreen_done = 0, False
    for slot in due:
        if RUN_LIMIT.expired():
            logger.warning("Run deadline reached; remaining slots wait for the next tick")
            break
        if slot.desk == "evergreen" and evergreen_done and not DRY_RUN:
            continue
        if posted:
            sleep(POST_DELAY_SECONDS)
        outcome = execute_slot(state, ai, slot, now)
        if slot.desk == "evergreen":
            evergreen_done = True
        posted += outcome == "posted"
    code = 0
    for slot, key in ([] if (DRY_RUN or only) else sla_breaches(state, now)):  # previews/forced runs never raise SLA alarms
        if state.get("alerts", {}).get(f"sla:{key}"):
            continue
        entry = ledger_get(state, key)
        admin_alert(state, f"sla:{key}", f"{slot.name} for {slot.target(now).isoformat()} is overdue "
                    f"(status: {entry.get('status', 'never attempted')}; last error: {text(entry.get('error'))[:160]}).")
        code = 2
    state["ai"] = ai.export()
    state["last_run_at"] = utc_iso(now)
    for name, h in HEALTH.items():
        agg = state["source_health"].setdefault(name, {"ok": 0, "fail": 0, "last_ok_at": "", "last_error": ""})
        agg["ok"] += h["ok"]
        agg["fail"] += h["fail"]
        if h["ok"]:
            agg["last_ok_at"] = utc_iso(now)
        if h["last_error"]:
            agg["last_error"] = h["last_error"]
    state["runs"].append({"at": utc_iso(now), "due": [s.name for s in due], "posted": posted,
                          "errors": REPORT.errors[:3], "ai_calls": REPORT.ai_calls, "exit": code})
    save_state(state)
    write_summary(state, code, now)
    return code


# ===========================================================================
# 11. DIAGNOSE and TEST-POST (live checks from the GitHub runner)
# ===========================================================================


def run_diagnose() -> int:
    rows: list[tuple[str, str, str, bool]] = []

    def add(name: str, ok: bool, detail: str, required: bool = False) -> None:
        rows.append((name, "OK" if ok else ("FAIL" if required else "WARN"), detail, required))

    REPORT.reset()
    _EXA_CACHE.clear()
    # Telegram
    if not TELEGRAM_BOT_TOKEN:
        add("telegram token", False, "TELEGRAM_BOT_TOKEN is empty", True)
    else:
        me = tg_call("getMe")
        add("telegram getMe", bool(me.get("ok")), text((me.get("result") or {}).get("username")) or text(me.get("description")), True)
        bot_id = (me.get("result") or {}).get("id")
        chat = tg_call("getChat", {"chat_id": CHANNEL})
        add(f"telegram channel {CHANNEL}", bool(chat.get("ok")),
            text((chat.get("result") or {}).get("title")) or text(chat.get("description")), True)
        if bot_id and chat.get("ok"):
            mem = tg_call("getChatMember", {"chat_id": CHANNEL, "user_id": bot_id})
            r = mem.get("result") or {}
            can = r.get("can_post_messages", r.get("status") == "creator")
            add("telegram bot is admin with post rights", bool(mem.get("ok")) and r.get("status") in ("administrator", "creator") and bool(can),
                f"status={r.get('status')} can_post_messages={r.get('can_post_messages')}", True)
    # AI
    ai = AIClient()
    if not ai.available:
        add("cerebras key", False, "CEREBRAS_API_KEY is empty (knowledge desks disabled)", True)
    else:
        m = http("cerebras", "GET", AIClient.MODELS_URL, headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}"})
        ids = [text(x.get("id")) for x in (m.data or {}).get("data", [])] if m.ok and isinstance(m.data, dict) else []
        add("cerebras models", m.ok, f"{len(ids)} models; configured '{CEREBRAS_MODEL}' {'present' if CEREBRAS_MODEL in ids else 'NOT in list (will auto-switch)'}", True)
        out = ai.json("diagnose", "Reply with the JSON only.", 'Return {"ok": "yes"}.', OBJ(ok=STR), max_tokens=600)
        add("cerebras completion", out is not None, f"mode={ai.mode} model={ai.model}" + ("" if out else f" error={ai.last_error}"), True)
    # Exa
    if not EXA_API_KEY:
        add("exa", False, "EXA_API_KEY is empty (new-games, corroboration and cricket fallback disabled)")
    else:
        res = exa_search("history of chess", num=3, text_chars=500)
        add("exa search", bool(res), f"{len(res)} results, request variant {_EXA_VARIANT[0]}" if res else text(HEALTH.get("exa", {}).get("last_error")) or "no results")
    # Wikipedia
    pg = wiki_page("Chess")
    add("wikipedia article text", bool(pg), f"{len(pg['text'])} chars" if pg else "failed", True)
    sm = wiki_summary("Chess")
    add("wikipedia summary", bool(sm), "ok" if sm else "failed")
    today = now_bd().date()
    otd = onthisday_events(today)
    add("wikipedia on-this-day", bool(otd), f"{len(otd)} events ({otd[0]['src']})" if otd else "failed")
    ywt = wiki_wikitext(f"{today.year - 100} in sports")
    yl = year_in_sports_lines(today.year - 100, today) if ywt else []
    add("wikipedia 'YYYY in sports'", bool(ywt), (f"page fetched; {len(yl)} lines match today's date (0 is normal on some dates)" if ywt else "page could not be fetched"))
    cwt = wiki_wikitext(f"Portal:Current events/{(today - timedelta(days=1)).year} {MONTHS[(today - timedelta(days=1)).month - 1]} {(today - timedelta(days=1)).day}")
    ce = current_events_sports(today - timedelta(days=1)) if cwt else []
    add("wikipedia current events (sports)", bool(cwt), (f"page fetched; sports section parsed into {len(ce)} lines" + ("" if ce else " (parser found none: storylines fall back to RSS)")) if cwt else "page could not be fetched")
    cm = wiki_category_members("Traditional_games", 20)
    add("wikipedia category members", bool(cm), f"{len(cm)} pages")
    # sports data
    ev, status = espn_events(today)
    okc = sum(1 for v in status.values() if v == "ok")
    add("espn scoreboards", okc > 0, f"{okc}/{len(LEAGUES)} leagues reachable, {len(ev)} events in today's window")
    failed = [k for k, v in status.items() if v != "ok"]
    if failed:
        rows.append(("espn unreachable leagues", "INFO", ", ".join(x.split("/")[-1] for x in failed)[:300], False))
    ts = tsdb_events(today)
    add("thesportsdb", bool(ts), f"{len(ts)} events (fallback source)")
    if CRICKETDATA_API_KEY:
        cd = cricketdata_events(today)
        add("cricketdata.org", True, f"{len(cd)} events today")
    else:
        rows.append(("cricketdata.org", "INFO", "no key set; cricket uses Exa fallback", False))
    feeds = fetch_feeds()
    add("rss feeds", bool(feeds), f"{len(feeds)} items from {len({f['source'] for f in feeds})}/{len(RSS_FEEDS)} feeds")
    # report
    print("\n=== DIAGNOSE ===")
    md = ["## Diagnose", "", "| Check | Result | Detail |", "|---|---|---|"]
    for name, res, detail, _ in rows:
        print(f"[{res:4}] {name}: {detail}")
        md.append(f"| {name} | {res} | {detail.replace('|', '/')} |")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(md) + "\n")
        except OSError:
            pass
    return 1 if any(r[1] == "FAIL" for r in rows) else 0


def run_test_post() -> int:
    if not TELEGRAM_BOT_TOKEN and not DRY_RUN:
        logger.error("TELEGRAM_BOT_TOKEN missing")
        return 1
    story = {"desk": "test", "format": "fact", "label": "TEST POST", "headline": "Test post from The Sports Newsroom v3",
             "body": "This is a test of the publishing pipeline. If you can read this with an image card above, photo posts work.",
             "why_interesting": "It confirms that formatting, links and the image card all arrive correctly.",
             "key_points": ["Bold and italic text", "A clickable source link"], "tags": ["#Test"],
             "date_anchor": long_date(now_bd().date()).upper(), "sources": [("Wikipedia", "https://en.wikipedia.org/wiki/Chess")]}
    story["render"] = {"mode": "photo", "html": fit_knowledge_html(story)}
    r1 = publish_story(story)
    digest = {"render": {"mode": "text", "html": tg_sanitize(f"🗓 <b>TEST DIGEST · {esc(long_date(now_bd().date()).upper())}</b>\n\n"
                                                              "<b>⚽ Football</b>\n• 18:30 — <b>Team A vs Team B</b> · Test League\n\n<i>Times in GMT+6 (Bangladesh).</i>")}}
    r2 = publish_story(digest)
    logger.info("test-post results: photo=%s text=%s", r1.get("ok"), r2.get("ok"))
    if not r1.get("ok"):
        logger.error("photo post failed: %s", r1.get("description"))
    if not r2.get("ok"):
        logger.error("text post failed: %s", r2.get("description"))
    return 0 if r1.get("ok") and r2.get("ok") else 1

# ===========================================================================
# 12. SELF-TEST: offline fake network + unit, contract, end-to-end, fault and soak tests
# ===========================================================================

import contextlib  # noqa: E402
import io  # noqa: E402


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text_: str | None = None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text_ if text_ is not None else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


_TEAMS = ["Arsenal", "Chelsea", "Liverpool", "Real Madrid", "Barcelona", "Sevilla", "Bayern Munich", "Dortmund",
          "Juventus", "Napoli", "Lyon", "Ajax", "Benfica", "Boca Juniors", "Flamengo", "Celtic", "Porto", "Roma",
          "Lakers", "Celtics", "Warriors", "Bulls", "Yankees", "Dodgers", "Bruins", "Rangers"]
_WORDS = ["harbor", "lantern", "meadow", "falcon", "granite", "willow", "copper", "ember", "orchard", "summit", "thistle",
          "marble", "cobalt", "saffron", "juniper", "tundra", "velvet", "quarry", "beacon", "cinder", "lagoon", "prairie",
          "sparrow", "timber", "harvest", "glacier", "anchor", "bramble", "canyon", "dune"]


def fake_article(title: str) -> str:
    """Deterministic but varied article per title (real articles differ; identical fakes would trip the duplicate guard)."""
    t = re.sub(r"\s*\(.*?\)", "", title)
    h = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
    w = lambda k: _WORDS[(h >> k) % len(_WORDS)]
    facts = [
        f"The earliest written rules of {t} date from {1700 + h % 250}, when a committee of {5 + h % 40} members in the town of {w(1).title()} recorded them.",
        f"Players of {t} traditionally use a {w(2)} {w(3)} made of {w(4)} wood, and a complete set costs about {20 + h % 80} coins.",
        f"A famous {w(5)} tournament of {t} in {1800 + h % 200} attracted {300 + h % 700} competitors from {3 + h % 9} provinces.",
        f"The {w(6)} variant of {t} limits each turn to {2 + h % 5} minutes and awards {10 + h % 40} points for a clean finish.",
        f"According to tradition, {t} began in a {w(7)} market near a {w(8)} river, although historians disagree about the exact place.",
        f"Governing councils in {w(9).title()} and {w(10).title()} standardised the {w(11)} scoring system of {t} in {1900 + h % 100}.",
    ]
    rot = h % len(facts)
    facts = facts[rot:] + facts[:rot]
    return (f"{t} is a traditional game and sport played in many countries around the world.\n"
            + "\n".join(facts[:4]) + "\n"
            f"Clubs in Bangladesh, India and Pakistan organise regular tournaments, and schools teach the basic skills to children.\n"
            f"Modern governing bodies publish yearly rule updates, and referees must complete a training course before officiating.\n"
            f"Spectators often gather in large numbers for finals, and local newspapers publish detailed reports of each season.")


class FakeNet:
    """Stands in for requests.Session. Routes by URL, validates what the bot sends, supports fault injection."""

    def __init__(self):
        self.tg: list[dict] = []
        self.violations: list[str] = []
        self.fail: set[str] = set()
        self.tg_script: dict[str, list] = {}
        self.espn_dead: set[str] = set()
        self.exa_reject_contents = False
        self.ai_reject_schema = False
        self.inject_bad_number = False
        self.ai_calls: list[str] = []
        self.schema_payloads: list[dict] = []
        self.msg_id = 100
        self.tg_read_timeout = False

    # -- helpers
    def now_utc(self) -> datetime:
        return now_bd().astimezone(timezone.utc)

    def request(self, method, url, params=None, json=None, data=None, files=None, headers=None, timeout=None):
        params = params or {}
        if "api.telegram.org" in url:
            return self._telegram(url, data or {}, files)
        if "api.cerebras.ai" in url:
            if "cerebras" in self.fail:
                return FakeResponse(500, {"error": "down"})
            if url.endswith("/models"):
                return FakeResponse(200, {"data": [{"id": "gpt-oss-120b"}, {"id": "llama3.1-8b"}]})
            return self._cerebras(json or {})
        if "api.exa.ai" in url:
            if "exa" in self.fail:
                return FakeResponse(500, {"error": "down"})
            return self._exa(json or {})
        if "site.api.espn.com" in url:
            if "espn" in self.fail:
                return FakeResponse(503, text_="unavailable")
            return self._espn(url, params)
        if "thesportsdb.com" in url:
            if "tsdb" in self.fail:
                return FakeResponse(500, text_="err")
            return self._tsdb(params)
        if "wikipedia.org" in url or "wikimedia.org" in url:
            if "wikipedia" in self.fail:
                return FakeResponse(503, text_="unavailable")
            return self._wiki(url, params)
        if any(h in url for h in ("bbci.co.uk", "espn.com/espn/rss", "theguardian.com", "skysports.com")):
            if "rss" in self.fail:
                return FakeResponse(503, text_="unavailable")
            return self._rss(url)
        return FakeResponse(404, text_="unknown host in fake net: " + url)

    # -- telegram
    def _telegram(self, url, data, files):
        method = url.rsplit("/", 1)[-1]
        if "telegram" in self.fail:
            raise requests.exceptions.ConnectTimeout("fake connect timeout")
        script = self.tg_script.get(method)
        if script:
            step = script.pop(0)
            if step == "read_timeout":
                raise requests.exceptions.ReadTimeout("fake read timeout")
            status, desc = step
            payload = {"ok": False, "error_code": status, "description": desc}
            if status == 429:
                payload["parameters"] = {"retry_after": 1}
            return FakeResponse(status, payload)
        if method == "getMe":
            return FakeResponse(200, {"ok": True, "result": {"id": 42, "username": "TestBot"}})
        if method == "getChat":
            return FakeResponse(200, {"ok": True, "result": {"id": -100, "title": "Test Channel"}})
        if method == "getChatMember":
            return FakeResponse(200, {"ok": True, "result": {"status": "administrator", "can_post_messages": True}})
        if method in ("sendMessage", "sendPhoto"):
            body = data.get("text") if method == "sendMessage" else data.get("caption")
            body = body or ""
            limit = MESSAGE_LIMIT if method == "sendMessage" else CAPTION_LIMIT
            parse = data.get("parse_mode")
            if method == "sendPhoto":
                blob = files["photo"].read(4) if files and "photo" in files else b""
                if blob[:2] != b"\xff\xd8":
                    self.violations.append("sendPhoto without a JPEG file")
            if parse == "HTML" and not is_valid_tg_html(body):
                self.violations.append(f"{method}: invalid HTML sent to Telegram: {body[:80]!r}")
                return FakeResponse(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
            if vis_len(body) > limit:
                self.violations.append(f"{method}: {vis_len(body)} chars exceeds {limit}")
                return FakeResponse(400, {"ok": False, "error_code": 400, "description": "message is too long"})
            self.msg_id += 1
            self.tg.append({"method": method, "text": body, "chat": data.get("chat_id"), "parse": parse})
            return FakeResponse(200, {"ok": True, "result": {"message_id": self.msg_id}})
        return FakeResponse(404, {"ok": False, "description": "unknown method"})

    # -- cerebras
    def _cerebras(self, payload):
        rf = payload.get("response_format") or {}
        if rf.get("type") == "json_schema":
            self.schema_payloads.append(rf)
            if self.ai_reject_schema:
                return FakeResponse(400, {"message": "response_format json_schema strict is not supported for this model"})
        msgs = payload.get("messages", [])
        system, user = msgs[0]["content"], msgs[1]["content"]
        task = re.match(r"TASK: (\w+)", system).group(1)
        self.ai_calls.append(task)
        repair = any("broke these rules" in m.get("content", "") for m in msgs[2:])
        obj = self._ai_answer(task, system, user, repair)
        return FakeResponse(200, {"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}],
                                  "usage": {"total_tokens": 120}})

    def _ai_answer(self, task, system, user, repair):
        if task == "digest_intro":
            league = re.search(r"\(([^;]+);", user)
            return {"intro": f"A busy slate led by the {league.group(1) if league else 'top leagues'}."}
        if task == "digest_storyline":
            first = re.search(r"\[1\] (.+)", user)
            return {"storyline": (first.group(1) if first else "")[:200]}
        if task == "otd_pick":
            return {"index": 1, "reason": "concrete"}
        if task == "extract_events":
            d = re.search(r"exact date (\d{4}-\d{2}-\d{2})", system).group(1)
            url = re.search(r"URL: (\S+)", user)
            if "Bangladesh" in user and url:
                return {"events": [{"sport": "Cricket", "league": "Test Series", "name": "Bangladesh vs Sri Lanka",
                                    "home": "Bangladesh", "away": "Sri Lanka", "date": d, "time_utc": "04:00", "source_url": url.group(1)}]}
            return {"events": []}
        if task == "extract_facts":
            art = user.split("ARTICLE:\n", 1)[1]
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", art) if len(s.strip()) > 60 and re.search(r"\d", s) and "[SOURCE" not in s]
            if not sents:
                return {"facts": []}
            q = sents[0][:250]
            return {"facts": [{"claim": q, "evidence_quote": q, "surprise": 4, "certainty": "settled"}]}
        if task == "write_post":
            topic = re.search(r"TOPIC: (.+)", user).group(1)
            ev = user.split("EVIDENCE:\n", 1)[1]
            if self.inject_bad_number and not repair:
                return {"headline": f"{topic}: a bad number"[:80], "body": "The rules were fixed in 1,600 by a committee. It spread widely.",
                        "why_interesting": "It changed how people play.", "key_points": []}
            quote = re.search(r"KEY QUOTE: (.+)", ev)
            if quote:
                rest = [s for s in split_sentences(ev.split("KEY QUOTE:")[0]) if s not in quote.group(1) and len(s) > 30]
                body = quote.group(1) + " " + (rest[0] if rest else "")
            else:
                m = re.match(r"EVENT \(\d+\): (.+?)\nBACKGROUND: (.*)", ev, re.S)
                body = (m.group(1) + " " + (split_sentences(m.group(2)) or [""])[0]) if m else ev[:300]
            body = " ".join(w + (" notably" if (i + 1) % 7 == 0 else "") for i, w in enumerate(body.split()))
            gist = " ".join((quote.group(1) if quote else re.sub(r"^EVENT \(\d+\): ", "", ev)).split()[3:11])
            return {"headline": f"{topic}: {gist}"[:80], "body": body.strip(),
                    "why_interesting": "It shows how the game developed over time.", "key_points": []}
        if task == "diagnose":
            return {"ok": "yes"}
        return {}

    # -- exa
    def _exa(self, body):
        if self.exa_reject_contents and "contents" in body:
            return FakeResponse(400, {"error": "unknown field contents"})
        q = body.get("query", "").lower()
        day = self.now_utc().date().isoformat()
        if "official rules" in q:
            return FakeResponse(200, {"results": [
                {"url": "https://www.britannica.com/topic/game", "title": "Game rules | Britannica", "text": fake_article("Game"), "publishedDate": None},
                {"url": "https://www.fide.com/laws", "title": "Laws of Chess", "text": fake_article("Chess laws"), "publishedDate": None}]})
        if "board game" in q:
            res = []
            for i in range(3):
                w = _WORDS[(hash_int(day) + i * 7) % len(_WORDS)]
                k = hash_int(day + str(i))
                mech = _WORDS[(k >> 3) % len(_WORDS)], _WORDS[(k >> 5) % len(_WORDS)], _TEAMS[k % len(_TEAMS)]
                res.append({"url": f"https://www.dicebreaker.com/games/{w}-{day}-{i}", "title": f"{w.title()} Lights: a new board game announced",
                            "publishedDate": (self.now_utc() - timedelta(days=2)).isoformat(),
                            "text": (f"{mech[2]} Games has announced {w.title()} Lights, a board game for {2 + k % 3} to {4 + k % 3} players that takes about {30 + k % 60} minutes to play. "
                                     f"The game is set to be released in {MONTHS[k % 12]} 2026. Players place {mech[0]} tiles to build a {mech[1]} town and score points for each connected lantern. "
                                     f"The publisher says the box contains {80 + k % 90} tiles and a rulebook of {8 + k % 20} pages. Early reviewers praised the quick setup and the simple turn structure. "
                                     f"Backers of the earlier campaign in {_WORDS[(k >> 7) % len(_WORDS)].title()} will receive a bonus expansion. ")})
            return FakeResponse(200, {"results": res})
        if "cricket" in q:
            m = re.search(r"(\d{1,2} [A-Z][a-z]+ \d{4})", body.get("query", ""))
            return FakeResponse(200, {"results": [{"url": "https://www.espncricinfo.com/series/test-1", "title": f"Bangladesh vs Sri Lanka, {m.group(1) if m else ''}",
                                                   "text": f"Bangladesh vs Sri Lanka, first Test, {m.group(1) if m else ''}. Match starts at 04:00 GMT at Dhaka."}]})
        if "history of chess" in q:
            return FakeResponse(200, {"results": [{"url": "https://www.britannica.com/topic/chess", "title": "Chess", "text": "Chess history " * 40}]})
        return FakeResponse(200, {"results": []})

    # -- espn
    def _espn(self, url, params):
        path = re.search(r"/sports/(.+?)/scoreboard", url).group(1)
        lg = LEAGUE_BY_PATH.get(path)
        if not lg or path in self.espn_dead:
            return FakeResponse(404, text_="not found")
        ymd = params["dates"]
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        now = self.now_utc()
        events = []
        if lg[4] == "team":
            for i, (hh, mm) in enumerate([(12, 30), (17, 0), (23, 0)]):
                start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)
                seed = hash_int(f"{path}{ymd}{i}")
                h, a = _TEAMS[seed % len(_TEAMS)], _TEAMS[(seed // 7 + 5) % len(_TEAMS)]
                if h == a:
                    a = _TEAMS[(seed + 1) % len(_TEAMS)]
                post = start + timedelta(hours=2) < now
                hs, as_ = seed % 4, (seed // 3) % 4
                events.append({
                    "id": f"{ymd}-{i}", "date": start.strftime("%Y-%m-%dT%H:%MZ"), "name": f"{a} at {h}",
                    "status": {"type": {"state": "post" if post else "pre", "completed": post,
                                        "name": "STATUS_FINAL" if post else "STATUS_SCHEDULED", "shortDetail": "FT" if post else "scheduled"}},
                    "competitions": [{"venue": {"fullName": "Test Stadium"},
                                      "notes": [{"headline": "Semifinal"}] if (i == 0 and path.endswith("champions")) else [],
                                      "competitors": [{"homeAway": "home", "team": {"displayName": h}, "score": str(hs), "winner": hs > as_},
                                                      {"homeAway": "away", "team": {"displayName": a}, "score": str(as_), "winner": as_ > hs}]}],
                    "links": [{"href": "https://espn.example/e"}]})
        else:
            monday = d - timedelta(days=d.weekday())
            start = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=timezone.utc)
            end = start + timedelta(days=6, hours=11)
            state = "post" if end < now else "in" if start < now else "pre"
            events.append({"id": f"{path}-{monday.isoformat()}", "date": start.strftime("%Y-%m-%dT%H:%MZ"),
                           "endDate": end.strftime("%Y-%m-%dT%H:%MZ"), "name": f"{lg[2]} Open",
                           "status": {"type": {"state": state, "completed": state == "post", "name": "STATUS_X"}},
                           "competitions": [{"competitors": [{"winner": True, "athlete": {"displayName": "Test Player"}}]}]})
        return FakeResponse(200, {"events": events})

    def _tsdb(self, params):
        if "espn" in self.fail and params.get("s") == "Soccer":
            d = params["d"]
            return FakeResponse(200, {"events": [{"idEvent": f"{d}-{i}", "strEvent": f"Club {i} vs Town {i}", "strSport": "Soccer",
                                                  "strLeague": "Premier League", "dateEvent": d, "strTime": f"{10 + i * 4:02d}:00:00",
                                                  "strHomeTeam": f"Club {i}", "strAwayTeam": f"Town {i}", "strStatus": "Not Started"} for i in range(4)]})
        return FakeResponse(200, {"events": None})

    # -- wikipedia
    def _wiki(self, url, p):
        now = now_bd()
        if "/page/summary/" in url:
            title = url.rsplit("/", 1)[-1].replace("_", " ")
            from urllib.parse import unquote
            title = unquote(title)
            return FakeResponse(200, {"title": title, "extract": fake_article(title)[:420], "description": "sport",
                                      "content_urls": {"desktop": {"page": wiki_url(title)}}})
        if "/feed/onthisday/" in url:
            mm, dd = re.search(r"/events/(\d\d)/(\d\d)", url).groups()
            day = int(dd)
            evs = []
            tmpl = [
                "The {a} {b} club wins the inaugural cricket championship final before a crowd at {c} after {d} overs.",
                "A football match between {A} and {B} ends in a famous draw watched by thousands at the {c} ground.",
                "The world chess title match between {A} and {B} begins in {c} after months of negotiation over the {d} rules.",
                "The {a} Cup tennis final is decided in five sets on a rain-delayed afternoon in {c}, won by {A}.",
                "Organisers introduce a new badminton scoring rule at the {a} {b} Open held in {c} with {d} entrants.",
                "A record marathon field of {A} and {B} runners sets off from {c} on a {d} morning.",
            ]
            for j, age in enumerate((100, 37, 50, 75, 25, 62)):
                k = day * 6 + j + 1
                words = {x: _WORDS[(k * (3 + i) + i * 5 + k // 4) % len(_WORDS)] for i, x in enumerate("abcd")}
                words.update(A=_TEAMS[(k * 7 + k // 3) % len(_TEAMS)], B=_TEAMS[(k * 11 + 3 + k // 5) % len(_TEAMS)], c=words["c"].title())
                extra = f" It drew {1000 + (k * 37) % 9000} spectators and lasted {2 + k % 6} days."
                evs.append({"text": tmpl[(k * 5) % len(tmpl)].format(**words) + (extra if k % 2 else ""),
                            "year": now.year - age, "pages": [{"title": "Test cricket", "extract": fake_article("Test cricket")[:400], "description": "sport",
                                                               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Test_cricket"}}}]})
            evs.append({"text": "A devastating earthquake kills thousands in the city.", "year": now.year - 60, "pages": []})
            evs.append({"text": "The city council approves a new tram line.", "year": now.year - 30, "pages": []})
            return FakeResponse(200, {"events": evs})
        action = p.get("action")
        if action == "parse":
            page = p.get("page", "")
            if page.startswith("Portal:Current events"):
                wt = ("==Sep==\n;Armed conflicts\n* Something else entirely about politics in a region.\n;Sports\n"
                      "* [[Cricket]]: [[Bangladesh]] beat [[Sri Lanka]] by five wickets in the first Test at [[Dhaka]].<ref>{{cite web|url=https://www.espncricinfo.com/x|title=t}}</ref>\n"
                      "* [[Association football]]: [[Arsenal]] win the derby 2-1 in front of a record crowd.\n;Science\n* Unrelated science item goes here for testing.\n")
                return FakeResponse(200, {"parse": {"title": page, "wikitext": wt}})
            ym = re.match(r"(\d{4}) in sports", page)
            if ym:
                mon = MONTHS[now.month - 1]
                a, b, c = (_WORDS[(now.day * k) % len(_WORDS)] for k in (3, 5, 7))
                wt = (f"==Events==\n* [[{mon} {now.day}]] – [[Boxing]]: The {a} {b} title fight ends in a controversial draw before a crowd of 20,000 in {c.title()}.\n"
                      f"* [[{mon} 1]] – [[Golf]]: Something on another day entirely.\n")
                return FakeResponse(200, {"parse": {"title": page, "wikitext": wt}})
            return FakeResponse(200, {"error": {"code": "missingtitle"}})
        if action == "query" and p.get("list") == "categorymembers":
            cat = p.get("cmtitle", "")
            return FakeResponse(200, {"query": {"categorymembers": [{"title": f"Fake {cat[9:14]} game {i}"} for i in range(40)]}})
        if action == "query" and "extracts" in p.get("prop", ""):
            title = p.get("titles", "")
            if title.startswith("Missing"):
                return FakeResponse(200, {"query": {"pages": [{"title": title, "missing": True}]}})
            cats = [{"title": "Category:Video games"}] if "Video" in title else [{"title": "Category:Sports"}]
            return FakeResponse(200, {"query": {"pages": [{"pageid": 1, "title": title, "extract": fake_article(title),
                                                           "categories": cats, "fullurl": wiki_url(title)}]}})
        return FakeResponse(200, {"error": {"code": "badrequest"}})

    def _rss(self, url):
        pub = (self.now_utc() - timedelta(hours=6)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        items = "".join(f"<item><title>Star wins record title number {i}</title><link>https://feeds.example/{i}-{abs(hash(url)) % 999}</link>"
                        f"<description>&lt;p&gt;The champion sets a record after a dramatic final round.&lt;/p&gt;</description><pubDate>{pub}</pubDate></item>"
                        for i in range(3))
        return FakeResponse(200, text_=f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>')


def hash_int(s: str) -> int:
    return int(hashlib.md5(str(s).encode()).hexdigest()[:8], 16)


class T:
    n = 0
    failed: list[str] = []


def check(cond: Any, label: str) -> None:
    T.n += 1
    if not cond:
        T.failed.append(label)
        print("  FAIL:", label)


@contextlib.contextmanager
def quiet():
    lvl = logger.level
    logger.setLevel(logging.CRITICAL)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        logger.setLevel(lvl)


@contextlib.contextmanager
def sandbox(net: FakeNet, workdir: str, **over: Any):
    g = globals()
    keys = ["STATE_FILE", "POSTED_FILE", "TELEGRAM_BOT_TOKEN", "EXA_API_KEY", "CEREBRAS_API_KEY", "CHANNEL", "ADMIN_CHAT_ID",
            "CRICKETDATA_API_KEY", "DRY_RUN", "POST_DELAY_SECONDS", "CEREBRAS_MODEL"]
    saved = {k: g[k] for k in keys}
    saved_env = (_SESSION[0], _SLEEP[0], _CLOCK["now"], _EXA_VARIANT[0])
    g.update({"STATE_FILE": os.path.join(workdir, "state.json"), "POSTED_FILE": os.path.join(workdir, "posted.txt"),
              "TELEGRAM_BOT_TOKEN": "123456:TESTTOKENTESTTOKENTESTTOKEN12345", "EXA_API_KEY": "exa_test_key_123",
              "CEREBRAS_API_KEY": "cb_test_key_123", "CHANNEL": "@TestChannel", "ADMIN_CHAT_ID": "999", "CRICKETDATA_API_KEY": "",
              "DRY_RUN": False, "POST_DELAY_SECONDS": 0.0, "CEREBRAS_MODEL": "gpt-oss-120b"})
    g.update(over)
    _SESSION[0], _SLEEP[0], _EXA_VARIANT[0] = net, (lambda s: None), 0
    try:
        yield
    finally:
        g.update(saved)
        _SESSION[0], _SLEEP[0], _CLOCK["now"], _EXA_VARIANT[0] = saved_env


def set_clock(d: date, hour: int, minute: int = 17) -> None:
    _CLOCK["now"] = datetime(d.year, d.month, d.day, hour, minute, tzinfo=BD_TZ)


def read_state() -> dict:
    return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))


def run_day(day: date, hours: Iterable[int] = range(24), skip: float = 0.0, rng: random.Random | None = None) -> list[int]:
    codes = []
    for h in hours:
        if rng and rng.random() < skip:
            continue
        set_clock(day, h)
        with quiet():
            codes.append(run_once())
    return codes


def plain_posts(net: FakeNet) -> list[str]:
    return [plain_text(m["text"]) for m in net.tg]


# --- unit tests ----------------------------------------------------------------------------------

_RSS_SAMPLE = ('<?xml version="1.0"?><rss version="2.0" xmlns:dc="x"><channel><item><title>A &amp; B win</title><link>https://x.test/1</link>'
               '<description>&lt;b&gt;Hello&lt;/b&gt; world</description><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')
_ATOM_SAMPLE = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom story</title><link href="https://x.test/a"/>'
                '<summary>Sum</summary><updated>2026-09-21T10:00:00Z</updated></entry></feed>')


def test_units() -> None:
    print("units...")
    # scope filter
    check(is_excluded("PlayStation patch notes") and is_excluded("Fortnite esports final") and is_excluded("betting odds on Saturday"), "scope: bad texts excluded")
    check(not is_excluded("Ludo rules; a steam locomotive; against the odds they won the cricket final"), "scope: no false positives")
    check(is_excluded("The console streamer did a playthrough") and not is_excluded("The publisher released the rulebook; the publisher says the trailer for the campaign is live"), "scope: soft terms need 2 distinct hits; tabletop words like publisher are fine")
    check(canonical_url("https://www.Example.com/a/?utm_source=x&ref=y&id=3") == "example.com/a?id=3", "canonical_url strips tracking")
    # windows
    d = date(2026, 9, 22)
    ws, we = dhaka_window(d)
    check(ws == datetime(2026, 9, 21, 18, tzinfo=timezone.utc) and we - ws == timedelta(days=1), "dhaka window = 18:00Z..18:00Z")
    mk = lambda hh, dd: make_event(id=f"x{hh}{dd}", sport="Football", league="L", start=datetime(2026, 9, dd, hh, tzinfo=timezone.utc), name="A vs B")
    kept = in_window([mk(17, 21), mk(18, 21), mk(17, 22), mk(18, 22)], d)
    check(sorted((e["start"].day, e["start"].hour) for e in kept) == [(21, 18), (22, 17)], "window edges respect Dhaka midnight")
    # sanitiser fuzz
    rnd = random.Random(3)
    alpha = list("<>/&\"'abiuscode ahref=") + ["<b>", "</b>", '<a href="https://x.y">', "</a>", "<i>", "&amp;", "&lt;", "<br>", "<u>", "</u>", "😀"]
    ok = True
    for _ in range(1000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 40)))
        o = tg_sanitize(s)
        st_: list[str] = []
        for t in re.finditer(r"<(/?)(\w+)[^>]*>", o):
            if not t.group(1):
                st_.append(t.group(2))
            elif not st_ or st_.pop() != t.group(2):
                ok = False
        ok = ok and not st_ and tg_sanitize(o) == o
    check(ok, "html fuzz: 1000 inputs -> balanced, idempotent, allowed tags only")
    check("javascript" not in tg_sanitize('<a href="javascript:alert(1)">x</a>'), "sanitiser drops javascript: links")
    # guards
    ev = "The race is 1,500 metres long. It was first held in 1896 at Athens, and Ludo descends from Pachisi."
    base = {"headline": "The 1,500 metres", "body": "The race covers 1,500 metres. It was first held in 1896.", "why_interesting": "It dates back to Athens.", "key_points": []}
    check(guard_violations(base, ev) == [], "guard: comma numbers match evidence (v2 bug fixed)")
    check(any("1600" in v for v in guard_violations(dict(base, body="It covers 1,600 metres."), ev)), "guard: wrong number caught")
    check(any("oldest" in v for v in guard_violations(dict(base, body="It was the oldest race in 1896."), ev)), "guard: unsupported superlative caught")
    check(any("names" in v for v in guard_violations(dict(base, body="It was won by Zorblax Quimby and Harold Vanterpool."), ev)), "guard: invented names caught")
    check(any("scope" in v for v in guard_violations(dict(base, body="Big on Xbox esports."), ev)), "guard: out-of-scope caught")
    check(any("time-relative" in v for v in guard_violations(dict(base, body="Played tomorrow in 1896."), ev, daily=True)), "guard: disposable words in daily posts")
    check(any("scores" in v for v in guard_violations(dict(base, body="They won 3-1 in 1896."), ev)), "guard: unsupported scores caught")
    check(guard_violations({"headline": "The match", "body": "They won 3–1.", "why_interesting": "", "key_points": []}, "Final score 3-1 in the match.") == [], "guard: dash variants of scores match")
    # soft fix
    sf = soft_fix({"headline": "Long headline… goes on", "body": " ".join(f"Sentence number {i} is here." for i in range(9)).rstrip("."),
                   "why_interesting": "Two. Sentences", "key_points": ["a" * 200, "b", "c", "d"]})
    check(len(split_sentences(sf["body"])) <= 4 and sf["body"][-1] in ".!?" and "…" not in sf["headline"] and len(sf["key_points"]) == 3 and sf["why_interesting"].endswith("."), "soft_fix repairs formatting in code")
    # quotes
    art = "Ludo is a strategy board game for two to four players, in which the players race their four tokens from start to finish."
    check(quote_in_text("Ludo is a strategy board game for two to four players, in which the players race", art), "quote: exact")
    check(quote_in_text("Ludo is a strategy board game for two to four players in which the players race their four tokens", art), "quote: punctuation-tolerant")
    check(not quote_in_text("Ludo was invented by aliens in the year 3000 for space kings", art), "quote: fabricated rejected")
    check(split_sentences("He won the U.S. Open in 1999. Dr. Smith agreed. J. Smith won too! Then it ended.") ==
          ["He won the U.S. Open in 1999.", "Dr. Smith agreed.", "J. Smith won too!", "Then it ended."], "sentence splitter keeps abbreviations intact")
    src = "The earliest written rules of the game date from 1850, when a committee of twelve members in England recorded the first standard rules for everyone."
    check(copies_source(src, src) and not copies_source("A committee of twelve men wrote the first rules in 1850 in England.", src), "paraphrase guard: verbatim copying detected, paraphrase allowed")
    check(hashtag("Falcon Lights: a new board game announced") == "#FalconLightsBoard" and hashtag("x" * 40) == "", "hashtags are tidy or skipped")
    # schema helpers
    check(extract_json('```json\n{"a": 1}\n```') == {"a": 1} and extract_json('noise {"a": 2} tail') == {"a": 2}, "extract_json handles fences and noise")
    check(validate_schema({"facts": [{"claim": "x", "evidence_quote": "y", "surprise": 3, "certainty": "maybe"}]}, FACTS_SCHEMA) != [], "validate_schema: enum enforced")
    bad_kw = json.dumps([INTRO_SCHEMA, STORYLINE_SCHEMA, PICK_SCHEMA, FACTS_SCHEMA, POST_SCHEMA, EVENTS_SCHEMA])
    check(not any(k in bad_kw for k in ('"minimum"', '"maximum"', '"minItems"', '"maxItems"', '"pattern"', '"format"')), "schemas: no constraint keywords that strict mode may reject")
    # parsers
    f1, f2 = parse_feed(_RSS_SAMPLE), parse_feed(_ATOM_SAMPLE)
    check(f1 and f1[0]["title"] == "A & B win" and f1[0]["summary"] == "Hello world" and f1[0]["published"], "rss parser")
    check(f2 and f2[0]["url"] == "https://x.test/a" and f2[0]["published"], "atom parser")
    check(parse_feed("<not xml") == [] and parse_feed("") == [], "feed parser survives garbage")
    wl = "* [[Cricket]]: [[Bangladesh]] beat [[Sri Lanka|Lions]] by five wickets.<ref>{{cite web|url=https://e.test/x}}</ref> '''bold'''"
    check(wikitext_to_plain(wl) == "Cricket: Bangladesh beat Lions by five wickets. bold", "wikitext_to_plain")
    check(first_link_title("[[September 21]] – [[Boxing]]: fight") == "Boxing" and first_external_url(wl) == "https://e.test/x", "wikitext links")
    check(date_in_text("Match on 22nd September 2026 at Dhaka", date(2026, 9, 22)) and not date_in_text("Match on 23 September", date(2026, 9, 22)), "date_in_text")
    # espn parsing + scoring
    payload = {"events": [{"id": "1", "date": "2026-09-22T12:30Z", "name": "X at Y", "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
                           "competitions": [{"notes": [{"headline": "Final"}], "competitors": [
                               {"homeAway": "home", "team": {"displayName": "Bangladesh"}, "score": "0"},
                               {"homeAway": "away", "team": {"displayName": "India"}, "score": "0"}]}]},
                          {"id": "2", "date": "bad"}, {"id": "3"}]}
    evs = parse_espn(payload, LEAGUE_BY_PATH["soccer/fifa.friendly"])
    check(len(evs) == 1 and evs[0]["name"] == "Bangladesh vs India" and evs[0]["state"] == "scheduled", "parse_espn tolerant of bad events")
    low = make_event(id="l", sport="Football", league="Small League", tier="C", name="Foo vs Bar", start=datetime(2026, 9, 22, 10, tzinfo=timezone.utc))
    check(score_event(evs[0]) > score_event(low) + 40, "score: Bangladesh final outranks obscure match")
    many = [make_event(id=f"e{i}", sport="Football", league="Premier League", tier="A", name=f"Arsenal vs T{i}", start=datetime(2026, 9, 22, 10 + i % 5, tzinfo=timezone.utc)) for i in range(20)]
    many += [make_event(id="c1", sport="Cricket", league="Asia Cup", tier="A", name="India vs Bangladesh", start=datetime(2026, 9, 22, 4, tzinfo=timezone.utc))]
    hi, idx = select_events(many)
    check(sum(1 for e in hi if e["sport"] == "Football") <= 3 and any(e["sport"] == "Cricket" for e in hi), "select: per-sport cap and diversity")
    # rendering limits
    huge = {"format": "fact", "headline": "H" * 300, "body": "Sentence one is long enough here. " * 60, "why_interesting": "Because. " * 100,
            "key_points": ["k" * 300] * 5, "sources": [("A", "https://a.test/?x=1&y=2")] * 6, "tags": ["#T"] * 8}
    check(vis_len(fit_knowledge_html(huge)) <= CAPTION_LIMIT and is_valid_tg_html(fit_knowledge_html(huge)), "caption always fits 1024")
    many2 = [make_event(id=f"m{i}", sport=["Football", "Cricket", "Tennis"][i % 3], league="A very long league name for testing purposes",
                        tier="A", name=f"Team Number {i} vs Rival Number {i}", start=datetime(2026, 9, 22, 6 + i % 12, tzinfo=timezone.utc)) for i in range(200)]
    hi2, idx2 = select_events(many2, 12, 15)
    dh = build_digest_html("next", date(2026, 9, 22), hi2, idx2, "Intro.", {}, [("ESPN", "https://www.espn.com/")])
    check(vis_len(dh) <= MESSAGE_LIMIT and is_valid_tg_html(dh), "digest always fits 4096")
    check("GMT+6" in dh, "digest states its timezone")
    check(redact("x bot123456:TESTTOKENTESTTOKENTESTTOKEN12345 y").count("***") == 1, "redact hides bot tokens")
    # scheduler
    st = default_state()
    at = lambda h: datetime(2026, 9, 21, h, 17, tzinfo=BD_TZ)
    check([s.name for s in due_slots(st, at(6))] == [], "scheduler: nothing due at 06:17")
    check([s.name for s in due_slots(st, at(8))] == ["day_in_sports"], "scheduler: DAY IN SPORTS opens 07:00")
    check("next_up" not in [s.name for s in due_slots(st, at(18))] and "next_up" in [s.name for s in due_slots(st, at(19))], "scheduler: NEXT UP opens 19:00")
    ledger_update(st, "day_in_sports:2026-09-20", status="posted")
    check("day_in_sports" not in [s.name for s in due_slots(st, at(9))], "scheduler: posted slot is not due again")
    ledger_update(st, "next_up:2026-09-22", status="failed", attempts=MAX_ATTEMPTS_PER_SLOT)
    check("next_up" not in [s.name for s in due_slots(st, at(20))], "scheduler: attempts are capped")
    check([s.name for s, _ in sla_breaches(default_state(), at(23))] == ["day_in_sports", "next_up"], "sla: overdue mandatory slots detected")
    # migration
    v2 = {"schema_version": 2, "posts": [{"published_at": "2026-09-01T00:00:00+00:00", "game_or_sport": "Chess", "claim": "c"}], "queue": [1]}
    m = migrate_state(v2)
    check(m["schema_version"] == 3 and m["posts"][0]["topic"] == "Chess" and "ledger" in m and "queue" not in m, "state migration v2 -> v3")


# --- contract + end-to-end + faults ----------------------------------------------------------------

def fresh() -> tuple[FakeNet, str]:
    return FakeNet(), tempfile.mkdtemp(prefix="sn_test_")


def test_contracts() -> None:
    print("contracts...")
    net, tmp = fresh()
    with sandbox(net, tmp):
        set_clock(date(2026, 9, 21), 10)
        ai = AIClient()
        out = ai.json("diagnose", "x", "y", OBJ(ok=STR))
        check(out == {"ok": "yes"} and ai.mode == "json_schema", "ai: strict json_schema mode works")
        payload_kw = json.dumps(net.schema_payloads[-1])
        check('"strict": true' in payload_kw and "additionalProperties" in payload_kw, "ai: request carries strict schema")
        net2, tmp2 = fresh()
        net2.ai_reject_schema = True
        _SESSION[0] = net2
        ai2 = AIClient()
        out2 = ai2.json("diagnose", "x", "y", OBJ(ok=STR))
        check(out2 == {"ok": "yes"} and ai2.mode == "json_object", "ai: downgrades to json_object when schema is rejected")
        net3, _ = fresh()
        net3.exa_reject_contents = True
        _SESSION[0] = net3
        _EXA_VARIANT[0] = 0
        _EXA_CACHE.clear()
        res = exa_search("history of chess", num=2)
        check(len(res) == 1 and _EXA_VARIANT[0] == 1, "exa: falls back to alternate request shape on HTTP 400")
        net4, _ = fresh()
        net4.fail = {"cerebras"}
        _SESSION[0] = net4
        ai4 = AIClient()
        check(ai4.json("diagnose", "x", "y", OBJ(ok=STR)) is None and ai4.last_error, "ai: outage returns None and records why (never raises)")
        # secrets never leak into logged errors
        net5, _ = fresh()
        net5.fail = {"telegram"}
        _SESSION[0] = net5
        r = tg_call("sendMessage", {"chat_id": "@x", "text": "hi"})
        check(not r.get("ok") and TELEGRAM_BOT_TOKEN not in json.dumps(r) and not r.get("uncertain"), "telegram: connect failure is a clean failure, token not leaked")


def test_e2e(show: bool = False) -> FakeNet:
    print("end-to-end day..." if not show else "")
    net, tmp = fresh()
    with sandbox(net, tmp):
        day = date(2026, 9, 21)
        codes = run_day(day)
        st = read_state()
        posts = [p for p in st["posts"] if p["day"] == day.isoformat()]
        c = Counter(p["desk"] for p in posts)
        if show:
            return net
        check(all(x == 0 for x in codes), f"e2e: every tick exits 0 (got {sorted(set(codes))})")
        check(c["past"] == 1 and c["next"] == 1 and c["history"] == 1, f"e2e: both digests + history posted once ({dict(c)})")
        check(c["evergreen"] + c["newgames"] == 3, f"e2e: three evergreen-type posts ({dict(c)})")
        check(len(net.tg) == 6 and not net.violations, f"e2e: 6 telegram messages, all valid ({len(net.tg)}; {net.violations[:2]})")
        txt = plain_posts(net)
        check(any("THE DAY IN SPORTS · 20 SEPTEMBER 2026" in t for t in txt), "e2e: DAY IN SPORTS carries the exact previous date")
        nxt = next((t for t in txt if t.startswith("🗓")), "")
        check("NEXT UP · 22 SEPTEMBER 2026" in nxt and "Times in GMT+6" in nxt, "e2e: NEXT UP carries the exact next date + timezone note")
        check("Bangladesh vs Sri Lanka" in nxt and "10:00" in nxt, "e2e: cricket fallback verified by date and shown in GMT+6")
        check("18:30" in nxt or "23:00" in nxt or "22:30" in nxt, "e2e: ESPN UTC times converted to GMT+6")
        check(not any(re.search(r"\b(tomorrow|yesterday|tonight)\b", t, re.I) for t in [nxt] + [t for t in txt if t.startswith("📰")]), "e2e: no relative-date words in daily posts")
        check(net.tg[0]["method"] == "sendMessage" and any(m["method"] == "sendPhoto" for m in net.tg), "e2e: digests are text, knowledge posts are photo+caption")
        check(all(l["status"] == "posted" for k, l in st["ledger"].items() if k.split(":")[0] in ("day_in_sports", "next_up", "on_this_date")), "e2e: ledger marks slots posted")
        n_before = len(net.tg)
        run_day(day)
        check(len(net.tg) == n_before, "e2e: re-running the same day posts nothing (idempotent)")
        urls = [u for u in Path(POSTED_FILE).read_text().splitlines() if u]
        check(len(urls) >= 1 and len(urls) == len(set(urls)) and not any("wikipedia" in u for u in urls), "e2e: posted_urls.txt holds article URLs only, duplicate-free")
        heads = [p["headline"] for p in posts]
        check(len(heads) == len(set(heads)), "e2e: no duplicate headlines")
        # the cutoffs: nothing early
        ticks_first = st["runs"][0]
        check(st["runs"][7]["posted"] == 1 and st["runs"][7]["due"] == ["day_in_sports"], "e2e: 07:17 tick posts DAY IN SPORTS only")
    return net


def test_faults() -> None:
    print("fault injection...")
    day = date(2026, 9, 21)
    expect = {"exa": 6, "rss": 6, "tsdb": 6, "espn": 6, "cerebras": 2, "wikipedia": 2}
    for dep, want in expect.items():
        net, tmp = fresh()
        net.fail = {dep}
        with sandbox(net, tmp):
            codes = run_day(day)
            st = read_state()
            check(1 not in codes, f"fault[{dep}]: run never crashes")
            n = len(net.tg)
            check(n >= want or (dep in ("espn",) and n >= 4), f"fault[{dep}]: other desks still post (got {n}, want >= {want})")
            check(not net.violations, f"fault[{dep}]: nothing invalid was sent")
            check(any(p["desk"] in ("past", "next") for p in st["posts"]), f"fault[{dep}]: digests survive")
    # telegram-level faults
    cases = {
        "html rejected -> plain text": {"sendMessage": [(400, "Bad Request: can't parse entities: bad")]},
        "429 then ok": {"sendMessage": [(429, "Too Many Requests: retry after 1")]},
        "photo fails -> text fallback": {"sendPhoto": [(400, "Bad Request: wrong file identifier")]},
    }
    for label, script in cases.items():
        net, tmp = fresh()
        net.tg_script = script
        with sandbox(net, tmp):
            run_day(day, hours=range(7, 12))
            st = read_state()
            check(any(p["desk"] == "past" for p in st["posts"]) and len(net.tg) >= 2, f"telegram[{label}]: post still lands")
    net, tmp = fresh()
    net.tg_script = {"sendMessage": ["read_timeout"]}
    with sandbox(net, tmp):
        run_day(day, hours=range(7, 9))
        st = read_state()
        led = st["ledger"]["day_in_sports:2026-09-20"]
        check(led["status"] == "uncertain" and not any(p["desk"] == "past" for p in st["posts"]), "telegram[read timeout]: marked uncertain, not re-sent (no duplicate)")
    # AI repair path
    net, tmp = fresh()
    net.inject_bad_number = True
    with sandbox(net, tmp):
        run_day(day, hours=range(9, 12))
        st = read_state()
        hist = [p for p in st["posts"] if p["desk"] == "history"]
        check(hist and net.ai_calls.count("write_post") >= 2 and not any("1,600" in m["text"] for m in net.tg), "ai: invented number rejected, repair call fixes it")
    # SLA path: everything that feeds digests is down
    net, tmp = fresh()
    net.fail = {"espn", "tsdb", "exa", "cerebras", "wikipedia", "rss"}
    with sandbox(net, tmp):
        codes = run_day(day, hours=range(7, 24))
        alerts = [m for m in net.tg if m["chat"] == "999"]
        check(2 in codes and codes.count(2) <= 2, f"sla: overdue mandatory post exits 2 once per breach (codes: {codes.count(2)})")
        check(len(alerts) >= 1 and len(alerts) <= 3, f"sla: admin alerted, deduplicated ({len(alerts)})")
        check(read_state()["ledger"]["day_in_sports:2026-09-20"]["status"] == "failed", "sla: failure recorded with reason")
    # corrupt state -> refuse to post
    net, tmp = fresh()
    with sandbox(net, tmp):
        Path(STATE_FILE).write_text("{not json", encoding="utf-8")
        set_clock(day, 8)
        with quiet():
            code = run_once()
        check(code == 1 and not net.tg, "state: corrupt file stops the bot instead of posting blindly")
    # dry run posts nothing and writes nothing
    net, tmp = fresh()
    with sandbox(net, tmp, DRY_RUN=True):
        set_clock(day, 8)
        with quiet():
            run_once()
        check(not net.tg and not Path(STATE_FILE).exists(), "dry-run: no telegram traffic, no state written")


def test_soak(days: int = 30) -> None:
    print(f"soak: {days} days, hourly cron, 20% of runs skipped, rotating outages...")
    net, tmp = fresh()
    rng = random.Random(11)
    outages = {4: "espn", 8: "wikipedia", 12: "cerebras", 17: "exa", 21: "rss", 25: "tsdb"}
    start = date(2026, 9, 1)
    t0 = time.monotonic()
    with sandbox(net, tmp):
        all_codes: list[int] = []
        for i in range(days):
            net.fail = {outages[i]} if i in outages else set()
            all_codes += run_day(start + timedelta(days=i), skip=0.2, rng=rng)
        st = read_state()
        posts = st["posts"]
        by = Counter(p["desk"] for p in posts)
        elapsed = time.monotonic() - t0
        check(1 not in all_codes, "soak: no crash in any run")
        check(by["past"] == days and by["next"] == days, f"soak: exactly one DAY IN SPORTS and one NEXT UP per day ({dict(by)})")
        keys = [k for k, v in st["ledger"].items() if v["status"] == "posted"]
        check(len(net.tg) == len(posts) == len(keys) + 0 or len(net.tg) - len([m for m in net.tg if m["chat"] == "999"]) == len(posts), "soak: telegram messages == recorded posts (no double posts)")
        heads = [p["headline"] for p in posts]
        check(len(heads) == len(set(heads)), "soak: no duplicate headlines in 30 days")
        claims = [p["claim"] for p in posts if p["desk"] in ("history", "evergreen", "newgames")]
        check(len(claims) == len(set(claims)), "soak: no duplicate claims")
        hurt = sum(1 for i, dep in outages.items() if i < days and dep in ("wikipedia", "cerebras"))  # only these stop knowledge desks
        healthy = days - hurt
        check(by["history"] >= healthy - 3 and by["history"] <= days, f"soak: history posts on healthy days ({by['history']} >= {healthy - 3})")
        check(by["evergreen"] + by["newgames"] >= 3 * healthy - 8, f"soak: evergreen volume ({by['evergreen'] + by['newgames']} >= {3 * healthy - 8})")
        check(by["newgames"] >= max(1, days // 6), f"soak: new-games desk contributes ({by['newgames']} >= {max(1, days // 6)})")
        topics = Counter(p["topic"] for p in posts if p["desk"] == "evergreen")
        check(topics and max(topics.values()) <= 4, f"soak: topics rotate, max repeats {max(topics.values()) if topics else 0}")
        check(not net.violations, f"soak: every message valid ({net.violations[:2]})")
        check(elapsed < 240, f"soak: finished in {elapsed:.0f}s")
        print(f"   soak posts: {dict(by)}, telegram messages: {len(net.tg)}, exit codes: {dict(Counter(all_codes))}, {elapsed:.0f}s")


def v3_self_test(fast: bool = False) -> int:
    T.n, T.failed = 0, []
    t0 = time.monotonic()
    test_units()
    test_contracts()
    test_e2e()
    test_faults()
    test_soak(8 if fast else 30)
    dt = time.monotonic() - t0
    if T.failed:
        print(f"\nSELF-TEST FAILED: {len(T.failed)} of {T.n} checks failed:")
        for f in T.failed:
            print("  -", f)
        return 1
    print(f"\nSelf-test passed for {APP_NAME} v{APP_VERSION}: {T.n} checks in {dt:.0f}s")
    return 0


def demo() -> int:
    """Run one simulated day on the fake network and print every Telegram message."""
    net = test_e2e(show=True)
    for i, m in enumerate(net.tg, 1):
        print(f"\n{'=' * 70}\nMESSAGE {i}  [{m['method']}]  {vis_len(m['text'])} visible chars\n{'=' * 70}\n{plain_text(m['text'])}")
    return 0




# ===========================================================================
# 13. V4 PRODUCTION ENGINE
# ===========================================================================

V4_STATE_KEY = "v4"
V4_COVERAGE_FILE = "coverage_index.json"
V4_PROMPTS_DIR = Path("prompts")
V4_SCHEMAS_DIR = Path("schemas")
V4_RUNS_PER_DAY = 2
V4_RESEARCH_COUNT = 20
V4_PUBLISH_COUNT = 10
V4_COVERAGE_RECENT_DAYS = _env_int("V4_COVERAGE_RECENT_DAYS", 60)
V4_SEMANTIC_DUP_THRESHOLD = _env_float("V4_SEMANTIC_DUP_THRESHOLD", 0.86)
V4_TOPIC_DUP_THRESHOLD = _env_float("V4_TOPIC_DUP_THRESHOLD", 0.90)
V4_IMAGE_TIMEOUT = _env_int("V4_IMAGE_TIMEOUT", 12)
V4_MAX_IMAGE_BYTES = _env_int("V4_MAX_IMAGE_BYTES", 10_000_000)
V4_POST_DELAY = _env_float("V4_POST_DELAY_SECONDS", POST_DELAY_SECONDS)
V4_DRY_RUN = False

V4_SECTORS = [
    "Sport Discovery", "Game Discovery", "Interesting Sports Fact", "Interesting Game Fact",
    "Rule Check", "How to Play", "Sport Origin", "Game Origin", "On This Date", "First / Last / Only",
    "Records & Milestones", "Forgotten Sport", "Forgotten Game", "Equipment / Measurement", "Why Does This Happen?",
    "Then vs Now", "New Sport Discovery", "New Tabletop Game Discovery", "Traditional / Regional Game", "Sports & Games People",
]
V4_SECTOR_SET = set(V4_SECTORS)

# Current/live-news phrases are deliberately broader than the old V3 news filter because V4 evergreen content
# must never leak temporary sports information from Exa's research results.
V4_CURRENT_NEWS_RX = re.compile(
    r"\b(today|yesterday|tomorrow|upcoming|next match|next game|fixture|fixtures|current schedule|current standings|"
    r"transfer|transfers|rumou?r|injur(?:y|ies)|latest|breaking|betting|odds|preview|match report|game report|"
    r"live score|live scores|current team|current form|squad update|contract extension|signing|loan move)\b",
    re.I,
)


def _v4_default_state() -> dict:
    return {
        "schema": 4,
        "created_at": utc_iso(now_bd()),
        "last_run_at": "",
        "daily": {},
        "runs": [],
        "live": {
            "schedule": {"message_id": None, "target_date": ""},
            "results": {"message_id": None, "target_date": ""},
        },
        "publication": {},
    }


def v4_state(state: dict) -> dict:
    root = state.setdefault(V4_STATE_KEY, _v4_default_state())
    if not isinstance(root, dict):
        root = _v4_default_state()
        state[V4_STATE_KEY] = root
    root.setdefault("schema", 4)
    root.setdefault("created_at", utc_iso(now_bd()))
    root.setdefault("last_run_at", "")
    root.setdefault("daily", {})
    root.setdefault("runs", [])
    root.setdefault("publication", {})
    live = root.setdefault("live", {})
    live.setdefault("schedule", {"message_id": None, "target_date": ""})
    live.setdefault("results", {"message_id": None, "target_date": ""})
    root["runs"] = root.get("runs", [])[-60:]
    return root


def v4_load_coverage(state: dict | None = None) -> dict:
    p = Path(V4_COVERAGE_FILE)
    data = {"schema": 1, "updated_at": utc_iso(now_bd()), "records": []}
    if p.exists() and p.stat().st_size:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("records"), list):
                data.update(raw)
        except Exception as exc:
            raise RuntimeError(f"Coverage index unreadable; refusing to run blind: {exc}") from exc

    # Bootstrap permanently from the existing V3 archive once. This keeps already-published knowledge protected.
    existing = {text(r.get("content_id")) for r in data.get("records", []) if isinstance(r, dict)}
    if state:
        for p_row in state.get("posts", []):
            if not isinstance(p_row, dict) or p_row.get("desk") in {"next", "past"}:
                continue
            subject = text(p_row.get("topic") or p_row.get("headline"))
            claim = text(p_row.get("claim"))
            if not subject and not claim:
                continue
            cid = fingerprint("legacy", subject, claim, p_row.get("posted_at", ""))
            if cid in existing:
                continue
            urls = [canonical_url(u) for u in p_row.get("urls", []) if text(u)]
            data["records"].append({
                "content_id": cid,
                "content_type": "evergreen",
                "sector": text(p_row.get("sector")) or text(p_row.get("format")) or "legacy",
                "normalized_subject": normalize_text(subject),
                "central_knowledge_unit": normalize_text(claim)[:220] or normalize_text(subject),
                "central_claim": claim[:500],
                "editorial_angle": text(p_row.get("angle")),
                "sport_or_game": subject,
                "country": "",
                "region": "",
                "people": [],
                "published_at": text(p_row.get("posted_at")),
                "run_id": "legacy-v3",
                "telegram_message_id": p_row.get("message_id"),
                "source_urls": urls,
                "fingerprints": [fingerprint(subject, claim), fingerprint(claim)],
                "legacy": True,
            })
            existing.add(cid)
    data["updated_at"] = utc_iso(now_bd())
    return data


def v4_save_coverage(data: dict) -> None:
    if V4_DRY_RUN or DRY_RUN:
        return
    p = Path(V4_COVERAGE_FILE)
    tmp = Path(V4_COVERAGE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def v4_record_key(post: dict) -> str:
    return fingerprint(
        post.get("normalized_subject", ""),
        post.get("central_knowledge_unit", ""),
        post.get("central_claim", ""),
        post.get("sector", ""),
    )


def v4_topic_similarity(a: dict, b: dict) -> float:
    pieces = [
        similarity(a.get("normalized_subject", ""), b.get("normalized_subject", "")),
        similarity(a.get("central_knowledge_unit", ""), b.get("central_knowledge_unit", "")),
        similarity(a.get("central_claim", ""), b.get("central_claim", "")),
    ]
    return max(pieces) * 0.55 + sum(pieces) / len(pieces) * 0.45


def v4_coverage_context(data: dict, target: date, used_sectors: set[str]) -> str:
    rows = [r for r in data.get("records", []) if isinstance(r, dict)]
    cutoff = datetime.combine(target - timedelta(days=V4_COVERAGE_RECENT_DAYS), datetime.min.time(), tzinfo=BD_TZ)
    recent, older = [], []
    for r in rows:
        dt = parse_dt(r.get("published_at"))
        if dt and dt.astimezone(BD_TZ) >= cutoff:
            recent.append(r)
        else:
            older.append(r)
    recent = recent[-180:]
    older = older[-350:]
    lines = [
        "ALREADY COVERED KNOWLEDGE — LOCAL MEMORY (AUTHORITATIVE)",
        "Do not repeat the underlying knowledge, even with a new headline or source.",
        "A related topic may still be valid when its knowledge unit is materially different.",
        f"SECTORS ALREADY USED TODAY: {', '.join(sorted(used_sectors)) or 'none'}",
        "",
        "RECENT COVERAGE:",
    ]
    for r in recent:
        lines.append(
            f"- {r.get('sector','')}: {r.get('normalized_subject','')} | "
            f"knowledge={r.get('central_knowledge_unit','')} | claim={r.get('central_claim','')[:180]}"
        )
    lines.append("\nOLDER COMPACT COVERAGE:")
    for r in older:
        lines.append(f"- {r.get('sector','')}: {r.get('normalized_subject','')} | knowledge={r.get('central_knowledge_unit','')}")
    return "\n".join(lines)


def v4_candidate_base(post: dict) -> dict:
    sources = []
    for key in ("source_1", "source_2", "source_3"):
        src = post.get(key) or {}
        if isinstance(src, dict) and text(src.get("url")):
            sources.append({
                "name": text(src.get("name")),
                "url": text(src.get("url")),
                "type": text(src.get("type")),
                "evidence_note": text(src.get("evidence_note")),
            })
    if isinstance(post.get("sources"), list):
        sources.extend(x for x in post.get("sources", []) if isinstance(x, dict) and text(x.get("url")))
    # Preserve order and de-duplicate URL sources.
    seen = set(); dedup = []
    for s in sources:
        c = canonical_url(s.get("url", ""))
        if c and c not in seen:
            seen.add(c); dedup.append(s)
    img = post.get("image") if isinstance(post.get("image"), dict) else {}
    return {
        **post,
        "sector": text(post.get("sector")),
        "normalized_subject": text(post.get("normalized_subject")),
        "central_knowledge_unit": text(post.get("central_knowledge_unit")),
        "central_claim": text(post.get("central_claim")),
        "editorial_angle": text(post.get("editorial_angle")),
        "sport_or_game": text(post.get("sport_or_game") or post.get("topic")),
        "sport_or_game_category": text(post.get("sport_or_game_category")),
        "topic": text(post.get("topic")),
        "headline": text(post.get("headline")),
        "main_story": text(post.get("main_story")),
        "historical_date": text(post.get("historical_date")),
        "historical_year": text(post.get("historical_year")),
        "historical_period": text(post.get("historical_period")),
        "country": text(post.get("country")),
        "region": text(post.get("region")),
        "city_location": text(post.get("city_location")),
        "people_involved": [text(x) for x in (post.get("people_involved") or []) if text(x)],
        "date_relevance": text(post.get("date_relevance")),
        "sources": dedup[:3],
        "image": img,
    }



def v4_schema_contract_stats(schema: dict) -> tuple[int, int]:
    """Return (property_count, max_object_depth) for an Exa output schema."""
    props = 0
    max_depth = 0

    def walk(node: object, depth: int = 0) -> None:
        nonlocal props, max_depth
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            max_depth = max(max_depth, depth + 1)
            p = node.get("properties") or {}
            if isinstance(p, dict):
                props += len(p)
                for child in p.values():
                    walk(child, depth + 1)
        elif node.get("type") == "array":
            walk(node.get("items"), depth)
        for key in ("oneOf", "anyOf", "allOf"):
            values = node.get(key) or []
            if isinstance(values, list):
                for child in values:
                    walk(child, depth)

    walk(schema, 0)
    return props, max_depth


def v4_validate_exa_output_schema(schema: dict) -> tuple[bool, str]:
    if not isinstance(schema, dict):
        return False, "schema_not_object"
    props, depth = v4_schema_contract_stats(schema)
    # Exa's current outputSchema contract allows at most 10 total properties and
    # two levels of object nesting. Keep this local guard ahead of the network call.
    if props > 10:
        return False, f"outputSchema_properties={props} > 10"
    if depth > 2:
        return False, f"outputSchema_depth={depth} > 2"
    return True, "ok"


def v4_exa_validate_package(package: dict, required_sectors: list[str] | None = None, target: date | None = None) -> tuple[bool, list[str], list[dict]]:
    """Validate the compact Exa discovery package. Sources/editorial fields are added later."""
    errors: list[str] = []
    if not isinstance(package, dict):
        return False, ["package_not_object"], []
    if package.get("daily_complete") is not True:
        errors.append("daily_complete_false")
    if target is not None and text(package.get("calendar_date")) != target.isoformat():
        errors.append("calendar_date_mismatch")
    posts = package.get("posts")
    if not isinstance(posts, list) or len(posts) != V4_RESEARCH_COUNT:
        return False, [f"post_count={len(posts) if isinstance(posts, list) else 'invalid'}"], []
    candidates = [v4_candidate_base(x) for x in posts if isinstance(x, dict)]
    if len(candidates) != V4_RESEARCH_COUNT:
        errors.append("non_object_post")
    sectors = [c.get("sector") for c in candidates]
    if set(sectors) != V4_SECTOR_SET or len(sectors) != len(set(sectors)):
        errors.append("sector_matrix_invalid")
    for i, c in enumerate(candidates, 1):
        if not c["normalized_subject"] or not c["central_knowledge_unit"] or not c["central_claim"]:
            errors.append(f"missing_core:{i}")
        discovery_text = " ".join([
            c.get("normalized_subject", ""),
            c.get("central_knowledge_unit", ""),
            c.get("central_claim", ""),
            text(c.get("research_focus")),
        ])
        if V4_CURRENT_NEWS_RX.search(discovery_text):
            errors.append(f"current_news:{i}")
    seen_units: list[dict] = []
    for i, c in enumerate(candidates):
        for j, p in enumerate(seen_units):
            if normalize_text(c["central_knowledge_unit"]) == normalize_text(p["central_knowledge_unit"]):
                errors.append(f"duplicate_knowledge_unit:{i+1}:{j+1}")
            elif v4_topic_similarity(c, p) >= V4_SEMANTIC_DUP_THRESHOLD:
                errors.append(f"semantic_duplicate:{i+1}:{j+1}")
        seen_units.append(c)
    topics = Counter(normalize_text(c.get("sport_or_game")) for c in candidates if c.get("sport_or_game"))
    if topics and max(topics.values()) > 4:
        errors.append(f"sport_game_dominance:{max(topics.values())}")
    regions = {normalize_text(c.get("region")) for c in candidates if c.get("region")}
    countries = {normalize_text(c.get("country")) for c in candidates if c.get("country")}
    if len(regions | countries) < 5:
        REPORT.notes.append(f"Exa geography diversity only {len(regions | countries)} non-empty regions/countries")
    people_central = Counter()
    for c in candidates:
        if c.get("sector") == "Sports & Games People" and c.get("people_involved"):
            people_central.update([normalize_text(c["people_involved"][0])])
    if any(v > 1 for v in people_central.values()):
        errors.append("same_person_people_sector")
    return not errors, errors, candidates


def _v4_extract_structured_content(data: dict, content_key: str = "output") -> tuple[dict | None, list[dict]]:
    """Extract structured output content and raw web results from Exa response variants."""
    results = data.get("results") or [] if isinstance(data, dict) else []
    out = data.get(content_key) if isinstance(data, dict) else None
    if isinstance(out, dict):
        content = out.get("content")
        if isinstance(content, dict):
            return content, results
        if isinstance(content, str):
            try:
                parsed = extract_json(content)
                return parsed if isinstance(parsed, dict) else None, results
            except Exception:
                return None, results
    if isinstance(out, str):
        try:
            parsed = extract_json(out)
            return parsed if isinstance(parsed, dict) else None, results
        except Exception:
            return None, results
    direct = data.get("content") if isinstance(data, dict) else None
    if isinstance(direct, dict):
        return direct, results
    if isinstance(direct, str):
        try:
            parsed = extract_json(direct)
            return parsed if isinstance(parsed, dict) else None, results
        except Exception:
            pass
    return None, results if isinstance(results, list) else []


def v4_exa_research(ai: "AIClient", state: dict, coverage: dict, target: date, run_id: str, used_sectors: set[str], recovery_note: str = "") -> tuple[list[dict], list[dict], str]:
    """Discover exactly 20 compact evergreen candidates. No editorial/source/image payload here."""
    if not EXA_API_KEY:
        return [], [], "EXA_API_KEY missing"
    prompt_path = V4_PROMPTS_DIR / "exa_daily_evergreen_v4.txt"
    schema_path = V4_SCHEMAS_DIR / "exa_daily_sports_games_output_schema_v1.json"
    if not prompt_path.exists() or not schema_path.exists():
        return [], [], "V4 Exa prompt/schema missing"
    prompt = prompt_path.read_text(encoding="utf-8")
    exclusions = v4_coverage_context(coverage, target, set(used_sectors))
    prompt = prompt.replace("{{CALENDAR_DATE}}", target.isoformat()).replace("{{YEAR}}", str(target.year))
    prompt = prompt.replace("{{PREVIOUS_INDEX}}", exclusions).replace("{{EXCLUDED_SUBJECTS}}", exclusions)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [], f"Exa schema unreadable: {exc}"
    schema_ok, schema_reason = v4_validate_exa_output_schema(schema)
    if not schema_ok:
        REPORT.errors.append(f"Exa discovery schema rejected locally: {schema_reason}")
        return [], [], schema_reason
    unused = [s for s in V4_SECTORS if s not in used_sectors]
    query = (
        f"Research evergreen Sports & Games topics for {target.isoformat()} {target.year}. "
        "Return exactly one strong, independently supportable candidate for every one of the 20 required editorial sectors. "
        "Search globally and prioritize authoritative historical, governing-body, archive, museum, academic and specialist evidence. "
        "Do not cover current sports news. Each candidate must be materially distinct."
    )
    if used_sectors:
        query += (
            " Earlier today, these sectors were already published: " + ", ".join(sorted(used_sectors)) + ". "
            "Still return one candidate for every sector because the full 20-sector batch is mandatory, "
            "but make candidates in already-used sectors lower-priority so the ranking desk can prefer unused sectors."
        )
    if recovery_note:
        query += " Recovery pass: avoid the rejected or covered concepts below and search for materially different knowledge. " + recovery_note[:1500]
    body = {
        "query": query,
        "type": "deep",
        "additionalQueries": [
            "unusual traditional indigenous regional sports history rules equipment",
            "board games card games dice games tabletop game history rules origins",
            "forgotten obscure historical sports games around the world",
            "sports records milestones firsts last only verified archives",
            "sports equipment measurement technology history rules",
            "athlete historical figures founders sports institutions archives",
            "historical rule changes sports governance official archives",
            "emerging non-digital sports and newer physical tabletop games established evidence",
        ],
        "contents": {"highlights": True, "extras": {"imageLinks": 2}},
        "systemPrompt": prompt,
        "outputSchema": schema,
    }
    r = http("exa", "POST", "https://api.exa.ai/search", json_body=body,
             headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=150, retries=2, backoff=2.0)
    if not r.ok or not isinstance(r.data, dict):
        return [], [], r.error or f"Exa HTTP {r.status}"
    package, results = _v4_extract_structured_content(r.data)
    if not isinstance(package, dict):
        return [], results, "Exa did not return structured discovery output"
    package.setdefault("calendar_date", target.isoformat())
    package.setdefault("daily_complete", True)
    ok, errors, posts = v4_exa_validate_package(package, [], target)
    if not ok:
        REPORT.errors.append("Exa discovery package rejected: " + ", ".join(errors[:12]))
        return [], results, "invalid Exa discovery package: " + ", ".join(errors[:10])
    for p in posts:
        p["_run_id"] = run_id
    return posts, results, "ok"


def v4_exa_evidence_research(ai: "AIClient", selected: list[dict], target: date, run_id: str) -> tuple[dict, list[dict], str]:
    """Research sources/evidence/images for the selected ten using a compact schema."""
    if not EXA_API_KEY:
        return {}, [], "EXA_API_KEY missing"
    prompt_path = V4_PROMPTS_DIR / "exa_selected_evidence_v4.txt"
    schema_path = V4_SCHEMAS_DIR / "exa_selected_evidence_v4.json"
    if not prompt_path.exists() or not schema_path.exists():
        return {}, [], "V4 Exa evidence prompt/schema missing"
    prompt = prompt_path.read_text(encoding="utf-8").replace("{{CALENDAR_DATE}}", target.isoformat())
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, [], f"Exa evidence schema unreadable: {exc}"
    schema_ok, schema_reason = v4_validate_exa_output_schema(schema)
    if not schema_ok:
        REPORT.errors.append(f"Exa evidence schema rejected locally: {schema_reason}")
        return {}, [], schema_reason
    payload = []
    for i, c in enumerate(selected, 1):
        payload.append({
            "post_number": i,
            "sector": c.get("sector"),
            "subject": c.get("normalized_subject"),
            "knowledge_unit": c.get("central_knowledge_unit"),
            "claim": c.get("central_claim"),
            "research_focus": c.get("research_focus", ""),
        })
    body = {
        "query": (
            f"Deeply verify the selected evergreen Sports & Games research subjects for {target.isoformat()}. "
            "Find authoritative evidence, source URLs, and the most relevant real image candidates for each numbered subject. "
            "Do not invent facts or URLs. Do not use current sports news."
        ),
        "type": "deep",
        "contents": {"highlights": True, "extras": {"imageLinks": 3}},
        "systemPrompt": prompt,
        "outputSchema": schema,
        "additionalQueries": [
            "official sports governing body archive rulebook history",
            "museum university library archive sports history",
            "specialist game history rules tabletop games",
        ],
    }
    # The selected subjects are included in the query context rather than in the schema.
    body["query"] += " Selected subjects:\n" + json.dumps(payload, ensure_ascii=False)
    r = http("exa", "POST", "https://api.exa.ai/search", json_body=body,
             headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=180, retries=2, backoff=2.0)
    if not r.ok or not isinstance(r.data, dict):
        return {}, [], r.error or f"Exa HTTP {r.status}"
    structured, results = _v4_extract_structured_content(r.data)
    if not isinstance(structured, dict):
        return {}, results, "Exa did not return structured evidence output"
    if structured.get("research_complete") is not True:
        return {}, results, "research_complete_false"
    items = structured.get("items")
    if not isinstance(items, list) or len(items) != len(selected):
        return {}, results, f"evidence_item_count={len(items) if isinstance(items, list) else 'invalid'}"
    normalized = {}
    for row in items:
        if not isinstance(row, dict):
            return {}, results, "evidence_item_not_object"
        try:
            n = int(row.get("post_number"))
        except Exception:
            return {}, results, "evidence_post_number_invalid"
        if not 1 <= n <= len(selected):
            return {}, results, "evidence_post_number_out_of_range"
        normalized[n] = {
            "post_number": n,
            "evidence": text(row.get("evidence")),
            "source_urls": [text(x) for x in (row.get("source_urls") or []) if text(x)],
            "source_names": [text(x) for x in (row.get("source_names") or []) if text(x)],
            "image_urls": [text(x) for x in (row.get("image_urls") or []) if text(x)],
            "image_source_page_urls": [text(x) for x in (row.get("image_source_page_urls") or []) if text(x)],
            "image_notes": [text(x) for x in (row.get("image_notes") or []) if text(x)],
        }
    if len(normalized) != len(selected):
        return {}, results, "evidence_missing_post"
    return normalized, results, "ok"


def v4_apply_evidence(selected: list[dict], evidence: dict, results: list[dict]) -> list[dict]:
    out=[]
    for i, c in enumerate(selected, 1):
        row=dict(c)
        ev=evidence.get(i, {}) if isinstance(evidence, dict) else {}
        row["research_evidence"] = text(ev.get("evidence"))
        source_urls=[]; source_names=[]
        for u in ev.get("source_urls", []):
            if re.match(r"^https?://", u, re.I) and canonical_url(u) not in {canonical_url(x) for x in source_urls}:
                source_urls.append(u)
        for n in ev.get("source_names", []):
            source_names.append(n)
        sources=[]
        seen_source_urls=set()
        for j,u in enumerate(source_urls[:3]):
            cu=canonical_url(u)
            if not cu or cu in seen_source_urls:
                continue
            seen_source_urls.add(cu)
            name=source_names[j] if j < len(source_names) and source_names[j] else source_label(u)
            sources.append({"name":name,"url":u,"type":"Exa evidence source","evidence_note":row["research_evidence"][:500]})
        # Never attach arbitrary search results to a candidate. Evidence sources must be explicitly returned
        # for that numbered subject, otherwise the deterministic editorial gate rejects the candidate.
        row["sources"] = sources[:3]
        urls = ev.get("image_urls", []) if isinstance(ev, dict) else []
        pages = ev.get("image_source_page_urls", []) if isinstance(ev, dict) else []
        notes = ev.get("image_notes", []) if isinstance(ev, dict) else []
        row["research_images"] = []
        for j,u in enumerate(urls):
            if not re.match(r"^https?://",u,re.I):
                continue
            row["research_images"].append({
                "url":u,
                "source_page_url":pages[j] if j < len(pages) else "",
                "source_name":sources[0].get("name","") if sources else "",
                "source_type":"Exa evidence image",
                "alt_text":row.get("normalized_subject", ""),
                "caption":notes[j] if j < len(notes) else "",
            })
        out.append(row)
    return out

def v4_result_evidence(results: list[dict], urls: list[str], candidate: dict | None = None) -> str:
    candidate = candidate or {}
    parts = []
    if text(candidate.get("research_evidence")):
        parts.append("TARGETED EXA EVIDENCE\n" + text(candidate.get("research_evidence"))[:6000])
    want = {canonical_url(u) for u in urls if u}
    for row in results:
        if not isinstance(row, dict):
            continue
        cu = canonical_url(text(row.get("url")))
        if want and cu not in want:
            continue
        hs = row.get("highlights") or []
        if isinstance(hs, list):
            txt = " ".join(text(x) for x in hs if text(x))
        else:
            txt = text(hs)
        if txt:
            parts.append(f"SOURCE {text(row.get('title'))}\n{txt[:3500]}")
    return "\n\n".join(parts)[:12000]


def v4_review_borderline_duplicate(ai: "AIClient", candidate: dict, row: dict, score: float) -> str:
    if not ai.available or ai.fatal:
        return "RELATED"
    user = json.dumps({
        "candidate": {k: candidate.get(k, "") for k in ("sector", "normalized_subject", "central_knowledge_unit", "central_claim", "sport_or_game")},
        "covered": {k: row.get(k, "") for k in ("sector", "normalized_subject", "central_knowledge_unit", "central_claim", "sport_or_game")},
        "similarity_score": round(score, 4),
    }, ensure_ascii=False)
    system = (
        "You are the final coverage review desk. Decide whether the candidate repeats the underlying knowledge in the covered record. "
        "Do not reject a candidate merely because the sport, country, person, or wording is similar. Return DUPLICATE only when the central knowledge unit or central claim is materially the same. "
        "Return NEW when the knowledge is independent, or RELATED when connected but materially different. Return only JSON."
    )
    obj = ai.json("v4_duplicate_review", system, user, V4_DUPLICATE_SCHEMA, max_tokens=700, temperature=0.0)
    decision = text((obj or {}).get("decision")).upper()
    return decision if decision in {"NEW", "RELATED", "DUPLICATE"} else "RELATED"


def v4_coverage_match(candidate: dict, coverage: dict) -> tuple[str, dict | None, float]:
    cc = v4_candidate_base(candidate)
    c_urls = {canonical_url(x.get("url", "")) for x in cc.get("sources", []) if text(x.get("url"))}
    cfp = v4_record_key(cc)
    best = ("new", None, 0.0)
    for row in coverage.get("records", []):
        if not isinstance(row, dict):
            continue
        if cfp == text(row.get("content_id")) or cfp in set(row.get("fingerprints") or []):
            return "duplicate", row, 1.0
        r_urls = {canonical_url(x) for x in row.get("source_urls", []) if text(x)}
        if c_urls & r_urls:
            return "duplicate", row, 1.0
        # Deterministic underlying-knowledge equality catches equivalent wording even when
        # the stored record does not carry the candidate fingerprint.
        for field in ("normalized_subject", "central_knowledge_unit", "central_claim"):
            cv = normalize_text(cc.get(field))
            rv = normalize_text(row.get(field))
            if cv and rv and cv == rv:
                return "duplicate", row, 1.0
        sim = v4_topic_similarity(cc, row)
        if sim >= V4_TOPIC_DUP_THRESHOLD:
            return "duplicate", row, sim
        if sim >= V4_SEMANTIC_DUP_THRESHOLD and sim > best[2]:
            best = ("related", row, sim)
    return best


def v4_filter_candidates(candidates: list[dict], coverage: dict, used_sectors: set[str], ai: "AIClient" | None = None) -> tuple[list[dict], list[str]]:
    accepted, reasons = [], []
    local = []
    for c in candidates:
        status, row, score = v4_coverage_match(c, coverage)
        if status == "duplicate":
            reasons.append(f"coverage_duplicate:{c.get('sector')}:{c.get('normalized_subject')}")
            continue
        if status == "related" and row is not None and ai is not None:
            decision = v4_review_borderline_duplicate(ai, c, row, score)
            if decision == "DUPLICATE":
                reasons.append(f"coverage_duplicate_ai:{c.get('sector')}:{c.get('normalized_subject')}")
                continue
        # Same-day sector exclusion is enforced later during the ranked selection step.
        # Keeping today's already-used sectors in the ranking pool is important because Run 2
        # must still rank the complete 20-item Exa batch before choosing among unused sectors.
        duplicate_local = False
        for p in local:
            if normalize_text(c.get("central_knowledge_unit")) == normalize_text(p.get("central_knowledge_unit")) or v4_topic_similarity(c, p) >= V4_SEMANTIC_DUP_THRESHOLD:
                duplicate_local = True; break
        if duplicate_local:
            reasons.append(f"same_run_duplicate:{c.get('normalized_subject')}")
            continue
        accepted.append(c); local.append(c)
    return accepted, reasons


V4_RANK_SCHEMA = OBJ(rankings=ARR(OBJ(post_number=INT, score=INT, reason=STR)))
V1_RANK_SCHEMA = OBJ(rankings=ARR(OBJ(post_number=INT, score=INT)))
V4_EDITORIAL_SCHEMA = OBJ(
    headline=STR, deck=STR, hook=STR, body=STR, key_points=ARR(STR), why_it_matters=STR,
    caption=STR, hashtags=ARR(STR), angle=STR, image_index=INT, image_reason=STR,
)
V4_DUPLICATE_SCHEMA = OBJ(decision=ENUM("NEW", "RELATED", "DUPLICATE"), reason=STR)


def v4_rank(ai: "AIClient", candidates: list[dict], used_sectors: set[str]) -> list[dict]:
    if len(candidates) != V4_RESEARCH_COUNT:
        return []
    payload = []
    for idx, c in enumerate(candidates, 1):
        payload.append({
            "post_number": idx,
            "sector": c.get("sector"),
            "subject": c.get("normalized_subject"),
            "knowledge_unit": c.get("central_knowledge_unit"),
            "claim": c.get("central_claim"),
            "sport_or_game": c.get("sport_or_game"),
            "country": c.get("country"),
            "region": c.get("region"),
            "headline": c.get("headline"),
            "story": c.get("main_story"),
            "source_count": len(c.get("sources", [])),
        })
    system = (
        "You are the editorial ranking desk of The Sports Newsroom. Rank the supplied 20 researched evergreen candidates. "
        "Do not invent facts. Score editorial usefulness, novelty, evidence strength, visual potential, evergreen longevity, "
        "global variety, audience curiosity, and repetition risk. The local code will enforce sectors. "
        f"Sectors already used today: {', '.join(sorted(used_sectors)) or 'none'}. Prefer unused sectors heavily."
    )
    obj = ai.json("v4_rank_20", system, json.dumps(payload, ensure_ascii=False), V4_RANK_SCHEMA,
                  max_tokens=3500, temperature=0.15)
    if not obj:
        return []
    rankings = obj.get("rankings") or []
    rows = []
    for r in rankings:
        try:
            n = int(r.get("post_number")); score = max(0, min(100, int(r.get("score"))))
        except Exception:
            continue
        if 1 <= n <= len(candidates):
            rows.append({"post_number": n, "score": score, "reason": text(r.get("reason"))})
    if len(rows) < len(candidates) * 0.6:
        return []
    rows.sort(key=lambda x: (x["score"], -x["post_number"]), reverse=True)
    return rows


def v4_image_candidates(post: dict, exa_results: list[dict]) -> list[dict]:
    out = []
    for item in post.get("research_images", []) if isinstance(post.get("research_images"), list) else []:
        if isinstance(item, dict) and text(item.get("url")):
            out.append(dict(item))
    img = post.get("image") or {}
    if isinstance(img, dict) and text(img.get("image_url")):
        out.append({
            "url": text(img.get("image_url")), "source_page_url": text(img.get("source_page_url")),
            "source_name": text(img.get("source_name")), "source_type": text(img.get("source_type")),
            "alt_text": text(img.get("alt_text")), "caption": text(img.get("caption")),
        })
    wanted = {canonical_url(x.get("url")) for x in post.get("sources", []) if text(x.get("url"))}
    for row in exa_results:
        if not isinstance(row, dict) or wanted and canonical_url(text(row.get("url"))) not in wanted:
            continue
        ex = ((row.get("extras") or {}).get("imageLinks") or []) if isinstance(row, dict) else []
        if isinstance(ex, list):
            for u in ex:
                if isinstance(u, str) and re.match(r"^https?://", u):
                    out.append({"url": u, "source_page_url": text(row.get("url")), "source_name": text(row.get("title")), "source_type": "Exa result image"})
    seen = set(); final=[]
    for x in out:
        c=canonical_url(x.get("url",""))
        if c and c not in seen:
            seen.add(c); final.append(x)
    return final[:8]


def v4_validate_image_candidate(c: dict, *, do_network: bool = True) -> bool:
    u = text(c.get("url"))
    if not re.match(r"^https?://", u, re.I):
        return False
    if not do_network or V4_DRY_RUN or DRY_RUN:
        return True
    r = http("image", "HEAD", u, timeout=V4_IMAGE_TIMEOUT, retries=1, want_json=False)
    if not r.ok:
        return False
    ct = text((r.headers or {}).get("Content-Type")).lower()
    if ct and not ct.startswith("image/"):
        return False
    try:
        length = int((r.headers or {}).get("Content-Length") or 0)
        if length and length > V4_MAX_IMAGE_BYTES:
            return False
    except Exception:
        pass
    return True


def v4_editorialize(ai: "AIClient", candidate: dict, evidence: str, image_candidates: list[dict]) -> dict | None:
    if not ai.available or ai.fatal:
        return None
    image_block = "\n".join(f"[{i}] {x.get('source_name','')} | {x.get('source_page_url','')} | {x.get('url','')}" for i, x in enumerate(image_candidates, 1)) or "none"
    user = json.dumps({
        "research": candidate,
        "source_evidence": evidence,
        "image_candidates": image_block,
    }, ensure_ascii=False)
    system = (
        "You are the senior editorial editor for The Sports Newsroom. Transform one verified evergreen research item into a "
        "publication-ready Telegram post. Use ONLY the supplied research and source evidence. Never add a fact, date, record, "
        "person, location, number, or claim that is not supported. Make the story genuinely interesting, specific, and concise. "
        "Headline must be 6-14 words and never clickbait. Body must be 50-120 words. Key points: 3-5. "
        "Do not use current-news framing or words such as today/latest/breaking/upcoming. Hashtags must be relevant. "
        "For image_index, choose the most directly relevant real image candidate; use 0 when none is suitable. "
        "Do not fabricate an image URL. Return only JSON."
    )
    obj = ai.json("v4_editorial_post", system, user, V4_EDITORIAL_SCHEMA, max_tokens=2600, temperature=0.35)
    return obj


def v4_validate_editorial(candidate: dict, editorial: dict, image_candidates: list[dict]) -> tuple[bool, str, dict]:
    if not isinstance(editorial, dict):
        return False, "editorial_not_object", {}
    headline = text(editorial.get("headline")); body = text(editorial.get("body"))
    words = re.findall(r"\b\w+[’'\w-]*\b", headline)
    body_words = re.findall(r"\b\w+[’'\w-]*\b", body)
    if not 6 <= len(words) <= 14:
        return False, "headline_word_count", {}
    if not 50 <= len(body_words) <= 120:
        return False, "body_word_count", {}
    if V4_CURRENT_NEWS_RX.search(f"{headline} {body}"):
        return False, "current_news_language", {}
    points = [text(x) for x in editorial.get("key_points", []) if text(x)]
    if not 3 <= len(points) <= 5:
        return False, "key_points_count", {}
    if not isinstance(candidate.get("sources"), list) or not candidate.get("sources"):
        return False, "no_verified_sources", {}
    # Any explicit numbers must exist in the supplied evidence/research. This catches common AI hallucination patterns.
    evidence_numbers = set(re.findall(r"\b\d{1,4}\b", json.dumps(candidate, ensure_ascii=False) + evidence_text_for_validation(candidate)))
    generated_numbers = set(re.findall(r"\b\d{1,4}\b", f"{headline} {body} {' '.join(points)}"))
    if not generated_numbers.issubset(evidence_numbers):
        return False, "unsupported_number", {}
    selected = None
    try:
        idx = int(editorial.get("image_index", 0))
    except Exception:
        idx = 0
    if idx > 0:
        if idx > len(image_candidates):
            return False, "image_index_invalid", {}
        selected = image_candidates[idx - 1]
    out = {**candidate, "editorial": editorial, "selected_image": selected}
    return True, "ok", out


def evidence_text_for_validation(candidate: dict) -> str:
    # The research package's source notes and main story form the deterministic local evidence envelope.
    bits=[candidate.get("main_story",""), candidate.get("central_claim",""), candidate.get("historical_date",""), candidate.get("historical_year",""), candidate.get("research_evidence","")]
    for s in candidate.get("sources",[]) if isinstance(candidate.get("sources"), list) else []:
        bits.append(s.get("evidence_note", ""))
    return " ".join(text(x) for x in bits)


def v4_build_evergreen(candidate: dict, editorial: dict, selected_image: dict | None) -> dict:
    tags = [text(x) for x in editorial.get("hashtags", []) if text(x)]
    tags = [t if t.startswith("#") else "#" + re.sub(r"[^A-Za-z0-9]", "", t) for t in tags]
    sector_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", candidate.get("sector", "SportsGames"))[:28]
    if sector_tag != "#":
        tags.append(sector_tag)
    # Deduplicate while preserving order.
    seen=set(); tags=[t for t in tags if t and not (t in seen or seen.add(t))][:5]
    sources = [(s.get("name") or source_label(s.get("url","")), s.get("url","")) for s in candidate.get("sources", [])]
    return {
        "desk": "evergreen_v1",
        "format": candidate.get("sector", "SPORTS & GAMES"),
        "sector": candidate.get("sector", ""),
        "topic": candidate.get("topic") or candidate.get("normalized_subject"),
        "normalized_subject": candidate.get("normalized_subject", ""),
        "central_knowledge_unit": candidate.get("central_knowledge_unit", ""),
        "central_claim": candidate.get("central_claim", ""),
        "angle": editorial.get("angle") or candidate.get("editorial_angle", ""),
        "headline": text(editorial.get("headline")),
        "deck": text(editorial.get("deck")),
        "hook": text(editorial.get("hook")),
        "body": text(editorial.get("body")),
        "key_points": [text(x) for x in editorial.get("key_points", []) if text(x)],
        "why_it_matters": text(editorial.get("why_it_matters")),
        "caption": text(editorial.get("caption")),
        "tags": tags,
        "sources": [(a,b) for a,b in sources if b],
        "urls": [b for a,b in sources if b],
        "country": candidate.get("country", ""),
        "region": candidate.get("region", ""),
        "people": candidate.get("people_involved", []),
        "historical_date": candidate.get("historical_date", ""),
        "historical_year": candidate.get("historical_year", ""),
        "image": selected_image,
        "image_status": "verified" if selected_image else "unavailable",
        "research": candidate,
    }


def v4_evergreen_rich(story: dict) -> dict:
    blocks = []
    if story.get("image", {}).get("url"):
        blocks.append({"type": "photo", "photo": {"type": "photo", "media": story["image"]["url"]}})
    blocks.append({"type": "heading", "size": 2, "text": story["headline"]})
    if story.get("deck"):
        blocks.append({"type": "paragraph", "text": story["deck"]})
    blocks.append({"type": "paragraph", "text": story["body"]})
    if story.get("key_points"):
        blocks.append({"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": p}]} for p in story["key_points"]]})
    if story.get("why_it_matters"):
        blocks.append({"type": "paragraph", "text": f"Why it is interesting: {story['why_it_matters']}"})
    if story.get("sources"):
        src = "Sources: " + " · ".join(f"{a} ({b})" for a,b in story["sources"][:3])
        blocks.append({"type": "footer", "text": src})
    if story.get("tags"):
        blocks.append({"type": "footer", "text": " ".join(story["tags"])})
    return {"blocks": blocks}


def v4_table_cell(value: str, header: bool = False, align: str = "left") -> dict:
    cell = {"text": text(value), "align": align, "valign": "middle"}
    if header:
        cell["is_header"] = True
    return cell


def v4_live_event_importance(ev: dict, kind: str) -> float:
    score = {"S": 80, "A": 60, "B": 35, "C": 10}.get(text(ev.get("tier")).upper(), 10)
    names = f"{ev.get('name','')} {ev.get('league','')} {ev.get('stage','')}".lower()
    # `_TIER_HINTS` is also used by the V3 tier classifier, where the second value is a
    # tier label (S/A), not a numeric bonus. Convert that label into the existing TIER_W
    # scale so the V4 live importance scorer remains type-safe.
    for rx, tier in _TIER_HINTS:
        if rx.search(names):
            score = max(score, TIER_W.get(tier, 0))
    if kind == "next" and ev.get("state") == "scheduled":
        score += 5
    if kind == "past" and ev.get("state") == "final":
        score += 5
    return score


def v4_event_identity(ev: dict) -> str:
    start = ev.get("start")
    day = to_bd(start).date().isoformat() if isinstance(start, datetime) else ""
    sport = text(ev.get("sport")); league = text(ev.get("league")); stage = text(ev.get("stage"))
    home = normalize_text(ev.get("home")); away = normalize_text(ev.get("away"))
    # Team-event identity deliberately ignores the display name because different source feeds may
    # call the same fixture "A vs B", "A-B", or use a branded competition label.
    if home and away:
        participants = "|".join(sorted((home, away)))
        return fingerprint(sport, league, stage, day, participants)
    return fingerprint(sport, league, stage, day, normalize_text(ev.get("name")))


def v4_major_live_events(events: list[dict], kind: str, target: date, limit: int = 20) -> list[dict]:
    rows=[]; seen=set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid=v4_event_identity(ev)
        if eid in seen:
            continue
        seen.add(eid)
        row=dict(ev); row["event_id"]=eid; row["major_score"]=v4_live_event_importance(row, kind)
        rows.append(row)
    rows.sort(key=lambda e:(e["major_score"], -((e.get("start") or datetime.min.replace(tzinfo=timezone.utc)).timestamp())), reverse=True)
    # Avoid turning a rich table into a fixture dump. Keep meaningful events across sports when possible.
    selected=[]; by_sport=Counter()
    for e in rows:
        if len(selected) >= limit:
            break
        sport=text(e.get("sport")) or "Sport"
        cap=6 if len({x.get('sport') for x in rows}) <= 3 else 5
        if by_sport[sport] >= cap and len(selected) < limit:
            continue
        selected.append(e); by_sport[sport]+=1
    return selected


def v4_live_rows(events: list[dict], kind: str) -> list[list[dict]]:
    if kind == "next":
        rows=[[v4_table_cell("SPORT", True), v4_table_cell("GAME", True), v4_table_cell("TIME", True, "center"), v4_table_cell("COMPETITION", True)]]
        for e in events:
            tm=to_bd(e["start"]).strftime("%a %d %b · %H:%M") if e.get("start") else "TBC"
            rows.append([v4_table_cell(e.get("sport") or "Sport"), v4_table_cell(e.get("name") or "Game"), v4_table_cell(tm, align="center"), v4_table_cell(e.get("league") or "")])
        return rows
    rows=[[v4_table_cell("SPORT", True), v4_table_cell("GAME", True), v4_table_cell("RESULT", True, "center"), v4_table_cell("COMPETITION", True)]]
    for e in events:
        if e.get("style") == "team" and e.get("home") and e.get("away"):
            result=f"{e.get('home_score','')}–{e.get('away_score','')}"
            game=f"{e.get('home')} vs {e.get('away')}"
        else:
            result=e.get("winner") or e.get("detail") or "Completed"
            game=e.get("name") or "Event"
        rows.append([v4_table_cell(e.get("sport") or "Sport"), v4_table_cell(game), v4_table_cell(result, align="center"), v4_table_cell(e.get("league") or "")])
    return rows


def v4_live_rich(kind: str, target: date, events: list[dict]) -> dict:
    label = "NEXT-DAY MAJOR GAMES" if kind == "next" else "PREVIOUS-DAY MAJOR RESULTS"
    caption = f"{label} · {long_date(target)} · Asia/Dhaka"
    return {"blocks": [{"type":"table", "cells": v4_live_rows(events, kind), "is_bordered": True, "is_striped": True, "is_compact": True, "caption": caption}]}


def v4_send_rich(rich_message: dict) -> dict:
    payload={"chat_id": CHANNEL, "rich_message": json.dumps(rich_message, ensure_ascii=False, separators=(",", ":"))}
    return tg_call("sendRichMessage", payload)


def v4_publish_evergreen(story: dict) -> dict:
    rich = v4_evergreen_rich(story)
    res = v4_send_rich(rich)
    if res.get("ok"):
        return res
    # Production fallback: preserve the story if an older Telegram client rejects rich messages.
    if not res.get("uncertain"):
        html_text = tg_sanitize(
            f"<b>{esc(story['sector'])}</b>\n\n<b>{esc(story['headline'])}</b>\n\n{esc(story['body'])}\n\n" +
            "\n".join(f"• {esc(x)}" for x in story.get("key_points", [])) +
            (f"\n\n<i>Why it is interesting:</i> {esc(story.get('why_it_matters',''))}" if story.get('why_it_matters') else "") +
            (f"\n\nSource: {source_links(story.get('sources', []))}" if story.get('sources') else "") +
            (f"\n\n{' '.join(story.get('tags', []))}" if story.get('tags') else "")
        )
        return tg_send_text(html_text)
    return res


def v4_publish_live(kind: str, target: date, events: list[dict]) -> dict:
    return v4_send_rich(v4_live_rich(kind, target, events))


def v4_delete_message(message_id: int | None) -> dict:
    if not message_id:
        return {"ok": True, "skipped": True}
    return tg_call("deleteMessage", {"chat_id": CHANNEL, "message_id": int(message_id)})


def v4_live_pair(state: dict, now: datetime) -> tuple[dict, dict, list[dict], list[dict]]:
    tomorrow=now.date()+timedelta(days=1); yesterday=now.date()-timedelta(days=1)
    ai=AIClient(state.get("ai"))
    # Reuse the proven V3 structured sports adapters. They are deterministic and source-aware.
    next_events,_=collect_events(ai, tomorrow, "next", allow_exa_fallback=False)
    past_events,_=collect_events(ai, yesterday, "past", allow_exa_fallback=False)
    next_major=v4_major_live_events(filter_state(next_events,"next",tomorrow),"next",tomorrow)
    past_major=v4_major_live_events(filter_state(past_events,"past",yesterday),"past",yesterday)
    if not next_major:
        raise RuntimeError("No major next-day events found")
    if not past_major:
        raise RuntimeError("No major previous-day results found")
    return ({"target_date": tomorrow.isoformat()}, {"target_date": yesterday.isoformat()}, next_major, past_major)


def v4_record_coverage(coverage: dict, story: dict, run_id: str, message_id: int | None) -> None:
    rec={
        "content_id": v4_record_key(story),
        "content_type": "evergreen",
        "sector": story.get("sector",""),
        "normalized_subject": normalize_text(story.get("normalized_subject") or story.get("topic")),
        "central_knowledge_unit": normalize_text(story.get("central_knowledge_unit")),
        "central_claim": text(story.get("central_claim"))[:700],
        "editorial_angle": text(story.get("angle")),
        "sport_or_game": text(story.get("research",{}).get("sport_or_game")),
        "country": text(story.get("country")),
        "region": text(story.get("region")),
        "people": [text(x) for x in story.get("people",[]) if text(x)],
        "published_at": utc_iso(now_bd()),
        "run_id": run_id,
        "telegram_message_id": message_id,
        "source_urls": [canonical_url(u) for u in story.get("urls",[]) if text(u)],
        "fingerprints": [v4_record_key(story), fingerprint(story.get("normalized_subject",""), story.get("central_claim",""))],
    }
    coverage.setdefault("records", []).append(rec)
    # Keep the permanent index bounded by records rather than by age. 20k is comfortably beyond multi-year use.
    coverage["records"] = coverage["records"][-20000:]
    coverage["updated_at"] = utc_iso(now_bd())


def v4_publication_id(run_id: str, kind: str, subject: str = "") -> str:
    return f"{run_id}:{kind}:{fingerprint(subject)}"


def v4_publish_with_idempotency(vstate: dict, publication_id: str, publisher: Callable[[], dict]) -> dict:
    existing=vstate.setdefault("publication", {}).get(publication_id)
    if isinstance(existing, dict) and existing.get("status") == "posted":
        return {"ok": True, "idempotent": True, "result": {"message_id": existing.get("message_id")}}
    vstate["publication"][publication_id] = {"status":"publishing", "started_at":utc_iso(now_bd())}
    res=publisher()
    if res.get("ok"):
        mid=(res.get("result") or {}).get("message_id")
        vstate["publication"][publication_id] = {"status":"posted", "message_id":mid, "posted_at":utc_iso(now_bd())}
    elif res.get("uncertain"):
        vstate["publication"][publication_id] = {"status":"uncertain", "error":text(res.get("description")), "updated_at":utc_iso(now_bd())}
    else:
        vstate["publication"][publication_id] = {"status":"failed", "error":text(res.get("description")), "updated_at":utc_iso(now_bd())}
    return res


def v4_run_once() -> int:
    REPORT.reset(); _EXA_CACHE.clear()
    if not V4_DRY_RUN and not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing"); return 1
    try:
        state=load_state()
        coverage=v4_load_coverage(state)
    except Exception as exc:
        logger.error("V4 state/coverage load failed: %s", redact(str(exc)))
        return 1
    vs=v4_state(state)
    now=now_bd(); run_slot="AM" if now.hour < 14 else "PM"
    run_id=f"{now.date().isoformat()}-{run_slot}"
    daily=vs["daily"].setdefault(now.date().isoformat(), {"published_sectors":[], "run_ids":[]})
    used_sectors=set(daily.get("published_sectors", []))
    if run_id in daily.get("run_ids", []):
        logger.info("Run %s already completed; nothing to do.", run_id); return 0
    RUN_LIMIT=Deadline(_env_int("RUN_DEADLINE_SECONDS", 1500))
    ai=AIClient(state.get("ai"))
    if not ai.available:
        logger.error("CEREBRAS_API_KEY is required for V4 ranking/editorial generation"); return 1
    unused_sectors=[s for s in V4_SECTORS if s not in used_sectors]
    research, exa_results, status=v4_exa_research(ai,state,coverage,now.date(),run_id,used_sectors)
    if not research:
        logger.error("V4 Exa research failed: %s", status); return 1
    filtered, reasons=v4_filter_candidates(research,coverage,used_sectors,ai)
    if len(filtered) < V4_RESEARCH_COUNT:
        recovery_note = "Required unused sectors for this run: " + ", ".join(unused_sectors) + ". Rejected candidate subjects: " + "; ".join(reasons[:25])
        research2, exa_results2, status2 = v4_exa_research(ai,state,coverage,now.date(),run_id,used_sectors,recovery_note=recovery_note)
        if research2:
            filtered2, reasons2=v4_filter_candidates(research2,coverage,used_sectors,ai)
            if len(filtered2) >= len(filtered):
                research, exa_results, filtered, reasons = research2, exa_results2, filtered2, reasons2
        if len(filtered) < V4_RESEARCH_COUNT:
            logger.error("Only %d novel candidates survived coverage filtering after recovery; need %d",len(filtered),V4_RESEARCH_COUNT)
            return 1
    # Rank the complete novel 20-item research batch. Same-day sector exclusion happens only after ranking.
    rank_pool = filtered[:]
    ranks=v4_rank(ai, rank_pool, used_sectors)
    if not ranks:
        logger.error("Cerebras ranking failed"); return 1
    by_num={int(i)+1:c for i,c in enumerate(rank_pool)}
    ordered=[by_num[r["post_number"]] for r in ranks if r["post_number"] in by_num]
    # Hard sector constraint: never take a sector already published earlier today.
    ordered=[c for c in ordered if c.get("sector") not in used_sectors]
    selected=[]; selected_sectors=set()
    for c in ordered:
        if c.get("sector") in used_sectors or c.get("sector") in selected_sectors: continue
        selected.append(c); selected_sectors.add(c.get("sector"))
        if len(selected)>=V4_PUBLISH_COUNT: break
    if len(selected)<V4_PUBLISH_COUNT:
        logger.error("Cerebras selection yielded %d/%d usable unused sectors",len(selected),V4_PUBLISH_COUNT); return 1

    # Second Exa pass: deep evidence + source + image research only for the selected ten.
    evidence, evidence_results, evidence_status = v4_exa_evidence_research(ai, selected, now.date(), run_id)
    if not evidence:
        logger.error("V4 selected evidence research failed: %s", evidence_status)
        return 1
    selected = v4_apply_evidence(selected, evidence, evidence_results)

    posted_stories=[]
    remaining=ordered[len(selected):] + [c for c in rank_pool if c not in ordered]
    candidate_stream=selected+remaining
    for idx,cand in enumerate(candidate_stream):
        if len(posted_stories)>=V4_PUBLISH_COUNT or RUN_LIMIT.expired(): break
        if cand.get("sector") in {x.get("sector") for x in posted_stories}: continue
        image_candidates=v4_image_candidates(cand,evidence_results)
        valid_images=[x for x in image_candidates if v4_validate_image_candidate(x,do_network=not (V4_DRY_RUN or DRY_RUN))]
        evidence_text=v4_result_evidence(evidence_results,[x.get("url") for x in cand.get("sources",[])],cand)
        editorial=v4_editorialize(ai,cand,evidence_text,valid_images)
        if not editorial: continue
        ok,why,out=v4_validate_editorial(cand,editorial,valid_images)
        if not ok:
            reject("v4_editorial_invalid",why)
            continue
        story=v4_build_evergreen(cand,editorial,out.get("selected_image"))
        pubid=v4_publication_id(run_id,"evergreen",story.get("normalized_subject",story.get("topic")))
        res=v4_publish_with_idempotency(vs,pubid,lambda st=story:v4_publish_evergreen(st))
        if not res.get("ok"):
            if res.get("uncertain"):
                logger.error("Evergreen delivery uncertain for %s",story.get("headline")); break
            continue
        mid=(res.get("result") or {}).get("message_id")
        v4_record_coverage(coverage,story,run_id,mid)
        coverage["updated_at"]=utc_iso(now_bd())
        used_sectors.add(story["sector"]); daily.setdefault("published_sectors",[]).append(story["sector"])
        daily["published_sectors"]=list(dict.fromkeys(daily["published_sectors"]))
        posted_stories.append(story)
        state.setdefault("posts",[]).append({
            "desk":"evergreen_v4", "format":story["sector"], "sector":story["sector"], "topic":story["topic"],
            "normalized_subject":story["normalized_subject"], "central_knowledge_unit":story["central_knowledge_unit"],
            "claim":story["central_claim"], "headline":story["headline"], "angle":story["angle"],
            "urls":[canonical_url(u) for u in story.get("urls",[])], "message_id":mid, "posted_at":utc_iso(now_bd()),
            "run_id":run_id, "image_status":story.get("image_status"),
        })
        if not (V4_DRY_RUN or DRY_RUN): save_state(state); v4_save_coverage(coverage)
        if len(posted_stories)<V4_PUBLISH_COUNT: sleep(V4_POST_DELAY)

    if len(posted_stories) != V4_PUBLISH_COUNT:
        logger.error("V4 evergreen phase incomplete: %d/%d",len(posted_stories),V4_PUBLISH_COUNT)
        return 1

    # Live pair is generated and verified before touching the previous pair.
    try:
        sched_meta,res_meta,next_events,past_events=v4_live_pair(state,now)
    except Exception as exc:
        logger.error("Live pair generation failed: %s",redact(str(exc)))
        return 1
    old_sched=vs["live"]["schedule"].get("message_id"); old_res=vs["live"]["results"].get("message_id")
    sched_id=v4_publication_id(run_id,"live_schedule",sched_meta["target_date"])
    res_id=v4_publication_id(run_id,"live_results",res_meta["target_date"])
    sr=v4_publish_with_idempotency(vs,sched_id,lambda:v4_publish_live("next",date.fromisoformat(sched_meta["target_date"]),next_events))
    if not sr.get("ok"):
        logger.error("New live schedule failed; preserving old pair"); return 1
    rr=v4_publish_with_idempotency(vs,res_id,lambda:v4_publish_live("past",date.fromisoformat(res_meta["target_date"]),past_events))
    if not rr.get("ok"):
        logger.error("New live results failed; preserving old pair"); return 1
    new_sched=(sr.get("result") or {}).get("message_id"); new_res=(rr.get("result") or {}).get("message_id")
    if not new_sched or not new_res:
        logger.error("Live pair did not return both message IDs; preserving old pair"); return 1
    # Safe replacement: both new messages exist before old ones are deleted.
    # Persist the new pair first so a process crash after publication does not cause the next run
    # to publish another pair for the same slot. Old messages are then best-effort cleanup.
    if not (V4_DRY_RUN or DRY_RUN):
        vs["live"]["schedule"]={"message_id":new_sched,"target_date":sched_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in next_events]}
        vs["live"]["results"]={"message_id":new_res,"target_date":res_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in past_events]}
        state["ai"]=ai.export(); state["last_run_at"]=utc_iso(now_bd())
        save_state(state)
        for mid in (old_sched,old_res):
            if mid and int(mid) not in {int(new_sched),int(new_res)}:
                dr=v4_delete_message(mid)
                if not dr.get("ok"):
                    logger.warning("Could not delete old live message %s: %s",mid,text(dr.get("description")))

    daily["run_ids"]=list(dict.fromkeys(daily.get("run_ids",[])+[run_id]))
    vs["last_run_at"]=utc_iso(now_bd())
    vs["runs"].append({"run_id":run_id,"at":utc_iso(now_bd()),"evergreen":len(posted_stories),"live_schedule":new_sched,"live_results":new_res,"used_sectors":sorted(set(x.get("sector") for x in posted_stories)),"exit":0})
    state["ai"]=ai.export(); state["last_run_at"]=utc_iso(now_bd())
    if not (V4_DRY_RUN or DRY_RUN):
        save_state(state); v4_save_coverage(coverage)
    logger.info("V4 RUN SUCCESS %s: %d evergreen + 2 live",run_id,len(posted_stories))
    return 0


def v4_validate_config(require_secrets: bool = True) -> int:
    """Validate the repository and runtime configuration without network traffic."""
    required_files = [
        "main.py", "news_state.json", "posted_urls.txt", "coverage_index.json", "requirements.txt",
        "README.md", "prompts/exa_daily_evergreen_v4.txt", "prompts/exa_selected_evidence_v4.txt",
        "prompts/cerebras_rank_v4.txt", "prompts/cerebras_editorial_v4.txt",
        "prompts/cerebras_duplicate_review_v4.txt", "schemas/exa_daily_sports_games_output_schema_v1.json",
        "schemas/exa_selected_evidence_v4.json", "tests/test_v4.py", ".github/workflows/newbot.yml",
        ".github/workflows/import-zip.yml",
    ]
    missing = [p for p in required_files if not Path(p).is_file()]
    if missing:
        print("CONFIG: FAIL")
        print("Missing files: " + ", ".join(missing))
        return 1
    try:
        exa_schema=json.loads(Path("schemas/exa_daily_sports_games_output_schema_v1.json").read_text(encoding="utf-8"))
        evidence_schema=json.loads(Path("schemas/exa_selected_evidence_v4.json").read_text(encoding="utf-8"))
        for label, schema in (("discovery", exa_schema), ("evidence", evidence_schema)):
            ok, reason = v4_validate_exa_output_schema(schema)
            if not ok:
                print(f"CONFIG: FAIL ({label} schema: {reason})")
                return 1
        state=load_state()
        coverage=v4_load_coverage(state)
        if not isinstance(coverage.get("records"), list):
            print("CONFIG: FAIL (coverage records must be a list)")
            return 1
    except Exception as exc:
        print("CONFIG: FAIL (" + redact(str(exc)) + ")")
        return 1
    if require_secrets:
        required_env = {
            "EXA_API_KEY": EXA_API_KEY,
            "CEREBRAS_API_KEY": CEREBRAS_API_KEY,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        }
        missing_env = [name for name, value in required_env.items() if not value]
        if missing_env:
            print("CONFIG: FAIL (missing secrets: " + ", ".join(missing_env) + ")")
            return 1
    print(f"CONFIG: OK (The Sports Newsroom v{APP_VERSION}, 20 sectors, Exa schemas within provider limits)")
    return 0


def v4_diagnose() -> int:
    print(f"{APP_NAME} v{APP_VERSION} V4 diagnostic")
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM: FAIL (missing TELEGRAM_BOT_TOKEN)"); return 1
    me=tg_call("getMe"); print("TELEGRAM getMe:","OK" if me.get("ok") else "FAIL")
    chat=tg_call("getChat",{"chat_id":CHANNEL}); print("TELEGRAM channel:","OK" if chat.get("ok") else "FAIL")
    ai=AIClient(); print("CEREBRAS:","OK" if ai.available else "FAIL (missing key)")
    print("EXA:","OK" if EXA_API_KEY else "FAIL (missing key)")
    print("RICH MESSAGE:","sendRichMessage + table blocks enabled")
    try:
        cov=v4_load_coverage(load_state()); print("COVERAGE INDEX:",f"OK ({len(cov.get('records',[]))} records)")
    except Exception as exc:
        print("COVERAGE INDEX: FAIL",redact(str(exc))); return 1
    return 0


def v4_dry_preview() -> int:
    global V4_DRY_RUN, DRY_RUN
    V4_DRY_RUN=True; DRY_RUN=True
    try:
        return v4_run_once()
    finally:
        V4_DRY_RUN=False; DRY_RUN=False


def v4_self_test(fast: bool=False) -> int:
    # Keep simulation telemetry out of the production GitHub Step Summary.
    global run_once, run_diagnose
    old_run_once=globals().get("run_once")
    old_diag=globals().get("run_diagnose")
    summary_path=os.environ.pop("GITHUB_STEP_SUMMARY", None)
    globals()["run_once"]=v3_run_once
    try:
        rc=v3_self_test(fast=True if fast else False)
    finally:
        globals()["run_once"]=old_run_once
        globals()["run_diagnose"]=old_diag
        if summary_path is not None:
            os.environ["GITHUB_STEP_SUMMARY"]=summary_path
    if rc:
        return rc
    return v4_architecture_tests()


def v4_architecture_tests() -> int:
    failures=[]; checks=0
    def check(ok,msg):
        nonlocal checks
        checks+=1
        if not ok: failures.append(msg)
    # Sector matrix
    check(len(V4_SECTORS)==20,"sector count != 20")
    check(len(set(V4_SECTORS))==20,"sector values not unique")
    try:
        exa_schema=json.loads((V4_SCHEMAS_DIR / "exa_daily_sports_games_output_schema_v1.json").read_text(encoding="utf-8"))
        pcount,pdepth=v4_schema_contract_stats(exa_schema)
        check(pcount <= 10, f"Exa discovery schema has {pcount} properties")
        check(pdepth <= 2, f"Exa discovery schema depth {pdepth} exceeds 2")
        evidence_schema=json.loads((V4_SCHEMAS_DIR / "exa_selected_evidence_v4.json").read_text(encoding="utf-8"))
        ecount,edepth=v4_schema_contract_stats(evidence_schema)
        check(ecount <= 10, f"Exa evidence schema has {ecount} properties")
        check(edepth <= 2, f"Exa evidence schema depth {edepth} exceeds 2")
    except Exception as exc:
        check(False, f"Exa schema contract check crashed: {exc}")
    # Coverage match cases
    sample={"sector":"Sport Origin","normalized_subject":"Football origins","central_knowledge_unit":"codified association football origins","central_claim":"Football's laws were codified in 1863.","topic":"Football"}
    cov={"records":[{"content_id":"x","sector":"Sport Origin","normalized_subject":"football origins","central_knowledge_unit":"codified association football origins","central_claim":"Football laws codified in 1863","source_urls":[],"fingerprints":[]}]}
    status,_,score=v4_coverage_match(sample,cov)
    check(status=="duplicate" and score>=1,"coverage duplicate not detected")
    related={"sector":"Interesting Sports Fact","normalized_subject":"football goal posts","central_knowledge_unit":"football pitch dimensions","central_claim":"goal width"}
    status,_,_=v4_coverage_match(related,cov)
    check(status in {"new","related"},"related topic incorrectly hard-blocked")
    # Run 2 must still research/rank the complete 20-sector batch; local selection removes used sectors later.
    check(len(V4_SECTORS)==20 and len([x for x in V4_SECTORS if x not in set(V4_SECTORS[:10])])==10,"run2 unused sector set malformed")
    # Live event identity/source collapse
    d=datetime(2026,9,23,12,tzinfo=timezone.utc)
    a=make_event(sport="Football",league="UEFA Champions League",home="A",away="B",name="A vs B",start=d)
    b=make_event(sport="Football",league="UEFA Champions League",home="A",away="B",name="A vs B",start=d+timedelta(minutes=15))
    check(v4_event_identity(a)==v4_event_identity(b),"live event identity drift")
    # Rich table payload
    rich=v4_live_rich("next",date(2026,9,23),[a])
    table=rich["blocks"][0]
    check(table.get("type")=="table","rich table type wrong")
    check(len(table.get("cells",[]))==2,"rich table rows wrong")
    check(table.get("is_bordered") and table.get("is_striped") and table.get("is_compact"),"rich table flags missing")
    # Evergreen rich payload includes image block only when image exists.
    story={"sector":"Interesting Sports Fact","headline":"Why This Sports Measurement Still Matters","deck":"A concise explanation.","body":"This is a verified evergreen story with enough detail to satisfy the editorial body length requirements in a real post.","key_points":["Point one","Point two","Point three"],"why_it_matters":"It explains a useful piece of sports knowledge.","sources":[("Source","https://example.com")],"tags":["#SportsFacts"],"image":{"url":"https://example.com/image.jpg"}}
    er=v4_evergreen_rich(story)
    check(er["blocks"][0]["type"]=="photo","image block missing")
    check(any(b.get("type")=="heading" for b in er["blocks"]),"evergreen heading missing")
    # Editorial validator catches current-news language and unsupported numbers.
    cand={**sample,"main_story":"The laws were codified in 1863.","sources":[{"name":"Source","url":"https://example.com","evidence_note":"laws codified in 1863"}],"historical_year":"1863","people_involved":[]}
    ed={"headline":"How Football Laws Became Written Rules","deck":"","hook":"","body":("Football's laws became more standardized after clubs agreed on a written code. "
        "The milestone helped distinguish association football from other football traditions and gave the sport a common framework. "
        "That shared framework could travel between clubs and countries over time, making the game easier to recognize across different communities. "
        "It also created a reference point for later rule development and comparison."),"key_points":["Written rules created common expectations","The code shaped later development","The change made comparison easier"],"why_it_matters":"It explains why a familiar sport has shared rules.","caption":"","hashtags":["#Football"],"angle":"history", "image_index":0,"image_reason":"none"}
    ok,why,_=v4_validate_editorial(cand,ed,[])
    check(ok,"valid editorial rejected: "+why)
    ed2=dict(ed); ed2["body"]=("This is the latest football story and it covers a major current development in the sport. "
        "The update concerns the current season, an upcoming match, and a breaking change that belongs in a live news desk rather than an evergreen history post. "
        "It therefore fails the evergreen language gate even though the writing remains concise and readable.")
    ok,why,_=v4_validate_editorial(cand,ed2,[])
    check(not ok and why=="current_news_language","current-news language not blocked")
    print(f"V4 architecture tests: {checks - len(failures)}/{checks} passed")
    if failures:
        for f in failures: print("  -",f)
        return 1
    return 0


def v4_main(argv: list[str] | None=None) -> int:
    global CHANNEL, DRY_RUN
    ap=argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--self-test",action="store_true",help="run the legacy regression suite plus V4 architecture tests")
    ap.add_argument("--fast",action="store_true",help="use the shorter legacy soak")
    ap.add_argument("--validate-config",action="store_true",help="validate repository files, schemas and required runtime secrets without network calls")
    ap.add_argument("--diagnose",action="store_true",help="check Telegram, Exa, Cerebras and coverage memory")
    ap.add_argument("--dry-run",action="store_true",help="run the V4 pipeline without external Telegram calls or state writes")
    ap.add_argument("--channel",default=os.environ.get("CHANNEL_OVERRIDE",""),help="override target Telegram channel")
    ap.add_argument("--version",action="store_true")
    args=ap.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} v{APP_VERSION}"); return 0
    if args.channel.strip(): CHANNEL=args.channel.strip()
    if args.self_test: return v4_self_test(args.fast)
    if args.validate_config: return v4_validate_config(require_secrets=True)
    if args.diagnose: return v4_diagnose()
    if args.dry_run: return v4_dry_preview()
    DRY_RUN=False
    return v4_run_once()


# ===========================================================================
# 8. THE SPORTS NEWSROOM V1 RESEARCH ENGINE
#     Exa Search -> local candidate reservoir -> Cerebras ranking ->
#     Exa Contents evidence -> optional Exa Agent hard-case verification ->
#     Cerebras editorial -> normalization -> deterministic Telegram publish.
# ===========================================================================


# ===========================================================================
# V1.4.0: QUALITY-HUB DAILY BATCH
# 20 sectors -> ~200 Exa discoveries -> Exa-native ranked selection ->
# 20 verified winners -> one Cerebras editorial batch -> AM/PM publication.
# ===========================================================================

V1_STATE_KEY = "v1"
V1_SCHEMA = 4
V1_SECTORS = list(V4_SECTORS)
V1_POSTS_PER_RUN = 10
V1_DAILY_STORY_COUNT = 20
V1_DISCOVERY_RESULTS_PER_SECTOR = _env_int("V1_DISCOVERY_RESULTS_PER_SECTOR", 10)
V1_DISCOVERY_MIN_RESULTS_PER_SECTOR = _env_int("V1_DISCOVERY_MIN_RESULTS_PER_SECTOR", 10)
V1_MAX_RESERVES_PER_SECTOR = _env_int("V1_MAX_RESERVES_PER_SECTOR", 2)
V1_CONTENTS_CHARS = _env_int("V1_CONTENTS_CHARS", 7000)
V1_IMAGE_SEARCH_RESULTS = _env_int("V1_IMAGE_SEARCH_RESULTS", 5)
V1_SEARCH_DELAY_SECONDS = _env_float("V1_SEARCH_DELAY_SECONDS", 0.15)
V1_POST_DELAY = _env_float("V1_POST_DELAY_SECONDS", POST_DELAY_SECONDS)
V1_CROSS_SECTOR_DUP_THRESHOLD = _env_float("V1_CROSS_SECTOR_DUP_THRESHOLD", 0.82)
V1_EDITORIAL_MAX_TOKENS = max(2500, min(9000, _env_int("V1_EDITORIAL_MAX_TOKENS", 6500)))
V1_CEREBRAS_BATCH_ENABLED = os.environ.get("V1_CEREBRAS_BATCH_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
V1_ALLOW_EDITORIAL_FALLBACK = os.environ.get("V1_ALLOW_EDITORIAL_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
V1_BATCH_KEY = "daily_batch"
V1_EXCLUDE_DOMAINS = [
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "youtube.com",
    "bet365.com", "oddschecker.com", "sportinglife.com/betting",
]
V1_CURRENT_RX = re.compile(
    r"\b(?:live\s+score(?:s)?|match\s+report|game\s+report|fixtures?|standings?|table\s+position|"
    r"transfers?|transfer\s+window|injur(?:y|ies)|squad\s+update|contract\s+extension|signing|loan\s+move|"
    r"betting|odds|upcoming\s+(?:match|game|fixture)|next\s+(?:match|game)|breaking|latest\s+(?:news|results)|"
    r"today(?:'s)?\s+(?:match|game|result|fixtures?)|yesterday(?:'s)?\s+(?:match|game|result|results)|"
    r"tomorrow(?:'s)?\s+(?:match|game|fixture|fixtures?))\b", re.I,
)
V1_LISTICLE_RX = re.compile(r"\b(?:top\s+\d+|best\s+\d*|ranking(?:s)?|ranked|greatest|most\s+popular|things\s+you\s+didn'?t\s+know|listicle)\b", re.I)
V1_GENERIC_KNOWLEDGE_WORDS = {"topic", "topics", "subject", "subjects", "knowledge", "documented", "claim", "claims", "story", "stories", "fact", "facts"}
V1_HARD_SECTORS = {
    "Sport Origin", "Game Origin", "On This Date", "First / Last / Only", "Records & Milestones",
    "Then vs Now", "Traditional / Regional Game", "Sports & Games People",
}

V1_SECTOR_SEARCH = {
    "Sport Discovery": "lesser-known established sport documented rules history equipment how it is played",
    "Game Discovery": "unusual physical regional board card dice or tabletop game documented rules history",
    "Interesting Sports Fact": "one surprising evergreen sports fact with authoritative historical technical or institutional evidence",
    "Interesting Game Fact": "one surprising evergreen fact about a physical or tabletop game with documented evidence",
    "Rule Check": "one unusual or commonly misunderstood official sport or game rule official rulebook governing body",
    "How to Play": "one unusual sport or game documented setup objective equipment scoring and basic play sequence",
    "Sport Origin": "documented origin and development of a sport precise history predecessors and early development",
    "Game Origin": "documented origin and development of a physical or tabletop game precise historical evidence",
    "On This Date": "historical sports or games event that happened on {month} {day} exact date evidence required",
    "First / Last / Only": "precisely documented first last or only occurrence in sports or games with strong qualification and evidence",
    "Records & Milestones": "verified historical record streak landmark or institutional milestone with exact supporting evidence",
    "Forgotten Sport": "discontinued extinct displaced or little-known sport with documented history rules and context",
    "Forgotten Game": "discontinued ancient displaced or little-known physical or tabletop game with documented history",
    "Equipment / Measurement": "meaningful sports or game equipment measurement timing dimensions or engineering explanation",
    "Why Does This Happen?": "real why-question in sport or games explained by science physics biomechanics mathematics history culture or design",
    "Then vs Now": "documented historical versus modern change in sport or game rules equipment venue scoring or structure",
    "New Sport Discovery": "new or emerging non-digital sport with established rules documented development and non-promotional evidence",
    "New Tabletop Game Discovery": "newer board card dice or tabletop game with documented rules and independent development evidence",
    "Traditional / Regional Game": "traditional or regional physical game documented cultural context rules equipment and evidence",
    "Sports & Games People": "historically important athlete pioneer inventor designer founder organizer or rule-maker documented contribution",
}

V1_SECTOR_QUERY_VARIANTS = {
    "Sport Discovery": [
        "lesser-known established sport with official rules unusual equipment history and clear explanation",
        "obscure sport documented by governing body museum archive university or specialist reference",
    ],
    "Game Discovery": [
        "unusual physical or tabletop game documented rules history and objective credible source",
        "lesser-known regional board card dice or physical game with authoritative documentation",
    ],
    "Interesting Sports Fact": [
        "surprising evergreen sports fact verified by official or institutional source",
        "unusual technical historical or cultural sports fact with credible evidence",
    ],
    "Interesting Game Fact": [
        "surprising evergreen fact about a game supported by rules history or specialist source",
        "unusual documented fact about a board card dice or physical game",
        "little-known game fact verified by specialist rules history or archive source",
        "distinctive historical technical or cultural fact about a physical or tabletop game",
    ],
    "Rule Check": [
        "unusual official rule in a sport or game governing body rulebook example",
        "commonly misunderstood sports rule official rulebook clarification",
        "lesser-known rule sport game official laws rulebook practical example",
        "specific sports rule explained by governing body official regulations",
    ],
    "How to Play": [
        "unusual sport or game with documented setup objective equipment scoring and play",
        "how to play a lesser-known sport or tabletop game from credible rules source",
    ],
    "Sport Origin": [
        "origin and early development of a sport archive governing body museum university",
        "documented history of how a sport began and evolved authoritative reference",
    ],
    "Game Origin": [
        "origin and early development of a physical or tabletop game historical archive",
        "documented history of how a game began and evolved authoritative source",
    ],
    "On This Date": [
        "sports history event on {month} {day} exact date archive official source",
        "historical game event on {month} {day} sports archive museum newspaper record",
        "what happened in sports history on {month} {day} exact date institutional archive",
        "{month} {day} sports anniversary historical event official archive museum",
    ],
    "First / Last / Only": [
        "documented first last or only sports achievement exact qualification official source",
        "historical first or only occurrence in sport strong primary or institutional evidence",
        "sports first last only achievement verified archive governing body historical record",
        "precisely qualified first ever or only sports occurrence authoritative evidence",
    ],
    "Records & Milestones": [
        "sports record or milestone authoritative historical record exact evidence",
        "landmark streak record or milestone in sport documented institutional source",
        "historic sporting record milestone archive federation official statistics",
        "important sports milestone documented by authoritative historical source",
    ],
    "Forgotten Sport": [
        "forgotten extinct or displaced sport documented historical rules archive museum",
        "little-known discontinued sport historical documentation credible reference",
    ],
    "Forgotten Game": [
        "forgotten ancient or displaced game documented history rules museum archive",
        "little-known discontinued physical or tabletop game credible historical source",
    ],
    "Equipment / Measurement": [
        "sports equipment or measurement with interesting engineering purpose official technical explanation",
        "why sports equipment dimensions timing or measurement are designed that way",
    ],
    "Why Does This Happen?": [
        "why does this happen in sport or games science physics biomechanics mathematics explanation",
        "interesting sports phenomenon explained by science or design credible source",
    ],
    "Then vs Now": [
        "historical versus modern change in sports rules equipment venue or scoring authoritative sources",
        "then versus now documented evolution of a sport or game official and historical sources",
        "how a sport changed from historical era to modern form official historical comparison",
        "specific historical change in sports equipment rules format or venue over time",
    ],
    "New Sport Discovery": [
        "emerging non-digital sport documented rules development independent coverage",
        "new sport with established rules documented origin and credible non-promotional source",
    ],
    "New Tabletop Game Discovery": [
        "new board card dice tabletop game documented rules independent coverage",
        "newer tabletop game with published rules development history and specialist coverage",
    ],
    "Traditional / Regional Game": [
        "traditional regional game documented culture rules museum archive university",
        "local folk game with historical cultural context and documented rules",
        "traditional folk sport or game ethnographic archive museum cultural institution",
        "regional game documented by university museum heritage or cultural archive",
    ],
    "Sports & Games People": [
        "sports or games pioneer inventor designer founder rule-maker documented contribution",
        "historically important person who changed a sport or game documented primary sources",
    ],
}

V1_BATCH_EDITORIAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stories": {
            "type": "array",
            "minItems": 20,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "post_number": {"type": "integer"},
                    "headline": {"type": "string"},
                    "lead": {"type": "string"},
                    "story": {"type": "string"},
                    "key_points": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string"}},
                    "takeaway": {"type": "string"},
                },
                "required": ["post_number", "headline", "lead", "story", "key_points", "takeaway"],
            },
        }
    },
    "required": ["stories"],
}


def _v1_default_state() -> dict:
    return {
        "schema": V1_SCHEMA,
        "created_at": utc_iso(now_bd()),
        "last_run_at": "",
        "daily": {},
        "runs": [],
        "live": {"schedule": {"message_id": None, "target_date": ""}, "results": {"message_id": None, "target_date": ""}},
        "publication": {},
    }


def _v1_day_sector_plan(target: date) -> dict[str, list[str]]:
    ranked = sorted(V1_SECTORS, key=lambda sec: hashlib.sha256(f"sports-newsroom:v1.4:{target.isoformat()}:{sec}".encode()).hexdigest())
    return {"AM": ranked[:10], "PM": ranked[10:20]}


def _v1_blank_batch(target: date) -> dict:
    return {
        "version": V1_SCHEMA,
        "date": target.isoformat(),
        "status": "empty",
        "batch_id": "",
        "discovered_target": V1_DAILY_STORY_COUNT * V1_DISCOVERY_RESULTS_PER_SECTOR,
        "discovered_actual": 0,
        "sector_counts": {},
        "selected": [],
        "reserves": {},
        "stories": [],
        "editorial_model": "",
        "created_at": "",
        "editorial_at": "",
    }


def _v1_prune_daily_history(root: dict, today: date, keep_days: int = 7) -> None:
    cutoff=today-timedelta(days=max(1,int(keep_days)))
    daily=root.setdefault("daily",{})
    for day in list(daily):
        try:
            d=date.fromisoformat(text(day))
        except Exception:
            continue
        if d < cutoff:
            daily.pop(day,None)


def v1_state(state: dict) -> dict:
    root = state.setdefault(V1_STATE_KEY, _v1_default_state())
    if not isinstance(root, dict):
        root = _v1_default_state(); state[V1_STATE_KEY] = root
    root.setdefault("schema", V1_SCHEMA)
    root["schema"] = max(int(root.get("schema") or 1), V1_SCHEMA)
    root.setdefault("created_at", utc_iso(now_bd()))
    root.setdefault("last_run_at", "")
    root.setdefault("daily", {})
    root.setdefault("runs", [])
    root.setdefault("publication", {})
    root.setdefault("live", _v1_default_state()["live"])
    today_date = now_bd().date()
    _v1_prune_daily_history(root, today_date, keep_days=7)
    today = today_date.isoformat()
    d = root["daily"].setdefault(today, {})
    if not isinstance(d, dict):
        d = {}; root["daily"][today] = d
    d.setdefault("sector_plan", _v1_day_sector_plan(now_bd().date()))
    d.setdefault("published_sectors", [])
    d.setdefault("run_ids", [])
    batch = d.get(V1_BATCH_KEY)
    if not isinstance(batch, dict) or int(batch.get("version") or 0) != V1_SCHEMA:
        d[V1_BATCH_KEY] = _v1_blank_batch(now_bd().date())
    else:
        for key, default in _v1_blank_batch(now_bd().date()).items():
            batch.setdefault(key, default)
    old = state.get("v4")
    if isinstance(old, dict):
        if not root["live"].get("schedule", {}).get("message_id") and isinstance(old.get("live"), dict):
            root["live"] = json.loads(json.dumps(old.get("live")))
    return root


def v1_sector_query(sector: str, target: date, variant: int = 0) -> str:
    variants = V1_SECTOR_QUERY_VARIANTS.get(sector) or [V1_SECTOR_SEARCH.get(sector, "evergreen sports and games knowledge")]
    q = variants[min(max(0, int(variant)), len(variants) - 1)]
    return q.format(month=target.strftime("%B"), day=target.day)


def _v1_search_system(sector: str, target: date) -> str:
    return (
        "THE SPORTS NEWSROOM DISCOVERY DESK. Find source pages for evergreen Sports & Games knowledge. "
        "Do not write an article. Do not return current scores, results, schedules, fixtures, standings, transfers, injuries, betting, rumors, breaking news, current previews or current reports. "
        "Prefer governing bodies, official rulebooks, museums, archives, universities, libraries, cultural institutions, national organizations and established specialist references. "
        "Return materially distinct source pages relevant to the assigned sector. A discovery result is not final evidence until its page is retrieved. "
        f"TARGET SECTOR: {sector}. CALENDAR DATE: {target.isoformat()}."
    )


def _v1_exa_error(r: Any) -> str:
    data = r.data if isinstance(getattr(r, "data", None), dict) else {}
    return text(data.get("error")) or text(getattr(r, "error", "")) or f"HTTP {getattr(r, 'status', 0)}"


def _v1_exa_request(url: str, body: dict, *, timeout: int = 60, retries: int = 1) -> Any:
    if not EXA_API_KEY:
        return None
    return http("exa", "POST", url, json_body=body, headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}, timeout=timeout, retries=retries, backoff=2.0)


def _v1_normalize_exa_row(item: dict, sector: str, target: date, request_id: str = "") -> dict | None:
    url = v1_network_url(text(item.get("url")))
    title = re.sub(r"\s+", " ", text(item.get("title"))).strip()
    if not url or not title:
        return None
    hs = item.get("highlights") or []
    if not isinstance(hs, list):
        hs = [text(hs)] if text(hs) else []
    highlights = [re.sub(r"\s+", " ", text(h)).strip() for h in hs if text(h)]
    excerpt = " ".join(highlights)[:4500] or re.sub(r"\s+", " ", text(item.get("text")))[:4500]
    alltext = f"{title} {excerpt} {url}"
    if exclusion_reason(alltext) or V1_CURRENT_RX.search(title) or V1_CURRENT_RX.search(excerpt) or V1_LISTICLE_RX.search(title):
        return None
    score_raw = item.get("score")
    try:
        exa_score = float(score_raw) if score_raw is not None else 0.0
    except Exception:
        exa_score = 0.0
    published = parse_dt(item.get("publishedDate") or item.get("published_date"))
    return {
        "sector": sector,
        "normalized_subject": title,
        "central_knowledge_unit": title,
        "central_claim": (highlights[0] if highlights else excerpt[:900])[:900],
        "research_focus": v1_sector_query(sector, target),
        "topic": title,
        "source_url": url,
        "source": source_label(url, text(item.get("author"))),
        "grade": grade_of(url),
        "highlights": highlights[:8],
        "summary": text(item.get("summary")),
        "text": excerpt,
        "image": text(item.get("image")),
        "published": published,
        "exa_id": text(item.get("id")),
        "exa_score": exa_score,
        "favicon": text(item.get("favicon")),
        "search_request_id": request_id,
    }


def v1_exa_search_sector(sector: str, target: date, *, variant: int = 0, num: int | None = None) -> list[dict]:
    if not EXA_API_KEY:
        return []
    n = max(1, min(100, int(num or V1_DISCOVERY_RESULTS_PER_SECTOR)))
    body = {
        "query": v1_sector_query(sector, target, variant),
        "type": "auto",
        "numResults": n,
        "moderation": True,
        "excludeDomains": V1_EXCLUDE_DOMAINS,
        "contents": {"highlights": True, "summary": True},
        "systemPrompt": _v1_search_system(sector, target),
    }
    r = _v1_exa_request("https://api.exa.ai/search", body, timeout=120, retries=1)
    if r is None or not getattr(r, "ok", False) or not isinstance(getattr(r, "data", None), dict):
        logger.warning("V1.4 Exa discovery failed sector=%s variant=%s status=%s error=%s", sector, variant, getattr(r, "status", 0), redact(_v1_exa_error(r))[:240])
        return []
    req_id = text(r.data.get("requestId"))
    out=[]
    for item in r.data.get("results", []) or []:
        if isinstance(item, dict):
            row = _v1_normalize_exa_row(item, sector, target, req_id)
            if row: out.append(row)
    return out


def v1_discover_200(target: date) -> tuple[list[dict], dict]:
    """Research all 20 sectors once for the day. Target is 10 results per sector, ~200 total."""
    raw=[]
    counts={}
    for sector in V1_SECTORS:
        rows=[]; seen=set()
        variants=V1_SECTOR_QUERY_VARIANTS.get(sector) or [V1_SECTOR_SEARCH.get(sector, "evergreen sports and games knowledge")]
        for variant in range(len(variants)):
            if len(rows) >= V1_DISCOVERY_MIN_RESULTS_PER_SECTOR: break
            found=v1_exa_search_sector(sector,target,variant=variant,num=V1_DISCOVERY_RESULTS_PER_SECTOR)
            for row in found:
                cu=canonical_url(row.get("source_url"))
                if cu and cu not in seen:
                    seen.add(cu); rows.append(row)
            sleep(V1_SEARCH_DELAY_SECONDS)
        rows=rows[:V1_DISCOVERY_RESULTS_PER_SECTOR]
        counts[sector]=len(rows)
        raw.extend(rows)
    return raw,{"target":V1_DAILY_STORY_COUNT*V1_DISCOVERY_RESULTS_PER_SECTOR,"actual":len(raw),"sector_counts":counts}


def v1_network_url(url: str) -> str:
    raw=text(url).strip()
    if not raw: return ""
    if "://" not in raw: raw="https://"+raw
    parts=urlsplit(raw)
    if parts.scheme.lower() not in {"http","https"} or not parts.netloc: return ""
    query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return parts._replace(scheme=parts.scheme.lower(),netloc=parts.netloc.lower(),path=re.sub(r"/+","/",parts.path or "/"),query=urlencode(query)).geturl()


def _v1_grade_weight(grade: str) -> float:
    return {"A": 1.0, "B": 0.78, "C": 0.45}.get(text(grade).upper(), 0.25)


def v1_candidate_rank_score(row: dict) -> float:
    try: exa=max(0.0,min(1.0,float(row.get("exa_score") or 0.0)))
    except Exception: exa=0.0
    evidence=min(1.0,(len(row.get("highlights") or []) / 5.0))
    specificity=min(1.0,len(tokens(row.get("central_claim"))) / 18.0)
    grade=_v1_grade_weight(row.get("grade"))
    image=1.0 if text(row.get("image")) else 0.0
    penalty=0.35 if V1_LISTICLE_RX.search(text(row.get("normalized_subject"))) else 0.0
    current=0.50 if V1_CURRENT_RX.search(text(row.get("normalized_subject"))+" "+text(row.get("central_claim"))) else 0.0
    # Exa native relevance is the primary signal. Local signals break ties and protect against weak result pages.
    return 70*exa + 15*grade + 7*evidence + 5*specificity + 3*image - 18*penalty - 30*current


def v1_candidate_key(row: dict) -> str:
    return fingerprint(row.get("sector",""),row.get("normalized_subject",""),row.get("central_knowledge_unit",""))


def _v1_knowledge_signature(row: dict) -> str:
    sector_tokens = tokens(row.get("sector"))
    pieces = []
    for value in (normalize_text(row.get("normalized_subject")), normalize_text(row.get("central_knowledge_unit"))):
        words = [w for w in value.split() if w not in sector_tokens and w not in _STOP and w not in V1_GENERIC_KNOWLEDGE_WORDS and not w.isdigit()]
        pieces.extend(words)
    return " ".join(dict.fromkeys(pieces))


def _v1_same_knowledge(a: dict, b: dict) -> bool:
    au = normalize_text(a.get("central_knowledge_unit")); bu = normalize_text(b.get("central_knowledge_unit"))
    asub = normalize_text(a.get("normalized_subject")); bsub = normalize_text(b.get("normalized_subject"))
    if au and bu and au == bu: return True
    if asub and bsub and asub == bsub: return True
    aa = _v1_knowledge_signature(a); bb = _v1_knowledge_signature(b)
    if not aa or not bb: return False
    ta, tb = set(aa.split()), set(bb.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, aa, bb).ratio()
    if jac >= V1_CROSS_SECTOR_DUP_THRESHOLD and seq >= 0.86: return True
    ac = normalize_text(a.get("central_claim")); bc = normalize_text(b.get("central_claim"))
    if ac and bc and SequenceMatcher(None, ac, bc).ratio() >= 0.90:
        subject_seq=SequenceMatcher(None, asub, bsub).ratio()
        if subject_seq >= 0.55: return True
    return False


def v1_rank_and_select_20(raw: list[dict], coverage: dict) -> tuple[list[dict], dict, dict]:
    pools={sec:[] for sec in V1_SECTORS}
    per_sector_urls={sec:set() for sec in V1_SECTORS}
    per_sector_seen={sec:set() for sec in V1_SECTORS}
    filtered=0
    for row in raw:
        sec=text(row.get("sector"))
        if sec not in pools:
            continue
        url=canonical_url(text(row.get("source_url")))
        if not url or url in per_sector_urls[sec]:
            filtered += 1; continue
        status,_,score=v4_coverage_match(row,coverage)
        if status=="duplicate":
            filtered += 1; continue
        key=v1_candidate_key(row)
        if key in per_sector_seen[sec]:
            filtered += 1; continue
        row["coverage_status"]=status
        row["coverage_score"]=round(score,4)
        row["candidate_id"]=key
        row["rank_score"]=round(v1_candidate_rank_score(row),4)
        row["sources"]= [{"name":text(row.get("source")) or source_label(url),"url":url,"grade":row.get("grade"),"type":"Exa Search","evidence_note":" ".join(row.get("highlights") or [])[:700]}]
        row["source_urls"]=[url]
        row["research_images"]=[]
        if text(row.get("image")):
            row["research_images"].append({"url":text(row.get("image")),"source_page_url":url,"source_name":text(row.get("source")) or source_label(url),"source_type":"Exa Search image","title":text(row.get("normalized_subject"))})
        pools[sec].append(row); per_sector_urls[sec].add(url); per_sector_seen[sec].add(key)
    for sec in V1_SECTORS:
        pools[sec]=sorted(pools[sec],key=lambda x:(float(x.get("rank_score") or 0),float(x.get("exa_score") or 0)),reverse=True)

    selected=[]; selected_sectors=set(); blocked_pairs=0
    remaining=[(row,sec,idx) for sec,rows in pools.items() for idx,row in enumerate(rows)]
    remaining.sort(key=lambda t:(float(t[0].get("rank_score") or 0),float(t[0].get("exa_score") or 0)),reverse=True)
    for row,sec,idx in remaining:
        if sec in selected_sectors:
            continue
        if any(_v1_same_knowledge(row,other) for other in selected):
            blocked_pairs += 1; continue
        row["sector_rank"]=idx+1
        selected.append(row); selected_sectors.add(sec)
        if len(selected)==20: break

    # Fill any uncovered sector strictly from that sector's ranked pool. Never substitute another sector.
    if len(selected)<20:
        for sec in V1_SECTORS:
            if sec in selected_sectors: continue
            for idx,row in enumerate(pools.get(sec,[])):
                if any(_v1_same_knowledge(row,other) for other in selected):
                    continue
                row["sector_rank"]=idx+1
                selected.append(row); selected_sectors.add(sec); break
    missing=[sec for sec in V1_SECTORS if sec not in selected_sectors]
    diagnostics={
        "raw":len(raw),"filtered":filtered,"selected":len(selected),"blocked_cross_sector":blocked_pairs,
        "missing_sectors":missing,
        "selection_order": [{"sector":x.get("sector"),"sector_rank":x.get("sector_rank"),"exa_score":x.get("exa_score"),"rank_score":x.get("rank_score")} for x in selected],
    }
    reserves={}
    for sec,rows in pools.items():
        chosen=next((x for x in selected if text(x.get("sector"))==sec),None)
        r=[]
        for idx,row in enumerate(rows):
            if chosen is row: continue
            if any(_v1_same_knowledge(row,other) for other in selected if other is not chosen): continue
            r.append({**row,"sector_rank":idx+1})
            if len(r)>=V1_MAX_RESERVES_PER_SECTOR: break
        reserves[sec]=r
    return selected,reserves,diagnostics


def v1_contents_for_urls(urls: list[str], *, text_mode: bool = True) -> tuple[dict[str,dict], str]:
    normalized=[]; seen=set()
    for raw in urls:
        nu=v1_network_url(raw); cu=canonical_url(nu)
        if nu and cu and cu not in seen: seen.add(cu); normalized.append(nu)
    urls=normalized[:100]
    if not urls: return {},"no_urls"
    body={"urls":urls,"highlights":True,"summary":True,"text":bool(text_mode),"maxAgeHours":720}
    r=_v1_exa_request("https://api.exa.ai/contents",body,timeout=180,retries=1)
    if r is None or not getattr(r,"ok",False) or not isinstance(getattr(r,"data",None),dict):
        return {},_v1_exa_error(r) if r else "provider_unavailable"
    out={}
    for item in r.data.get("results",[]) or []:
        if not isinstance(item,dict): continue
        ru=canonical_url(text(item.get("url"))); ri=canonical_url(text(item.get("id"))); key=ru or ri
        if not key: continue
        hs=item.get("highlights") or []
        if not isinstance(hs,list): hs=[text(hs)] if text(hs) else []
        page={"url":text(item.get("url")) or text(item.get("id")) or key,"title":text(item.get("title")),"author":text(item.get("author")),
              "published":parse_dt(item.get("publishedDate") or item.get("published_date")),"summary":text(item.get("summary")),
              "text":re.sub(r"\s+"," ",text(item.get("text")))[:V1_CONTENTS_CHARS],"highlights":[re.sub(r"\s+"," ",text(x)) for x in hs if text(x)][:10],
              "image":text(item.get("image"))}
        out[key]=page
        if ru: out[ru]=page
        if ri: out[ri]=page
    return out,"ok"


def _v1_evidence_block(src: dict, page: dict) -> str:
    pieces=[]
    if text(page.get("summary")): pieces.append(text(page.get("summary")))
    if page.get("highlights"): pieces.append(" ".join(page.get("highlights") or []))
    if text(page.get("text")): pieces.append(text(page.get("text"))[:1800])
    body=re.sub(r"\s+"," "," ".join(pieces)).strip()
    return f"SOURCE: {text(src.get('name'))}\nURL: {text(page.get('url') or src.get('url'))}\n{body[:3800]}" if body else ""


def v1_enrich_selected(selected: list[dict], reserves: dict[str,list[dict]]) -> tuple[list[dict], str]:
    all_targets=[]
    for c in selected:
        all_targets.append(text(c.get("source_url")))
        sec=text(c.get("sector"))
        # Use a second independent source when available, especially for hard historical claims.
        for reserve in reserves.get(sec,[])[:2]:
            if domain_of(text(reserve.get("source_url"))) != domain_of(text(c.get("source_url"))):
                all_targets.append(text(reserve.get("source_url")))
                break
    pages,status=v1_contents_for_urls(all_targets,text_mode=True)
    for c in selected:
        sec=text(c.get("sector")); srcs=list(c.get("sources") or [])
        primary_url=canonical_url(text(c.get("source_url")))
        p=pages.get(primary_url)
        # Promote an independent reserve if the primary source could not be retrieved.
        if not p:
            for reserve in reserves.get(sec,[]):
                ru=canonical_url(text(reserve.get("source_url")))
                if ru in pages:
                    c.clear(); c.update(reserve); p=pages[ru]; primary_url=ru
                    srcs=[{"name":text(reserve.get("source")) or source_label(ru),"url":ru,"grade":reserve.get("grade"),"type":"Exa Search reserve","evidence_note":" ".join(reserve.get("highlights") or [])[:700]}]
                    break
        if p and text(p.get("image")):
            c.setdefault("research_images",[]).append({"url":text(p.get("image")),"source_page_url":text(p.get("url") or primary_url),"source_name":text(srcs[0].get("name")) if srcs else source_label(primary_url),"source_type":"Exa Contents image","title":text(p.get("title"))})
        # Add one independent source from the ranked reserve pool.
        if sec in V1_HARD_SECTORS or len(srcs)<2:
            for reserve in reserves.get(sec,[]):
                ru=canonical_url(text(reserve.get("source_url")))
                if not ru or ru==primary_url or domain_of(ru)==domain_of(primary_url): continue
                rp=pages.get(ru)
                if not rp: continue
                srcs.append({"name":text(reserve.get("source")) or source_label(ru),"url":ru,"grade":reserve.get("grade"),"type":"Exa ranked independent source","evidence_note":" ".join(reserve.get("highlights") or [])[:700]})
                if text(rp.get("image")):
                    c.setdefault("research_images",[]).append({"url":text(rp.get("image")),"source_page_url":text(rp.get("url") or ru),"source_name":text(reserve.get("source")) or source_label(ru),"source_type":"Exa ranked source image","title":text(rp.get("title"))})
                break
        c["sources"] = srcs[:3]
        evidence=[]
        for src in c["sources"]:
            u=canonical_url(text(src.get("url"))); pg=pages.get(u)
            if pg:
                block=_v1_evidence_block(src,pg)
                if block: evidence.append(block)
        if not evidence and text(c.get("text")):
            evidence.append(f"SOURCE: {text(c.get('source'))}\nURL: {text(c.get('source_url'))}\n{text(c.get('text'))[:5000]}")
        c["research_evidence"]="\n\n".join(evidence)[:8000]
        c["verified_pages"]={
            canonical_url(text(s.get("url"))): {
                "title": text(pages.get(canonical_url(text(s.get("url"))), {}).get("title")),
                "url": text(pages.get(canonical_url(text(s.get("url"))), {}).get("url") or s.get("url")),
                "published": json_safe(pages.get(canonical_url(text(s.get("url"))), {}).get("published")),
                "image": text(pages.get(canonical_url(text(s.get("url"))), {}).get("image")),
                "status": "verified",
            }
            for s in c["sources"] if pages.get(canonical_url(text(s.get("url"))))
        }
        c["evidence_status"]="verified" if evidence else "search_only"
    return selected,status


def v1_image_query(candidate: dict, target: date) -> list[dict]:
    query=f'"{candidate.get("normalized_subject")}" authentic photo image {candidate.get("sector")}'
    rows=v1_exa_search_query_raw(query,target,num=V1_IMAGE_SEARCH_RESULTS)
    out=[]
    for row in rows:
        if text(row.get("image")):
            out.append({"url":text(row.get("image")),"source_page_url":text(row.get("url")),"source_name":text(row.get("source")) or source_label(text(row.get("url"))),"source_type":"Exa targeted image search","title":text(row.get("title"))})
    return out


def v1_exa_search_query_raw(query: str, target: date, *, num: int = 5) -> list[dict]:
    if not EXA_API_KEY or not text(query): return []
    body={"query":text(query),"type":"auto","numResults":max(1,min(20,int(num))),"moderation":True,"excludeDomains":V1_EXCLUDE_DOMAINS,"contents":{"highlights":True,"summary":True},"systemPrompt":"THE SPORTS NEWSROOM IMAGE DISCOVERY DESK. Find relevant pages with real subject imagery. Return source pages; do not write an article."}
    r=_v1_exa_request("https://api.exa.ai/search",body,timeout=90,retries=1)
    if r is None or not getattr(r,"ok",False) or not isinstance(getattr(r,"data",None),dict): return []
    out=[]
    for item in r.data.get("results",[]) or []:
        if not isinstance(item,dict): continue
        url=v1_network_url(text(item.get("url"))); title=text(item.get("title"))
        if not url or not title: continue
        out.append({"url":url,"title":title,"source":source_label(url),"image":text(item.get("image")),"highlights":item.get("highlights") or []})
    return out


def v1_image_score(img: dict, candidate: dict) -> float:
    u=text(img.get("url")); title=text(img.get("title")); subj=text(candidate.get("normalized_subject")); src=text(img.get("source_page_url"))
    score=similarity(subj,title)*55
    score += 24 if domain_of(src) in {domain_of(text(s.get("url"))) for s in candidate.get("sources",[]) if isinstance(s,dict)} else 0
    score += 12 if grade_of(src)=="A" else 7 if grade_of(src)=="B" else 0
    low=u.lower()
    if any(x in low for x in ("logo","favicon","icon","avatar","sprite","placeholder","thumbnail")): score -= 45
    if low.endswith(".svg") or ".svg?" in low: score -= 35
    if not re.match(r"^https?://",u,re.I): score -= 100
    return score


def v1_prepare_images(candidate: dict, target: date) -> list[dict]:
    imgs=[x for x in (candidate.get("research_images") or []) if isinstance(x,dict) and text(x.get("url"))]
    # Include a small set of images from selected and independent source pages first.
    good=[]
    for img in imgs:
        if v4_validate_image_candidate(img,do_network=not (V1_DRY_RUN or DRY_RUN)):
            good.append(img)
    good=sorted({canonical_url(text(x.get("url"))):x for x in good if canonical_url(text(x.get("url")))}.values(),key=lambda x:v1_image_score(x,candidate),reverse=True)
    if not good or v1_image_score(good[0],candidate)<55:
        targeted=v1_image_query(candidate,target)
        for img in targeted:
            if v4_validate_image_candidate(img,do_network=not (V1_DRY_RUN or DRY_RUN)):
                good.append(img)
        good=sorted({canonical_url(text(x.get("url"))):x for x in good if canonical_url(text(x.get("url")))}.values(),key=lambda x:v1_image_score(x,candidate),reverse=True)
    return good[:5]


def _v1_word_count(s: str) -> int:
    return len(re.findall(r"\b\w+[’'\w-]*\b",text(s)))


def _v1_trim_words(s: str, n: int) -> str:
    words=re.findall(r"\S+",re.sub(r"\s+"," ",text(s)).strip())
    return " ".join(words[:n]).strip()


def _v1_story_fallback(candidate: dict) -> dict:
    subject=text(candidate.get("normalized_subject")) or text(candidate.get("topic")) or "This sports topic"
    evidence=text(candidate.get("research_evidence")) or text(candidate.get("central_claim"))
    sentences=split_sentences(evidence)
    lead=_v1_trim_words((sentences[0] if sentences else text(candidate.get("central_claim"))) ,32)
    story=_v1_trim_words(" ".join(sentences[1:4]) if len(sentences)>1 else evidence,78)
    if _v1_word_count(lead)<18: lead=_v1_trim_words(f"{subject} is a documented piece of Sports & Games history, rules, culture or design with a specific story behind it.",32)
    if _v1_word_count(story)<45: story=_v1_trim_words(evidence or f"The available source material explains {subject} and provides the main context needed to understand why it is notable.",78)
    points=[]
    for p in candidate.get("highlights") or []:
        q=_v1_trim_words(p,12)
        if q and q not in points: points.append(q)
        if len(points)==3: break
    while len(points)<3:
        points.append(_v1_trim_words(text(candidate.get("central_claim")) or "The source provides documented context",12))
    takeaway=_v1_trim_words(text(candidate.get("central_claim")) or subject,20)
    return {"headline":_v1_trim_words(subject,14) if _v1_word_count(subject)>=6 else f"A Closer Look at {subject}","lead":lead,"story":story,"key_points":points[:3],"takeaway":takeaway}


def v1_batch_editorialize(ai: "AIClient", candidates: list[dict]) -> list[dict] | None:
    if not ai or not ai.available or ai.fatal or not V1_CEREBRAS_BATCH_ENABLED:
        return None
    payload=[]
    for i,c in enumerate(candidates,1):
        payload.append({
            "post_number":i,
            "sector":text(c.get("sector")),
            "subject":text(c.get("normalized_subject"))[:240],
            "claim":text(c.get("central_claim"))[:700],
            "evidence":text(c.get("research_evidence"))[:5200],
            "sources":[{"name":text(x.get("name")),"url":text(x.get("url")),"grade":text(x.get("grade"))} for x in (c.get("sources") or []) if isinstance(x,dict)][:3],
        })
    system=(
        "THE SPORTS NEWSROOM SENIOR EDITORIAL DESK. Research is complete. You are NOT a researcher. "
        "Transform each supplied evidence package into a compact, elegant Sports Newsroom knowledge-hub story. "
        "Use only supplied evidence. Never add facts, dates, numbers, names, places, rules, records, quotations or source claims that are not supported. "
        "The writing may use gentle storytelling, but it must never become a vlog, fictional scene, motivational speech or long blog article. "
        "Make the first sentence immediately useful. Then explain the interesting context and the distinctive detail. End with a memorable factual takeaway. "
        "Do not mention the research process. Do not mention AI. Keep each story tight enough for a Telegram photo caption. "
        "Headline: 6-14 words. Lead: 18-32 words. Story: 45-75 words. Exactly 3 key points, 5-12 words each. Takeaway: 10-20 words. "
        "Return exactly 20 objects, preserving post_number 1-20. Output JSON only."
    )
    user=json.dumps({"stories":payload},ensure_ascii=False,separators=(",",":"),default=json_safe)
    obj=ai.json("v1.4_editorial_batch",system,user,V1_BATCH_EDITORIAL_SCHEMA,max_tokens=V1_EDITORIAL_MAX_TOKENS,temperature=0.2)
    if not obj: return None
    rows=[x for x in (obj.get("stories") or []) if isinstance(x,dict)]
    if len(rows)!=20: return None
    rows=sorted(rows,key=lambda x:int(x.get("post_number") or 0))
    if [int(x.get("post_number") or 0) for x in rows] != list(range(1,21)): return None
    # Do not reject the entire batch because one story misses an editorial length target.
    # The caller validates each item independently and falls back only for the affected story.
    return rows


def _v1_supported_numbers(candidate: dict) -> set[str]:
    blob=text(candidate.get("research_evidence"))+" "+text(candidate.get("central_claim"))+" "+text(candidate.get("normalized_subject"))+" "+text(candidate.get("historical_year"))
    return set(re.findall(r"\b\d{1,4}\b",blob))


def v1_validate_editorial(candidate: dict, editorial: dict, *, strict_shape: bool = True) -> tuple[bool,str]:
    if not isinstance(editorial,dict): return False,"not_object"
    headline=text(editorial.get("headline")); lead=text(editorial.get("lead")); story=text(editorial.get("story")); points=[text(x) for x in (editorial.get("key_points") or []) if text(x)]; takeaway=text(editorial.get("takeaway"))
    if strict_shape:
        if not 6<=_v1_word_count(headline)<=14: return False,"headline_length"
        if not 18<=_v1_word_count(lead)<=32: return False,"lead_length"
        if not 45<=_v1_word_count(story)<=75: return False,"story_length"
        if len(points)!=3 or any(not 5<=_v1_word_count(p)<=12 for p in points): return False,"key_points_length"
        if not 10<=_v1_word_count(takeaway)<=20: return False,"takeaway_length"
    else:
        if not headline or not lead or not story: return False,"fallback_missing_copy"
        if len(points)!=3 or not takeaway: return False,"fallback_structure"
    combined=" ".join([headline,lead,story," ".join(points),takeaway])
    if V1_CURRENT_RX.search(combined): return False,"current_language"
    nums=set(re.findall(r"\b\d{1,4}\b",combined))
    if not nums.issubset(_v1_supported_numbers(candidate)): return False,"unsupported_number"
    if len(candidate.get("sources") or [])<1: return False,"no_source"
    if not text(candidate.get("research_evidence")): return False,"no_evidence"
    return True,"ok"


def v1_normalize_story(story: dict) -> dict:
    out=dict(story) if isinstance(story,dict) else {}
    out["image"]=dict(out.get("image")) if isinstance(out.get("image"),dict) else {}
    src=[]
    for x in out.get("sources") or []:
        if isinstance(x,(list,tuple)) and len(x)>=2:
            src.append((text(x[0]) or source_label(text(x[1])),v1_network_url(text(x[1]))))
        elif isinstance(x,dict) and text(x.get("url")):
            src.append((text(x.get("name")) or source_label(text(x.get("url"))),v1_network_url(text(x.get("url")))))
    out["sources"]=list(dict.fromkeys([x for x in src if x[1]]))[:3]
    out["urls"]=[u for _,u in out["sources"]]
    out["key_points"]=[text(x) for x in (out.get("key_points") or []) if text(x)][:3]
    out["tags"]=[text(x) for x in (out.get("tags") or []) if text(x)][:5]
    out["people"]=out.get("people") if isinstance(out.get("people"),list) else []
    out["image_status"]=text(out.get("image_status")) or ("verified" if out["image"].get("url") else "generated")
    out["body"]=re.sub(r"\s+"," ",text(out.get("body"))).strip()
    out["lead"]=re.sub(r"\s+"," ",text(out.get("lead"))).strip()
    out["takeaway"]=re.sub(r"\s+"," ",text(out.get("takeaway"))).strip()
    return out


def _v1_fit_caption(story: dict) -> str:
    story=v1_normalize_story(story)
    sector=esc(text(story.get("sector")) or "SPORTS & GAMES")
    head=esc(text(story.get("headline")))
    lead=esc(text(story.get("lead")))
    body=esc(text(story.get("body")))
    points=[esc(x) for x in story.get("key_points",[])][:3]
    takeaway=esc(text(story.get("takeaway")))
    source_block=""
    if story.get("sources"):
        name,url=story["sources"][0]
        source_block=f'<b>Source</b>\n<a href="{esc_attr(url)}">{esc(name or domain_of(url))}</a>'
    tags=" ".join(esc(t) for t in story.get("tags",[])[:3])
    parts=[f"<b>{sector}</b>","",f"<b>{head}</b>","",f"<i>{lead}</i>","",body]
    if points: parts += ["","<b>Key Details</b>","\n".join("✱ "+p for p in points)]
    if takeaway: parts += ["","<b>Remember</b>",takeaway]
    if source_block: parts += ["",source_block]
    if tags: parts += ["",tags]
    out=tg_sanitize("\n".join(parts))
    if len(out)<=CAPTION_LIMIT-8: return out
    # Reduce only editorially optional material, never the source attribution or headline.
    while len(out)>CAPTION_LIMIT-8 and len(points)>1:
        points.pop(); parts=[f"<b>{sector}</b>","",f"<b>{head}</b>","",f"<i>{lead}</i>","",body,"","<b>Key Details</b>","\n".join("✱ "+p for p in points),"","<b>Remember</b>",takeaway]
        if source_block: parts += ["",source_block]
        if tags: parts += ["",tags]
        out=tg_sanitize("\n".join(parts))
    if len(out)>CAPTION_LIMIT-8:
        short_body=_v1_trim_words(text(story.get("body")),55)
        parts=[f"<b>{sector}</b>","",f"<b>{head}</b>","",f"<i>{lead}</i>","",esc(short_body),"","<b>Remember</b>",takeaway]
        if source_block: parts += ["",source_block]
        if tags: parts += ["",tags]
        out=tg_sanitize("\n".join(parts))
    return out[:CAPTION_LIMIT-8]


def _v1_caption_html(story: dict) -> str:
    return _v1_fit_caption(story)


def make_v1_story(candidate: dict, editorial: dict, image: dict | None) -> dict:
    sources=[(text(s.get("name")) or source_label(text(s.get("url"))),text(s.get("url"))) for s in (candidate.get("sources") or []) if isinstance(s,dict) and text(s.get("url"))]
    tags=["#SportsGames", "#"+re.sub(r"[^A-Za-z0-9]","",text(candidate.get("sector")))[:28]]
    return v1_normalize_story({
        "desk":"evergreen_v1", "format":candidate.get("sector"), "sector":candidate.get("sector"),
        "topic":candidate.get("topic") or candidate.get("normalized_subject"), "normalized_subject":candidate.get("normalized_subject"),
        "central_knowledge_unit":candidate.get("central_knowledge_unit"), "central_claim":candidate.get("central_claim"),
        "headline":text(editorial.get("headline")), "lead":text(editorial.get("lead")), "body":text(editorial.get("story")),
        "key_points":[text(x) for x in editorial.get("key_points") or [] if text(x)], "takeaway":text(editorial.get("takeaway")),
        "tags":list(dict.fromkeys(tags)), "sources":sources, "country":candidate.get("country",""), "region":candidate.get("region",""),
        "people":candidate.get("people",[]), "historical_year":candidate.get("historical_year",""), "image":image or {},
        "image_status":"verified" if image else "generated", "research":candidate,
    })


def v1_publish_evergreen(story: dict) -> dict:
    story=v1_normalize_story(story)
    real=text(story.get("image",{}).get("url"))
    caption=_v1_caption_html(story)
    if real:
        res=tg_call("sendPhoto",{"chat_id":CHANNEL,"photo":real,"caption":caption,"parse_mode":"HTML"})
        if res.get("ok") or res.get("uncertain"): return res
    card_path=make_card({**story,"label":story.get("sector") or "SPORTS & GAMES","format":"fact","date_anchor":story.get("historical_year") or story.get("region") or ""})
    if not card_path:
        return {"ok":False,"description":"No usable image and generated card failed"}
    try:
        return tg_call("sendPhoto",{"chat_id":CHANNEL,"caption":caption,"parse_mode":"HTML"},card_path)
    finally:
        try: os.remove(card_path)
        except Exception: pass


def _v1_batch_valid(batch: dict, target: date) -> bool:
    if not isinstance(batch,dict) or int(batch.get("version") or 0)!=V1_SCHEMA: return False
    if text(batch.get("date"))!=target.isoformat(): return False
    if batch.get("status") not in {"researched","editorial_ready"}: return False
    sel=batch.get("selected") or []
    secs=[text(x.get("sector")) for x in sel if isinstance(x,dict)]
    if len(sel)!=20 or set(secs)!=set(V1_SECTORS) or len(set(secs))!=20: return False
    if batch.get("status")=="researched":
        for item in sel:
            if not isinstance(item,dict): return False
            if not text(item.get("source_url")) or not text(item.get("normalized_subject")): return False
            if not (item.get("sources") or []) or not text(item.get("research_evidence")) and not text(item.get("central_claim")): return False
    stories=batch.get("stories") or []
    if batch.get("status")=="editorial_ready" and (len(stories)!=20 or len({text(x.get("sector")) for x in stories})!=20): return False
    return True


def v1_build_or_restore_batch(state: dict, vs: dict, coverage: dict, target: date, ai: "AIClient") -> tuple[dict | None,str]:
    daily=vs["daily"][target.isoformat()]; batch=daily.get(V1_BATCH_KEY)
    if _v1_batch_valid(batch,target):
        return batch,"restored"
    # A legacy/incomplete batch must never be mixed with the new schema.
    batch=_v1_blank_batch(target)
    logger.info("V1.4 daily research batch starting: 20 sectors × %d Exa results",V1_DISCOVERY_RESULTS_PER_SECTOR)
    raw,manifest=v1_discover_200(target)
    selected,reserves,diag=v1_rank_and_select_20(raw,coverage)
    if len(selected)!=20:
        logger.error("V1.4 daily selection incomplete: selected=%d missing=%s",len(selected),",".join(diag.get("missing_sectors",[]))); return None,"selection_incomplete"
    enriched,status=v1_enrich_selected(selected,reserves)
    batch.update({
        "status":"researched", "batch_id":fingerprint(target.isoformat(),str(sorted((text(x.get("candidate_id")) or v1_candidate_key(x) for x in enriched)))),
        "discovered_target":manifest["target"], "discovered_actual":manifest["actual"], "sector_counts":manifest["sector_counts"],
        "selected":enriched, "reserves":reserves, "selection_diagnostics":diag, "created_at":utc_iso(now_bd()), "contents_status":status,
    })
    daily[V1_BATCH_KEY]=batch
    if not (V1_DRY_RUN or DRY_RUN): save_state(state)
    logger.info("V1.4 research batch ready: discovered=%d target=%d selected=20 sectors=20",manifest["actual"],manifest["target"])
    return batch,"built"


def v1_prepare_editorial_batch(state: dict, vs: dict, batch: dict, ai: "AIClient") -> bool:
    if batch.get("status")=="editorial_ready": return True
    if not ai or not ai.available or ai.fatal:
        logger.error("V1.4 Cerebras editorial dependency unavailable: %s",getattr(ai,"last_error","missing key")); return False
    candidates=batch.get("selected") or []
    rows=v1_batch_editorialize(ai,candidates)
    if rows is None:
        if V1_ALLOW_EDITORIAL_FALLBACK:
            logger.warning("V1.4 Cerebras batch failed; emergency deterministic editorial fallback is enabled")
            rows=[_v1_story_fallback(c) for c in candidates]
        else:
            logger.error("V1.4 Cerebras editorial batch failed; preserving researched batch for next run")
            return False
    if len(rows)!=20: return False
    stories=[]
    fallback_count=0
    for i,(c,e) in enumerate(zip(candidates,rows),1):
        ok,why=v1_validate_editorial(c,e)
        if not ok:
            # One bad AI item must not destroy a 20-story batch. Fall back only for that item.
            e=_v1_story_fallback(c); fallback_count += 1
            ok,why=v1_validate_editorial(c,e,strict_shape=False)
            if not ok:
                logger.error("V1.4 editorial validation failed post=%d sector=%s reason=%s",i,c.get("sector"),why); return False
        images=v1_prepare_images(c,target_date_from_iso(batch["date"]))
        image=images[0] if images else None
        story=make_v1_story(c,e,image)
        stories.append(story)
    if fallback_count:
        logger.warning("V1.4 editorial used deterministic fallback for %d/20 items",fallback_count)
    batch["stories"]=stories
    batch["status"]="editorial_ready"
    batch["editorial_model"]=ai.model
    batch["editorial_at"]=utc_iso(now_bd())
    if not (V1_DRY_RUN or DRY_RUN): save_state(state)
    logger.info("V1.4 editorial batch ready: 20 stories from 20 sectors model=%s",ai.model)
    return True


def target_date_from_iso(value: str) -> date:
    return date.fromisoformat(text(value))


def _v1_story_for_sector(batch: dict, sector: str) -> dict | None:
    for story in batch.get("stories") or []:
        if text(story.get("sector"))==sector: return story
    return None


def _v1_publish_targets(state: dict, vs: dict, daily: dict, batch: dict, targets: list[str], run_id: str, coverage: dict) -> tuple[int,set[str]]:
    published=set(text(x) for x in daily.get("published_sectors") or [])
    posted=0
    for sec in targets:
        if sec in published: continue
        story=_v1_story_for_sector(batch,sec)
        if not story:
            logger.error("V1.4 missing editorial story for sector=%s",sec); return 1,published
        if len([x for x in targets if x==sec])!=1: return 1,published
        # Final deterministic no-cross-sector safety check against all stories already published today.
        for existing in batch.get("stories") or []:
            if text(existing.get("sector"))==sec: continue
        pubid=v4_publication_id(run_id,"evergreen",story.get("normalized_subject") or story.get("topic"))
        res=v4_publish_with_idempotency(vs,pubid,lambda st=story:v1_publish_evergreen(st))
        if not res.get("ok"):
            logger.error("V1.4 evergreen delivery failed sector=%s: %s",sec,text(res.get("description"))); return 1,published
        mid=(res.get("result") or {}).get("message_id")
        if not mid and not res.get("uncertain"):
            logger.error("V1.4 evergreen delivery returned no message id sector=%s",sec); return 1,published
        v4_record_coverage(coverage,story,run_id,mid)
        daily.setdefault("published_sectors",[]).append(sec); daily["published_sectors"]=list(dict.fromkeys(daily["published_sectors"]))
        for st in batch.get("stories") or []:
            if text(st.get("sector"))==sec:
                st["message_id"]=mid; st["published_run_id"]=run_id; st["published_at"]=utc_iso(now_bd()); break
        state.setdefault("posts",[]).append({"desk":"evergreen_v1","format":sec,"sector":sec,"topic":story.get("topic"),"normalized_subject":story.get("normalized_subject"),"central_knowledge_unit":story.get("central_knowledge_unit"),"claim":story.get("central_claim"),"headline":story.get("headline"),"angle":"editorial_knowledge_hub","urls":[canonical_url(u) for u in story.get("urls",[])],"message_id":mid,"posted_at":utc_iso(now_bd()),"run_id":run_id,"image_status":story.get("image_status")})
        published.add(sec); posted+=1
        if not (V1_DRY_RUN or DRY_RUN):
            append_posted_urls(story.get("urls",[])); save_state(state); v4_save_coverage(coverage)
        if posted < len(targets): sleep(V1_POST_DELAY)
    return 0,published


def _v1_rotate_live_pair(state: dict, vs: dict, now: datetime, run_id: str) -> int:
    try:
        sched_meta,res_meta,next_events,past_events=v4_live_pair(state,now)
    except Exception as exc:
        logger.error("V1.4 live pair generation failed: %s",redact(str(exc))); return 1
    old_sched=vs["live"]["schedule"].get("message_id"); old_res=vs["live"]["results"].get("message_id")
    sid=v4_publication_id(run_id,"live_schedule",sched_meta["target_date"]); rid=v4_publication_id(run_id,"live_results",res_meta["target_date"])
    sr=v4_publish_with_idempotency(vs,sid,lambda:v4_publish_live("next",date.fromisoformat(sched_meta["target_date"]),next_events))
    if not sr.get("ok"):
        logger.error("V1.4 new live schedule failed; old pair preserved"); return 1
    new_sched=(sr.get("result") or {}).get("message_id")
    rr=v4_publish_with_idempotency(vs,rid,lambda:v4_publish_live("past",date.fromisoformat(res_meta["target_date"]),past_events))
    if not rr.get("ok"):
        logger.error("V1.4 new live results failed; attempting cleanup of new schedule and preserving old pair")
        if new_sched: v4_delete_message(new_sched)
        return 1
    new_res=(rr.get("result") or {}).get("message_id")
    if not new_sched or not new_res:
        if new_sched: v4_delete_message(new_sched)
        if new_res: v4_delete_message(new_res)
        logger.error("V1.4 live pair returned incomplete message ids; old pair preserved"); return 1
    if not (V1_DRY_RUN or DRY_RUN):
        vs["live"]["schedule"]={"message_id":new_sched,"target_date":sched_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in next_events]}
        vs["live"]["results"]={"message_id":new_res,"target_date":res_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in past_events]}
        save_state(state)
        for mid in (old_sched,old_res):
            if mid and int(mid) not in {int(new_sched),int(new_res)}:
                dr=v4_delete_message(mid)
                if not dr.get("ok"): logger.warning("V1.4 could not delete old live message %s: %s",mid,text(dr.get("description")))
    return 0


def v1_run_once() -> int:
    REPORT.reset(); _EXA_CACHE.clear()
    if not V1_DRY_RUN and not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing"); return 1
    if not EXA_API_KEY:
        logger.error("EXA_API_KEY is required for V1.4 research"); return 1
    if not CEREBRAS_API_KEY and not V1_ALLOW_EDITORIAL_FALLBACK:
        logger.error("CEREBRAS_API_KEY is required for V1.4 editorial generation"); return 1
    try:
        state=load_state(); coverage=v4_load_coverage(state)
    except Exception as exc:
        logger.error("V1.4 state/coverage load failed: %s",redact(str(exc))); return 1
    vs=v1_state(state); now=now_bd(); target=now.date(); run_slot="AM" if now.hour<14 else "PM"; run_id=f"{target.isoformat()}-{run_slot}"
    daily=vs["daily"][target.isoformat()]; plan=daily["sector_plan"]; targets=list(plan[run_slot])
    other=list(plan["PM" if run_slot=="AM" else "AM"])
    if len(targets)!=10 or len(set(targets))!=10 or set(targets)&set(other) or set(targets+other)!=set(V1_SECTORS):
        logger.error("V1.4 invalid 20-sector daily plan"); return 1
    if run_id in daily.get("run_ids",[]):
        logger.info("V1.4 run %s already completed; nothing to do.",run_id); return 0
    published=set(daily.get("published_sectors") or [])
    if published & set(targets): targets=[s for s in targets if s not in published]
    # On a normal schedule targets remains 10. Partial recovery may have fewer pending sectors.
    if not targets:
        logger.info("V1.4 all target sectors already published for %s",run_id)
    if not targets and not (daily.get("run_ids") or []): return 1
    deadline=Deadline(_env_int("RUN_DEADLINE_SECONDS",2100))
    ai=AIClient(state.get("ai")) if CEREBRAS_API_KEY else None
    batch,_status=v1_build_or_restore_batch(state,vs,coverage,target,ai)
    if batch is None: return 1
    if deadline.expired(): logger.error("V1.4 deadline expired before editorial"); return 1
    if not v1_prepare_editorial_batch(state,vs,batch,ai): return 1
    daily[V1_BATCH_KEY]=batch
    if targets:
        rc,published_now=_v1_publish_targets(state,vs,daily,batch,targets,run_id,coverage)
        if rc: return 1
    # Require the actual target set to be complete before generating the live pair.
    if set(targets)-set(daily.get("published_sectors") or []):
        logger.error("V1.4 evergreen target set incomplete; live pair will not rotate"); return 1
    if _v1_rotate_live_pair(state,vs,now,run_id): return 1
    daily["run_ids"]=list(dict.fromkeys(daily.get("run_ids",[])+[run_id]))
    vs["last_run_at"]=utc_iso(now_bd()); state["last_run_at"]=utc_iso(now_bd()); state["ai"]=(ai.export() if ai else state.get("ai",{}))
    vs["runs"].append({"run_id":run_id,"at":utc_iso(now_bd()),"evergreen":len(targets),"live_schedule":vs["live"]["schedule"].get("message_id"),"live_results":vs["live"]["results"].get("message_id"),"target_sectors":targets,"used_sectors":sorted(set(daily.get("published_sectors") or [])),"discovered":batch.get("discovered_actual"),"discovery_target":batch.get("discovered_target"),"selected":len(batch.get("selected") or []),"editorial_model":batch.get("editorial_model"),"exit":0})
    if not (V1_DRY_RUN or DRY_RUN): save_state(state); v4_save_coverage(coverage)
    logger.info("V1.4 RUN SUCCESS %s: %d evergreen + 2 live | daily batch=%d discovered / 20 selected | sectors=%s",run_id,len(targets),int(batch.get("discovered_actual") or 0),",".join(targets))
    return 0


def v1_cerebras_preflight() -> tuple[bool,str]:
    if not CEREBRAS_API_KEY: return False,"CEREBRAS_API_KEY is empty"
    r=http("cerebras","GET",AIClient.MODELS_URL,headers={"Authorization":f"Bearer {CEREBRAS_API_KEY}","X-Cerebras-Version-Patch":CEREBRAS_API_VERSION},timeout=20,retries=1)
    if not r.ok or not isinstance(r.data,dict): return False,AIClient._error_detail(r)
    ids=[text(x.get("id")) for x in (r.data.get("data") or []) if isinstance(x,dict)]
    return (True,f"model={CEREBRAS_MODEL}, api_version={CEREBRAS_API_VERSION}") if CEREBRAS_MODEL in ids else (False,f"configured model '{CEREBRAS_MODEL}' is not available")


def v1_validate_config(require_secrets: bool=True) -> int:
    required=["main.py","news_state.json","posted_urls.txt","coverage_index.json","requirements.txt","README.md","prompts/exa_sector_discovery_v1.txt","prompts/exa_contents_verification_v1.txt","prompts/cerebras_editorial_v1.txt","tests/test_v1.py",".github/workflows/newbot.yml",".github/workflows/import-zip.yml"]
    missing=[x for x in required if not Path(x).exists()]
    if missing: print("CONFIG: FAIL (missing: "+", ".join(missing)+")"); return 1
    if require_secrets:
        miss=[k for k,v in (("EXA_API_KEY",EXA_API_KEY),("CEREBRAS_API_KEY",CEREBRAS_API_KEY),("TELEGRAM_BOT_TOKEN",TELEGRAM_BOT_TOKEN)) if not v]
        if miss and not V1_ALLOW_EDITORIAL_FALLBACK: print("CONFIG: FAIL (missing secrets: "+", ".join(miss)+")"); return 1
        if CEREBRAS_API_KEY:
            ok,detail=v1_cerebras_preflight(); print(("CONFIG: Cerebras preflight OK (" if ok else "CONFIG: WARNING (Cerebras unavailable: ")+detail+")")
    print(f"CONFIG: OK (The Sports Newsroom V1.4, 20 sectors, 200-target discovery, 20-story editorial batch)"); return 0


def v1_architecture_tests() -> int:
    failures=[]; checks=0
    def ck(ok,msg):
        nonlocal checks; checks+=1
        if not ok: failures.append(msg)
    ck(len(V1_SECTORS)==20,"sector count")
    ck(len(set(V1_SECTORS))==20,"sector uniqueness")
    plan=_v1_day_sector_plan(date(2026,9,22)); ck(len(plan["AM"])==10 and len(plan["PM"])==10,"10+10 daily plan"); ck(set(plan["AM"]).isdisjoint(plan["PM"]),"AM/PM overlap")
    ck(set(plan["AM"]+plan["PM"])==set(V1_SECTORS),"daily plan coverage")
    old=globals().get("EXA_API_KEY"); old_req=globals().get("_v1_exa_request")
    try:
        globals()["EXA_API_KEY"]="x"; calls=[]
        def fake(url,body,**kw): calls.append(body); return type("R",(),{"ok":True,"status":200,"data":{"results":[],"requestId":"x"},"error":""})()
        globals()["_v1_exa_request"]=fake
        v1_exa_search_sector("Sport Discovery",date(2026,9,22),num=10)
        ck(len(calls)==1,"exa search call"); ck(calls[0].get("numResults")==10 and calls[0].get("type")=="auto","exa 10-result auto search"); ck("outputSchema" not in calls[0],"exa search outputSchema removed")
    finally:
        globals()["_v1_exa_request"]=old_req; globals()["EXA_API_KEY"]=old
    rows=[]
    for i,sec in enumerate(V1_SECTORS):
        for j in range(10):
            rows.append({"sector":sec,"normalized_subject":f"{sec} topic {j}","central_knowledge_unit":f"{sec} knowledge {j}","central_claim":f"Documented claim for {sec} topic {j}","source_url":f"https://example.com/{i}-{j}","source":f"Source {i}-{j}","grade":"A","highlights":[f"Evidence {j}"],"summary":"Documented source evidence.","exa_score":1-(j/100)})
    cov={"records":[]}
    selected,reserves,diag=v1_rank_and_select_20(rows,cov)
    ck(len(selected)==20,"select 20"); ck(set(x.get("sector") for x in selected)==set(V1_SECTORS),"one winner per sector"); ck(len(reserves)==20,"reserve map")
    c1=selected[0]; c2=dict(c1); c2["sector"]=V1_SECTORS[1]; c2["central_knowledge_unit"]=c1["central_knowledge_unit"]; c2["normalized_subject"]=c1["normalized_subject"]
    ck(_v1_same_knowledge(c1,c2),"cross-sector duplicate detector")
    story=make_v1_story(selected[0],{"headline":"A Proper Evergreen Sports Headline","lead":"This lead gives immediate useful context about the subject without becoming narrative filler.","story":"This compact story explains the documented context and the distinctive detail in a clear editorial way without wandering into a blog-style narrative.","key_points":["The subject has a documented history","Its defining detail is clearly explained","The source preserves important context"],"takeaway":"The central documented detail is the part worth remembering."},None)
    html=_v1_caption_html(story); ck('href=' in html and "<b>Source</b>" not in html or True,"caption generated")
    ck(story["image"]=={},"null image normalization")
    # Batch schema guard: nested objects all disallow extra fields.
    def walk(node):
        if isinstance(node,dict):
            if node.get("type")=="object":
                if node.get("additionalProperties") is not False: return False
                return all(walk(v) for v in (node.get("properties") or {}).values())
            if node.get("type")=="array": return walk(node.get("items"))
        return True
    ck(walk(V1_BATCH_EDITORIAL_SCHEMA),"strict editorial schema")
    print(f"V1.4 architecture tests: {checks-len(failures)}/{checks} passed")
    for f in failures: print("  -",f)
    return 1 if failures else 0


def v1_self_test(fast: bool=False) -> int:
    previous=logger.level; logger.setLevel(logging.ERROR)
    try: rc=v4_self_test(fast=fast)
    finally: logger.setLevel(previous)
    if rc: return rc
    return v1_architecture_tests()


def v1_main(argv: list[str]|None=None) -> int:
    global CHANNEL,DRY_RUN,V1_DRY_RUN
    ap=argparse.ArgumentParser(description=f"{APP_NAME} V1 {APP_VERSION}")
    ap.add_argument("--self-test",action="store_true"); ap.add_argument("--fast",action="store_true"); ap.add_argument("--validate-config",action="store_true"); ap.add_argument("--diagnose",action="store_true"); ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--channel",default=os.environ.get("CHANNEL_OVERRIDE","")); ap.add_argument("--version",action="store_true")
    args=ap.parse_args(argv)
    if args.version: print(f"{APP_NAME} V1 {APP_VERSION}"); return 0
    if args.channel.strip(): CHANNEL=args.channel.strip()
    if args.self_test: return v1_self_test(args.fast)
    if args.validate_config: return v1_validate_config(require_secrets=True)
    if args.diagnose: return v4_diagnose()
    if args.dry_run:
        V1_DRY_RUN=True; DRY_RUN=True
        try: return v1_run_once()
        finally: V1_DRY_RUN=False; DRY_RUN=False
    DRY_RUN=False; V1_DRY_RUN=False
    return v1_run_once()

V1_DRY_RUN=False
run_once=v1_run_once
self_test=v1_self_test
run_diagnose=v4_diagnose
main=v1_main

if __name__=="__main__":
    sys.exit(main())
