#!/usr/bin/env python3
# The Sports Newsroom V1 - single Search-first evergreen + live sports newsroom for @TheSportsNewsroom.
#
# Design (see README.md):
#   V1: Exa Search discovers -> local coverage/dedupe -> Cerebras ranks -> Exa Contents verifies ->
#   optional Exa Agent resolves hard cases -> Cerebras edits -> normalization/validation -> Telegram publishes.
#   Live sports remains a separate temporary pair with transactional rotation.
#
# Dependencies: requests, Pillow.  Exa, Cerebras and Telegram are called over plain REST.
# Content scope: real-world sports and physical/tabletop games only.
# There is one production engine. The live-sports adapters are shared data services used by the same engine.

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
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
APP_VERSION = "1.6.0"

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

RUN_DEADLINE_SECONDS = _env_int("RUN_DEADLINE_SECONDS", 1500)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 15)
CEREBRAS_API_VERSION = "2"

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
        except (TypeError, ValueError, OverflowError):
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError, IndexError):
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
    """Bring older state formats forward without losing the post archive."""
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
    # Older posts may use published_at/subject/claim; keep them readable by current dedupe
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
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)












# --- slots -------------------------------------------------------------------------------------












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






def render_evergreen_post(story: dict) -> dict:
    """Build the only evergreen Telegram HTML payload. Order is fixed and intentionally minimal."""
    story=v1_normalize_story(story)
    headline=text(story.get("headline")).strip()
    body=text(story.get("body")).strip()
    if not headline or not body:
        raise ValueError("evergreen renderer requires headline and body")

    source_parts=[]
    seen_urls=set()
    for item in story.get("sources", []):
        if not isinstance(item,(list,tuple)) or len(item)<2:
            continue
        label=text(item[0]).strip()
        url=text(item[1]).strip()
        if not url or url in seen_urls or not re.match(r"^https?://",url,re.I):
            continue
        seen_urls.add(url)
        if not label or re.match(r"^https?://",label,re.I):
            label=domain_of(url)
        source_parts.append(f'<a href="{esc_attr(url)}">{esc(label)}</a>')
        if len(source_parts)>=3:
            break
    if not source_parts:
        raise ValueError("evergreen renderer requires at least one source")

    tags=[]
    seen_tags=set()
    for raw in story.get("tags", []):
        tag=text(raw).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag="#"+re.sub(r"[^A-Za-z0-9]", "", tag)
        if tag == "#" or tag in seen_tags:
            continue
        seen_tags.add(tag)
        tags.append(tag)
        if len(tags)>=3:
            break
    if not tags:
        tags=["#SportsGames"]

    html_text="\n\n".join([
        f"<b>{esc(headline)}</b>",
        esc(body),
        "Source: " + " · ".join(source_parts),
        " ".join(esc(tag) for tag in tags),
    ])
    if vis_len(html_text)>CAPTION_LIMIT:
        raise ValueError("evergreen Telegram HTML exceeds caption limit")

    # The rendered value is intentionally shared as the sendPhoto caption. Keeping parse_mode here
    # makes the publication contract explicit; tg_call enforces it again at the transport boundary.
    return {"html":html_text,"parse_mode":"HTML"}


# --- publisher ---------------------------------------------------------------------------------


def tg_call(method: str, data: dict | None = None, file_path: str = "", file_field: str = "photo") -> dict:
    """Call a Bot API method and enforce HTML parse mode for any anchor-bearing payload."""
    data=data or {}
    for field in ("text", "caption"):
        value=text(data.get(field))
        if re.search(r"<a\s+href\s*=", value, re.I) and text(data.get("parse_mode")).upper() != "HTML":
            raise AssertionError(f"{method}.{field} contains <a href but parse_mode is not HTML")
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
    MAX_CALLS_PER_RUN = max(1, _env_int("CEREBRAS_MAX_CALLS_PER_RUN", 16))
    RATE_LIMIT_PAUSE_SECONDS = max(1.0, _env_float("CEREBRAS_RATE_LIMIT_PAUSE_SECONDS", 5.0))

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
        self._throttle_until = 0.0
        self._model_fallback_tried = False
        self.failures = 0
        self.calls_this_run = 0
        self.rate_limit_streak = 0
        self._call_cap_logged = False

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
        now = time.monotonic()
        wait = max(self.MIN_INTERVAL - (now - self._last_call), self._throttle_until - now)
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
            if self.calls_this_run >= self.MAX_CALLS_PER_RUN:
                self.last_error = f"Cerebras per-run call cap reached ({self.MAX_CALLS_PER_RUN})"
                if not self._call_cap_logged:
                    logger.warning(self.last_error + "; skipping further AI work this run")
                    self._call_cap_logged = True
                return None
            self._space()
            self.calls_this_run += 1
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
                self.rate_limit_streak += 1
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
                adaptive_pause = min(30.0, self.RATE_LIMIT_PAUSE_SECONDS * (2 ** max(0, self.rate_limit_streak - 1)))
                wait = min(30.0, max(2.0, ra or reset or (2.0 ** attempt), adaptive_pause))
                self._throttle_until = max(self._throttle_until, time.monotonic() + adaptive_pause)
                logger.warning("Cerebras rate limited (attempt %d/5, streak=%d): %s; retrying in %.1fs", attempt + 1, self.rate_limit_streak, self.last_error, wait)
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

            self.rate_limit_streak = 0
            self._throttle_until = 0.0
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






























# --- RSS (standard library parser) -------------------------------------------------------------

RSS_FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPN", "url": "https://www.espn.com/espn/rss/news"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/uk/sport/rss"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
]







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







# ===========================================================================
# 9. DESKS. Each returns (story | None, reason). Publishing/ledger is done by the runner.
# ===========================================================================

SOURCE_HOME = {"ESPN": "https://www.espn.com/", "TheSportsDB": "https://www.thesportsdb.com/",
               "CricketData.org": "https://cricketdata.org/", "Wikipedia": "https://en.wikipedia.org/wiki/Portal:Current_events"}


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(normalize_text(p) for p in parts).encode()).hexdigest()[:16]




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

# ===========================================================================
# 8. SHARED V1 COVERAGE, PUBLICATION AND LIVE-SPORTS SERVICES
# ===========================================================================

COVERAGE_FILE = "coverage_index.json"
PROMPTS_DIR = Path("prompts")
SCHEMAS_DIR = Path("schemas")
RUNS_PER_DAY = 2
EVERGREEN_POSTS_PER_RUN = 10
STATE_RETENTION_DAYS = _env_int("STATE_RETENTION_DAYS", 120)
SEMANTIC_DUP_THRESHOLD = _env_float("SEMANTIC_DUP_THRESHOLD", 0.86)
TOPIC_DUP_THRESHOLD = _env_float("TOPIC_DUP_THRESHOLD", 0.90)
IMAGE_TIMEOUT = _env_int("IMAGE_TIMEOUT", 12)
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_BYTES", 10_000_000)
V1_POST_DELAY_SECONDS = _env_float("POST_DELAY_SECONDS", 3.0)



def load_coverage(state: dict | None = None) -> dict:
    p = Path(COVERAGE_FILE)
    data = {"schema": 1, "updated_at": utc_iso(now_bd()), "records": []}
    if p.exists() and p.stat().st_size:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("records"), list):
                data.update(raw)
        except Exception as exc:
            raise RuntimeError(f"Coverage index unreadable; refusing to run blind: {exc}") from exc

    # Bootstrap permanently from the existing legacy archive once. This keeps already-published knowledge protected.
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
                "run_id": "legacy-archive",
                "telegram_message_id": p_row.get("message_id"),
                "source_urls": urls,
                "fingerprints": [fingerprint(subject, claim), fingerprint(claim)],
                "legacy": True,
            })
            existing.add(cid)
    data["updated_at"] = utc_iso(now_bd())
    return data

def save_coverage(data: dict) -> None:
    if DRY_RUN:
        return
    p = Path(COVERAGE_FILE)
    tmp = Path(COVERAGE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)

def record_key(post: dict) -> str:
    return fingerprint(
        post.get("normalized_subject", ""),
        post.get("central_knowledge_unit", ""),
        post.get("central_claim", ""),
        post.get("sector", ""),
    )

def topic_similarity_coverage(a: dict, b: dict) -> float:
    pieces = [
        similarity(a.get("normalized_subject", ""), b.get("normalized_subject", "")),
        similarity(a.get("central_knowledge_unit", ""), b.get("central_knowledge_unit", "")),
        similarity(a.get("central_claim", ""), b.get("central_claim", "")),
    ]
    return max(pieces) * 0.55 + sum(pieces) / len(pieces) * 0.45

def coverage_candidate_base(post: dict) -> dict:
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

def coverage_match(candidate: dict, coverage: dict) -> tuple[str, dict | None, float]:
    cc = coverage_candidate_base(candidate)
    c_urls = {canonical_url(x.get("url", "")) for x in cc.get("sources", []) if text(x.get("url"))}
    cfp = record_key(cc)
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
        sim = topic_similarity_coverage(cc, row)
        if sim >= TOPIC_DUP_THRESHOLD:
            return "duplicate", row, sim
        if sim >= SEMANTIC_DUP_THRESHOLD and sim > best[2]:
            best = ("related", row, sim)
    return best


def evidence_text_for_validation(candidate: dict) -> str:
    # The research package's source notes and main story form the deterministic local evidence envelope.
    bits=[candidate.get("main_story",""), candidate.get("central_claim",""), candidate.get("historical_date",""), candidate.get("historical_year",""), candidate.get("research_evidence","")]
    for s in candidate.get("sources",[]) if isinstance(candidate.get("sources"), list) else []:
        bits.append(s.get("evidence_note", ""))
    return " ".join(text(x) for x in bits)

def build_evergreen(candidate: dict, editorial: dict) -> dict:
    tags = [text(x) for x in editorial.get("hashtags", []) if text(x)]
    tags = [t if t.startswith("#") else "#" + re.sub(r"[^A-Za-z0-9]", "", t) for t in tags]
    sector_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", candidate.get("sector", "SportsGames"))[:28]
    if sector_tag != "#":
        tags.append(sector_tag)
    # Deduplicate while preserving order. Keep the final published tag set compact.
    seen=set(); tags=[t for t in tags if t and not (t in seen or seen.add(t))][:3]
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
        "body": text(editorial.get("body")),
        "tags": tags,
        "sources": [(a,b) for a,b in sources if b],
        "urls": [b for a,b in sources if b],
        "country": candidate.get("country", ""),
        "region": candidate.get("region", ""),
        "people": candidate.get("people_involved", []),
        "historical_date": candidate.get("historical_date", ""),
        "historical_year": candidate.get("historical_year", ""),
        "image": {},
        "image_status": "card_only",
        "research": candidate,
    }

def table_cell(value: str, header: bool = False, align: str = "left") -> dict:
    cell = {"text": text(value), "align": align, "valign": "middle"}
    if header:
        cell["is_header"] = True
    return cell

def live_event_importance(ev: dict, kind: str) -> float:
    score = {"S": 80, "A": 60, "B": 35, "C": 10}.get(text(ev.get("tier")).upper(), 10)
    names = f"{ev.get('name','')} {ev.get('league','')} {ev.get('stage','')}".lower()
    # `_TIER_HINTS` is shared with the tier classifier, where the second value is a
    # tier label (S/A), not a numeric bonus. Convert that label into the existing TIER_W
    # scale so the live importance scorer remains type-safe.
    for rx, tier in _TIER_HINTS:
        if rx.search(names):
            score = max(score, TIER_W.get(tier, 0))
    if kind == "next" and ev.get("state") == "scheduled":
        score += 5
    if kind == "past" and ev.get("state") == "final":
        score += 5
    return score

def live_event_identity(ev: dict) -> str:
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

def major_live_events(events: list[dict], kind: str, target: date, limit: int = 20) -> list[dict]:
    rows=[]; seen=set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid=live_event_identity(ev)
        if eid in seen:
            continue
        seen.add(eid)
        row=dict(ev); row["event_id"]=eid; row["major_score"]=live_event_importance(row, kind)
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

def live_rows(events: list[dict], kind: str) -> list[list[dict]]:
    if kind == "next":
        rows=[[table_cell("SPORT", True), table_cell("GAME", True), table_cell("TIME", True, "center"), table_cell("COMPETITION", True)]]
        for e in events:
            tm=to_bd(e["start"]).strftime("%a %d %b · %H:%M") if e.get("start") else "TBC"
            rows.append([table_cell(e.get("sport") or "Sport"), table_cell(e.get("name") or "Game"), table_cell(tm, align="center"), table_cell(e.get("league") or "")])
        return rows
    rows=[[table_cell("SPORT", True), table_cell("GAME", True), table_cell("RESULT", True, "center"), table_cell("COMPETITION", True)]]
    for e in events:
        if e.get("style") == "team" and e.get("home") and e.get("away"):
            result=f"{e.get('home_score','')}–{e.get('away_score','')}"
            game=f"{e.get('home')} vs {e.get('away')}"
        else:
            result=e.get("winner") or e.get("detail") or "Completed"
            game=e.get("name") or "Event"
        rows.append([table_cell(e.get("sport") or "Sport"), table_cell(game), table_cell(result, align="center"), table_cell(e.get("league") or "")])
    return rows

def live_rich(kind: str, target: date, events: list[dict]) -> dict:
    label = "NEXT-DAY MAJOR GAMES" if kind == "next" else "PREVIOUS-DAY MAJOR RESULTS"
    caption = f"{label} · {long_date(target)} · Asia/Dhaka"
    return {"blocks": [{"type":"table", "cells": live_rows(events, kind), "is_bordered": True, "is_striped": True, "is_compact": True, "caption": caption}]}

def send_rich(rich_message: dict) -> dict:
    payload={"chat_id": CHANNEL, "rich_message": json.dumps(rich_message, ensure_ascii=False, separators=(",", ":"))}
    return tg_call("sendRichMessage", payload)

def publish_live(kind: str, target: date, events: list[dict]) -> dict:
    return send_rich(live_rich(kind, target, events))

def delete_message(message_id: int | None) -> dict:
    if not message_id:
        return {"ok": True, "skipped": True}
    return tg_call("deleteMessage", {"chat_id": CHANNEL, "message_id": int(message_id)})

def live_pair(state: dict, now: datetime) -> tuple[dict, dict, list[dict], list[dict]]:
    tomorrow=now.date()+timedelta(days=1); yesterday=now.date()-timedelta(days=1)
    ai=AIClient(state.get("ai"))
    # Reuse the shared structured sports-data adapters. They are deterministic and source-aware.
    next_events,_=collect_events(ai, tomorrow, "next", allow_exa_fallback=False)
    past_events,_=collect_events(ai, yesterday, "past", allow_exa_fallback=False)
    next_major=major_live_events(filter_state(next_events,"next",tomorrow),"next",tomorrow)
    past_major=major_live_events(filter_state(past_events,"past",yesterday),"past",yesterday)
    if not next_major:
        raise RuntimeError("No major next-day events found")
    if not past_major:
        raise RuntimeError("No major previous-day results found")
    return ({"target_date": tomorrow.isoformat()}, {"target_date": yesterday.isoformat()}, next_major, past_major)

def record_coverage(coverage: dict, story: dict, run_id: str, message_id: int | None) -> None:
    rec={
        "content_id": record_key(story),
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
        "fingerprints": [record_key(story), fingerprint(story.get("normalized_subject",""), story.get("central_claim",""))],
    }
    coverage.setdefault("records", []).append(rec)
    # Keep the permanent index bounded by records rather than by age. 20k is comfortably beyond multi-year use.
    coverage["records"] = coverage["records"][-20000:]
    coverage["updated_at"] = utc_iso(now_bd())

def publication_id(run_id: str, kind: str, subject: str = "") -> str:
    return f"{run_id}:{kind}:{fingerprint(subject)}"

def publish_with_idempotency(vstate: dict, publication_id: str, publisher: Callable[[], dict]) -> dict:
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

V1_RANK_SCHEMA = OBJ(rankings=ARR(OBJ(post_number=INT, score=INT)))

EDITORIAL_SCHEMA = OBJ(
    headline=STR, body=STR, hashtags=ARR(STR),
)

AGENT_HARD_CASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "qualified", "unsupported", "insufficient", "conflict"]},
        "qualified_claim": {"type": "string"},
        "evidence_summary": {"type": "string"},
        "source_urls": {"type": "array", "maxItems": 6, "items": {"type": "string", "format": "uri"}},
        "conflicts": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    },
    "required": ["verdict", "qualified_claim", "evidence_summary", "source_urls", "conflicts"],
}

PROMPT_FALLBACKS = {
    "exa_sector_discovery_v1.txt": (
        "THE SPORTS NEWSROOM V1: EXA SEARCH DISCOVERY\n\n"
        "Search the public web for evergreen Sports & Games source pages. Do not write the article. "
        "Prefer primary and authoritative sources, precise dates and documented evidence. Exclude current-news material, "
        "promotional pages, live scores, fixtures, standings, transfers, injuries, betting and rumors. "
        "Return normal Exa Search results. The caller supplies a sector-specific intent."
    ),
    "cerebras_rank_v1.txt": (
        "THE SPORTS NEWSROOM V1: CEREBRAS RANKING DESK\n\n"
        "Rank the supplied evergreen Sports & Games candidates using only the provided Exa evidence. "
        "Prioritize evergreen value, source quality, evidence strength, distinct knowledge, global variety, usefulness, "
        "curiosity, visual potential and low repetition risk. Return every candidate exactly once in ranked order."
    ),
    "cerebras_editorial_v1.txt": (
        "THE SPORTS NEWSROOM V1: CEREBRAS EDITORIAL DESK\n\n"
        "Transform one verified evergreen Sports & Games research item into a concise publication-ready Telegram post. "
        "Use ONLY the supplied verified research and evidence. Never invent facts, dates, records, people, locations, rules, origins, numbers or URLs. "
        "Write ONE short paragraph, 40-60 words, in an editorial/storyteller voice. No lists. Do not restate the same point twice. "
        "Headline: 6-14 words, specific and non-clickbait. Return at most 3 relevant hashtags. Do not use current-news framing. "
        "Do not generate or select image URLs. Return only JSON with headline, body and hashtags."
    ),
    "exa_hard_case_agent_v1.txt": (
        "THE SPORTS NEWSROOM V1: EXA AGENT HARD-CASE VERIFICATION\n\n"
        "Resolve difficult evergreen Sports & Games claims such as disputed origins, first/last/only claims, exact dates, "
        "conflicting records and traditional/regional attribution. Prefer multiple authoritative sources, preserve qualifiers, "
        "and never invent facts or URLs. Return only the supplied structured fields."
    ),
}

# ===========================================================================
# 8. THE SPORTS NEWSROOM V1 RESEARCH ENGINE
#     Exa Search -> local candidate reservoir -> Cerebras ranking ->
#     Exa Contents evidence -> optional Exa Agent hard-case verification ->
#     Cerebras editorial -> normalization -> deterministic Telegram publish.
# ===========================================================================

V1_STATE_KEY = "v1"
V1_SECTORS = [
    "Sport Discovery", "Game Discovery", "Interesting Sports Fact", "Interesting Game Fact",
    "Rule Check", "How to Play", "Sport Origin", "Game Origin", "On This Date", "First / Last / Only",
    "Records & Milestones", "Forgotten Sport", "Forgotten Game", "Equipment / Measurement", "Why Does This Happen?",
    "Then vs Now", "New Sport Discovery", "New Tabletop Game Discovery", "Traditional / Regional Game", "Sports & Games People",
]
V1_SEARCH_RESULTS_PER_SECTOR = _env_int("V1_SEARCH_RESULTS_PER_SECTOR", 6)
V1_MAX_CANDIDATES_PER_SECTOR = _env_int("V1_MAX_CANDIDATES_PER_SECTOR", 3)
V1_CONTENTS_URLS_PER_STORY = _env_int("V1_CONTENTS_URLS_PER_STORY", 3)
V1_AGENT_MAX_CASES = _env_int("V1_AGENT_MAX_CASES", 2)
V1_SEARCH_DELAY_SECONDS = _env_float("V1_SEARCH_DELAY_SECONDS", 0.12)
V1_CONTENTS_CHARS = _env_int("V1_CONTENTS_CHARS", 9000)
V1_RANK_MAX_CANDIDATES = 20
V1_RANK_MAX_TOKENS = max(512, min(900, _env_int("V1_RANK_MAX_TOKENS", 700)))
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
V1_CURRENT_TITLE_RX = V1_CURRENT_RX

V1_SECTOR_SEARCH = {
    "Sport Discovery": "unusual lesser-known established sport history rules equipment and how it is played",
    "Game Discovery": "unusual physical traditional regional board card dice or tabletop game history and rules",
    "Interesting Sports Fact": "surprising but well-documented evergreen sports fact with clear historical or technical evidence",
    "Interesting Game Fact": "surprising but well-documented evergreen game fact with historical rules or design evidence",
    "Rule Check": "official or historical sports and games rule that is unusual, misunderstood, or changed over time",
    "How to Play": "clear documented rules setup objective equipment scoring and basic play sequence for an unusual sport or game",
    "Sport Origin": "documented origin and development of a sport, including predecessors, early evidence, spread, and modern form",
    "Game Origin": "documented origin and development of a physical, traditional, board, card, dice, or tabletop game",
    "On This Date": "a well-documented historical sports or games event associated with September {month} {day}; exact date evidence required",
    "First / Last / Only": "precisely documented first, last, or only occurrence in sports or games with strong qualifying evidence",
    "Records & Milestones": "verified historical sports or games record, streak, landmark, or institutional milestone",
    "Forgotten Sport": "discontinued, extinct, nearly forgotten, geographically limited, or replaced sport with documented history",
    "Forgotten Game": "discontinued, ancient, nearly forgotten, geographically limited, or replaced physical or tabletop game",
    "Equipment / Measurement": "meaningful sports or game equipment, measurement, timing, dimensions, scoring, or engineering explanation",
    "Why Does This Happen?": "one real why-question in sport or games explained by science, physics, biomechanics, mathematics, history, culture, or design",
    "Then vs Now": "documented historical versus modern change in rules, equipment, venue, scoring, terminology, technology, or structure",
    "New Sport Discovery": "new or emerging non-digital sport with established rules, documented development, and evidence strong enough for long-term knowledge",
    "New Tabletop Game Discovery": "newer board, card, dice, tabletop, or physical game with documented rules and development, without promotional framing",
    "Traditional / Regional Game": "traditional or regional physical game with documented cultural context, rules, equipment, and evidence",
    "Sports & Games People": "historically important athlete, pioneer, inventor, designer, founder, organizer, rule-maker, historian, or contributor",
}

V1_HARD_SECTORS = {"Sport Origin", "Game Origin", "On This Date", "First / Last / Only", "Records & Milestones", "Then vs Now", "Traditional / Regional Game", "Sports & Games People"}


def _v1_default_state() -> dict:
    return {
        "schema": 1,
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


def v1_state(state: dict) -> dict:
    root = state.setdefault(V1_STATE_KEY, _v1_default_state())
    if not isinstance(root, dict):
        root = _v1_default_state()
        state[V1_STATE_KEY] = root
    root.setdefault("schema", 1)
    root.setdefault("created_at", utc_iso(now_bd()))
    root.setdefault("last_run_at", "")
    root.setdefault("daily", {})
    root.setdefault("runs", [])
    root.setdefault("publication", {})
    root.setdefault("live", _v1_default_state()["live"])
    # One-time migration of the previous live pair/day memory. The old key is data only.
    old = state.get("v4")
    if isinstance(old, dict):
        if not root["live"].get("schedule", {}).get("message_id") and isinstance(old.get("live"), dict):
            root["live"] = json.loads(json.dumps(old.get("live")))
        today = now_bd().date().isoformat()
        if today not in root["daily"] and isinstance(old.get("daily", {}).get(today), dict):
            root["daily"][today] = json.loads(json.dumps(old["daily"][today]))
    return root


def v1_sector_query(sector: str, target: date) -> str:
    template = V1_SECTOR_SEARCH.get(sector, "evergreen sports and games knowledge")
    return template.format(month=target.strftime("%B"), day=target.day)


def _v1_search_system(sector: str, target: date, used_sectors: set[str]) -> str:
    used = ", ".join(sorted(used_sectors)) or "none"
    base = _v1_prompt("exa_sector_discovery_v1.txt", "Retrieve evergreen Sports & Games source pages; do not write an article.")
    return (base + f"\n\nTARGET SECTOR: {sector}\nCALENDAR DATE: {target.isoformat()}\n"
            + f"PREVIOUSLY USED SECTORS TODAY: {used}\n"
            + "Return normal Exa Search results. Do not return a synthetic publication object.")


def _v1_exa_error(r: Any) -> str:
    data = r.data if isinstance(getattr(r, "data", None), dict) else {}
    return text(data.get("error")) or text(getattr(r, "error", "")) or f"HTTP {getattr(r, 'status', 0)}"


def _v1_exa_request(url: str, body: dict, *, timeout: int = 60, retries: int = 0) -> Any:
    """V1 provider boundary. 400s are surfaced, not hidden behind payload guessing."""
    if not EXA_API_KEY:
        return None
    return http(
        "exa", "POST", url, json_body=body,
        headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
        timeout=timeout, retries=retries, backoff=2.0,
    )


def v1_exa_search_sector(sector: str, target: date, used_sectors: set[str], *, mode: str = "auto", num: int | None = None) -> list[dict]:
    """Native Exa /search discovery. No outputSchema, no guessed payload variants."""
    if not EXA_API_KEY:
        return []
    query = v1_sector_query(sector, target)
    body = {
        "query": query,
        "type": mode,
        "numResults": max(1, min(100, int(num or V1_SEARCH_RESULTS_PER_SECTOR))),
        "moderation": True,
        "excludeDomains": V1_EXCLUDE_DOMAINS,
        "contents": {"highlights": True},
        "systemPrompt": _v1_search_system(sector, target, used_sectors),
    }
    if mode != "deep":
        body.pop("type") if mode == "auto" else None
    r = _v1_exa_request("https://api.exa.ai/search", body, timeout=60, retries=1)
    if r is None or not getattr(r, "ok", False) or not isinstance(getattr(r, "data", None), dict):
        tag = ""
        if isinstance(getattr(r, "data", None), dict):
            tag = text(r.data.get("tag"))
        logger.warning("V1 Exa Search failed sector=%s status=%s tag=%s error=%s", sector, getattr(r, "status", 0), tag, redact(_v1_exa_error(r))[:180])
        return []
    out = []
    for item in r.data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        url = text(item.get("url")); title = re.sub(r"\s+", " ", text(item.get("title"))).strip()
        if not url or not title or not re.match(r"^https?://", url, re.I):
            continue
        hs = item.get("highlights") or []
        if not isinstance(hs, list):
            hs = [text(hs)] if text(hs) else []
        highlights = [re.sub(r"\s+", " ", text(h)) for h in hs if text(h)]
        excerpt = " ".join(highlights)[:3500] or re.sub(r"\s+", " ", text(item.get("text")))[:3500]
        alltext = f"{title} {excerpt} {url}"
        if exclusion_reason(alltext):
            reject("v1_discovery_exclusion", f"{sector}: {title[:120]}")
            continue
        published = parse_dt(item.get("publishedDate") or item.get("published_date"))
        recent = bool(published and published >= datetime.now(timezone.utc) - timedelta(days=45))
        title_current = V1_CURRENT_TITLE_RX.search(title)
        excerpt_current = V1_CURRENT_RX.search(excerpt)
        if title_current or (recent and excerpt_current):
            reject("v1_current_news", f"{sector}: {title[:120]}")
            continue
        image = text(item.get("image"))
        published = parse_dt(item.get("publishedDate") or item.get("published_date"))
        out.append({
            "sector": sector,
            "normalized_subject": title,
            "central_knowledge_unit": title,
            "central_claim": excerpt[:700] or title,
            "research_focus": v1_sector_query(sector, target),
            "topic": title,
            "sport_or_game": "",
            "source_url": url,
            "source": source_label(url, text(item.get("author"))),
            "grade": grade_of(url),
            "highlights": highlights[:6],
            "text": excerpt,
            "image": image,
            "published": published,
            "exa_id": text(item.get("id")),
            "favicon": text(item.get("favicon")),
            "search_request_id": text(r.data.get("requestId")),
        })
    return out


def v1_discover_all_sectors(target: date, used_sectors: set[str], *, recovery_sectors: set[str] | None = None) -> list[dict]:
    sectors = [s for s in V1_SECTORS if not recovery_sectors or s in recovery_sectors]
    raw: list[dict] = []
    # Deliberately sequential. Exa's documented default /search limit is 10 QPS; this avoids bursts and is
    # easier to reason about in GitHub Actions. The contents phase is batched separately.
    for sector in sectors:
        mode = "deep" if sector in V1_HARD_SECTORS and recovery_sectors else "auto"
        rows = v1_exa_search_sector(sector, target, used_sectors, mode=mode)
        raw.extend(rows)
        sleep(V1_SEARCH_DELAY_SECONDS)
    return raw


def v1_network_url(url: str) -> str:
    """Return a valid absolute URL for provider requests while keeping canonicalization separate."""
    raw=text(url).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw="https://"+raw
    parts=urlsplit(raw)
    if parts.scheme.lower() not in {"http","https"} or not parts.netloc:
        return ""
    query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    path=re.sub(r"/+","/",parts.path or "/")
    return parts._replace(scheme=parts.scheme.lower(),netloc=parts.netloc.lower(),path=path,query=urlencode(query)).geturl()


def v1_candidate_key(row: dict) -> str:
    return fingerprint(row.get("sector", ""), row.get("normalized_subject", ""), row.get("central_knowledge_unit", ""))


def v1_discovery_quality(row: dict) -> float:
    grade = {"A": 30, "B": 20, "C": 8}.get(text(row.get("grade")).upper(), 5)
    h = min(20, len(row.get("highlights") or []) * 3)
    image = 5 if text(row.get("image")) else 0
    title = min(15, len(tokens(row.get("normalized_subject"))) * 2)
    return float(grade + h + image + title)


def v1_build_reservoir(raw: list[dict], coverage: dict, used_sectors: set[str]) -> tuple[list[dict], list[str]]:
    accepted: list[dict] = []
    reasons: list[str] = []
    seen_urls: set[str] = set()
    per_sector: Counter[str] = Counter()
    seen_local: list[dict] = []
    ordered = sorted(raw, key=lambda x: (v1_discovery_quality(x), text(x.get("grade"))), reverse=True)
    for c in ordered:
        url = canonical_url(text(c.get("source_url")))
        if not url or url in seen_urls:
            reasons.append("url_duplicate")
            continue
        status, row, score = coverage_match(c, coverage)
        if status == "duplicate":
            reasons.append(f"coverage_duplicate:{c.get('sector')}:{c.get('normalized_subject')}")
            continue
        if status == "related":
            # Do not call Cerebras for every fuzzy match. Only the strongest borderline matches are
            # escalated later by the selector if needed.
            c["coverage_status"] = "related"
            c["coverage_score"] = round(score, 4)
        sec = text(c.get("sector"))
        if per_sector[sec] >= V1_MAX_CANDIDATES_PER_SECTOR:
            continue
        local_dup = False
        for p in seen_local:
            if p.get("sector") == sec and topic_similarity_coverage(c, p) >= SEMANTIC_DUP_THRESHOLD:
                local_dup = True
                break
        if local_dup:
            reasons.append(f"same_sector_duplicate:{sec}")
            continue
        c["candidate_id"] = v1_candidate_key(c)
        network_url=v1_network_url(text(c.get("source_url")))
        if not network_url:
            reasons.append("invalid_source_url")
            continue
        c["source_urls"] = [network_url]
        c["sources"] = [{"name": c.get("source"), "url": network_url, "type": "Exa Search result", "evidence_note": " ".join(c.get("highlights") or [])[:500]}]
        c["research_images"] = ([{"url": text(c["image"]), "source_page_url": network_url, "source_name": c.get("source"), "source_type": "Exa Search result image"}] if text(c.get("image")) else [])
        accepted.append(c)
        seen_urls.add(url); seen_local.append(c); per_sector[sec] += 1
    # We want all 20 sectors represented in the first reservoir. If a sector has no survivor, recovery will target it.
    return accepted, reasons


def v1_rank(ai: "AIClient", candidates: list[dict], used_sectors: set[str]) -> list[dict]:
    """Rank exactly one best candidate per sector; keep the Cerebras request intentionally tiny."""
    if not candidates:
        return []
    by_sector: dict[str, list[dict]] = {}
    for c in candidates:
        by_sector.setdefault(text(c.get("sector")), []).append(c)
    pool: list[dict] = []
    for sec in V1_SECTORS:
        rows = sorted(by_sector.get(sec, []), key=v1_discovery_quality, reverse=True)
        if rows:
            pool.append(rows[0])
    if len(pool) < V1_RANK_MAX_CANDIDATES:
        existing = {id(x) for x in pool}
        for c in sorted(candidates, key=v1_discovery_quality, reverse=True):
            if id(c) in existing:
                continue
            pool.append(c); existing.add(id(c))
            if len(pool) >= V1_RANK_MAX_CANDIDATES:
                break
    pool = pool[:V1_RANK_MAX_CANDIDATES]

    payload=[]
    for i,c in enumerate(pool,1):
        hs=[re.sub(r"\s+", " ", text(x))[:180] for x in (c.get("highlights") or []) if text(x)]
        payload.append({
            "candidate_number": i,
            "sector": text(c.get("sector")),
            "subject": text(c.get("normalized_subject"))[:180],
            "knowledge_unit": text(c.get("central_knowledge_unit"))[:180],
            "claim": text(c.get("central_claim"))[:220],
            "source_grade": text(c.get("grade")),
            "has_image": bool(c.get("image")),
            "highlight": hs[0] if hs else "",
            "coverage": text(c.get("coverage_status", "new")),
        })

    def deterministic()->list[dict]:
        rows=[{"post_number":i+1,"score":int(round(v1_discovery_quality(c)*2)),"reason":"deterministic fallback","_candidate_pool":pool} for i,c in enumerate(pool)]
        rows.sort(key=lambda x:(x["score"],-x["post_number"]),reverse=True)
        return rows

    if not ai.available or ai.fatal:
        return deterministic()
    system=_v1_prompt("cerebras_rank_v1.txt",
        "You are the ranking desk for The Sports Newsroom V1. Rank the 20-sector evergreen candidate set using only the supplied evidence. "
        "Return every candidate number exactly once in ranked order with a 0-100 score. "
        f"Sectors already published today: {', '.join(sorted(used_sectors)) or 'none'}. Prefer unused sectors. Do not write explanations.")
    obj=ai.json("v1_rank_reservoir",system,json.dumps(payload,ensure_ascii=False,separators=(",",":")),V1_RANK_SCHEMA,max_tokens=V1_RANK_MAX_TOKENS,temperature=0.0)
    if not obj:
        logger.warning("V1 Cerebras ranking unavailable; using deterministic ranking fallback: %s",ai.last_error)
        return deterministic()
    rows=[]; seen=set()
    for r in obj.get("rankings") or []:
        try: n=int(r.get("post_number")); score=max(0,min(100,int(r.get("score"))))
        except (TypeError, ValueError): continue
        if 1<=n<=len(pool) and n not in seen:
            seen.add(n); rows.append({"post_number":n,"score":score,"reason":""})
    if len(rows)<max(10,int(len(pool)*0.75)):
        logger.warning("V1 Cerebras ranking returned incomplete ordering (%d/%d); using deterministic ranking",len(rows),len(pool))
        return deterministic()
    for i in range(1,len(pool)+1):
        if i not in seen:
            rows.append({"post_number":i,"score":int(round(v1_discovery_quality(pool[i-1])*2)),"reason":"completion fallback"})
    rows.sort(key=lambda x:(x["score"],-x["post_number"]),reverse=True)
    return [{**r,"_candidate_pool":pool} for r in rows]


def v1_contents_for_urls(urls: list[str], *, text_mode: bool = True) -> tuple[dict[str,dict], str]:
    """Batch Exa /contents. URLs are already-known Exa Search outputs."""
    normalized=[]; seen=set()
    for raw in urls:
        nu=v1_network_url(raw)
        cu=canonical_url(nu)
        if nu and cu and cu not in seen:
            seen.add(cu); normalized.append(nu)
    urls=normalized[:100]
    if not urls: return {}, "no_urls"
    body={"urls":urls,"highlights":True,"text":True,"maxAgeHours":720}
    # No outputSchema, no legacy context, no livecrawl. Contents is a retrieval/extraction endpoint.
    r=_v1_exa_request("https://api.exa.ai/contents",body,timeout=120,retries=1)
    if r is None or not getattr(r,"ok",False) or not isinstance(getattr(r,"data",None),dict):
        logger.warning("V1 Exa Contents failed status=%s error=%s",getattr(r,"status",0),redact(_v1_exa_error(r))[:180])
        return {},_v1_exa_error(r) if r else "provider_unavailable"
    out={}
    for item in r.data.get("results",[]) or []:
        if not isinstance(item,dict): continue
        result_url=canonical_url(text(item.get("url")))
        result_id=canonical_url(text(item.get("id")))
        key=result_url or result_id
        if not key: continue
        hs=item.get("highlights") or []
        if not isinstance(hs,list): hs=[text(hs)] if text(hs) else []
        page={
            "url":text(item.get("url")) or text(item.get("id")) or key,
            "title":text(item.get("title")),
            "author":text(item.get("author")),
            "published":parse_dt(item.get("publishedDate") or item.get("published_date")),
            "text":re.sub(r"\s+"," ",text(item.get("text")))[:V1_CONTENTS_CHARS],
            "highlights":[re.sub(r"\s+"," ",text(x)) for x in hs if text(x)][:8],
            "summary":text(item.get("summary")),
        }
        out[key]=page
        # Exa may return a canonical document URL different from the requested ID/URL (for example an HTML/PDF redirect).
        # Index both forms so the original Search source still resolves to the extracted page.
        if result_id: out[result_id]=page
        if result_url: out[result_url]=page
    statuses={text(x.get("id")):text(x.get("status")) for x in (r.data.get("statuses") or []) if isinstance(x,dict)}
    for k,v in statuses.items():
        ck=canonical_url(k)
        if ck and ck in out: out[ck]["status"]=v
    return out,"ok"


def v1_evidence_query(candidate: dict) -> str:
    sector=text(candidate.get("sector"))
    focus=text(candidate.get("research_focus"))
    claim=text(candidate.get("central_claim"))
    return f"{sector}: verify {text(candidate.get('normalized_subject'))}. {focus}. Focus on evidence for the claim: {claim[:400]}"


def v1_evidence_text(candidate: dict, pages: dict[str,dict]) -> str:
    parts=[]
    for src in candidate.get("sources",[]) if isinstance(candidate.get("sources"),list) else []:
        u=canonical_url(text(src.get("url")))
        page=pages.get(u,{})
        if not page: continue
        hs=page.get("highlights") or []
        txt=" ".join(hs)[:3000] or text(page.get("text"))[:5000]
        if txt:
            parts.append(f"SOURCE: {text(page.get('title')) or src.get('name')}\nURL: {page.get('url') or u}\n{txt}")
    return "\n\n".join(parts)[:14000]


def v1_exa_search_query(query: str, target: date, used_sectors: set[str], *, mode: str = "auto", num: int = 4) -> list[dict]:
    if not EXA_API_KEY or not text(query):
        return []
    body={
        "query": text(query),
        "numResults": max(1, min(100, int(num))),
        "moderation": True,
        "excludeDomains": V1_EXCLUDE_DOMAINS,
        "contents": {"highlights": True},
        "systemPrompt": _v1_search_system("Targeted verification", target, used_sectors),
    }
    if mode != "auto":
        body["type"]=mode
    r=_v1_exa_request("https://api.exa.ai/search",body,timeout=60,retries=1)
    if r is None or not getattr(r,"ok",False) or not isinstance(getattr(r,"data",None),dict):
        logger.warning("V1 targeted Exa Search failed status=%s error=%s",getattr(r,"status",0),redact(_v1_exa_error(r))[:180])
        return []
    out=[]
    for item in r.data.get("results",[]) or []:
        if not isinstance(item,dict): continue
        url=text(item.get("url")); title=re.sub(r"\s+"," ",text(item.get("title"))).strip()
        if not url or not title or not re.match(r"^https?://",url,re.I): continue
        hs=item.get("highlights") or []
        if not isinstance(hs,list): hs=[text(hs)] if text(hs) else []
        highlights=[re.sub(r"\s+"," ",text(h)) for h in hs if text(h)]
        excerpt=" ".join(highlights)[:3500] or re.sub(r"\s+"," ",text(item.get("text")))[:3500]
        published = parse_dt(item.get("publishedDate") or item.get("published_date"))
        recent = bool(published and published >= datetime.now(timezone.utc) - timedelta(days=45))
        if exclusion_reason(f"{title} {excerpt} {url}") or V1_CURRENT_TITLE_RX.search(title) or (recent and V1_CURRENT_RX.search(excerpt)):
            continue
        out.append({"url":url,"title":title,"highlights":highlights[:6],"text":excerpt,"source":source_label(url,text(item.get("author"))),"grade":grade_of(url),"image":text(item.get("image")),"exa_id":text(item.get("id")),"published":published})
    return out


def v1_secondary_search(candidate: dict, target: date) -> list[dict]:
    q=(v1_evidence_query(candidate)+" Prefer an independent authoritative source. Verify exact dates, names, records, rules, or historical qualifiers. Search for the specific subject, not the generic sport.")
    rows=v1_exa_search_query(q,target,set(),mode="auto",num=5)
    wanted=(tokens(candidate.get("normalized_subject")) | tokens(candidate.get("central_knowledge_unit")) | tokens(candidate.get("central_claim")))
    kept=[]
    for r in rows:
        rt=tokens((r.get("title","")+" "+r.get("text","")+" "+r.get("url", "")))
        if wanted and len(wanted & rt) < max(1,min(2,len(wanted))):
            continue
        r["secondary_search_reason"]=q[:700]
        kept.append(r)
    return kept


def v1_hard_case_needed(candidate: dict, pages: dict[str,dict]) -> bool:
    sector=text(candidate.get("sector"))
    if sector in V1_HARD_SECTORS:
        return True
    evidence=v1_evidence_text(candidate,pages)
    nums=re.findall(r"\b\d{3,4}\b",text(candidate.get("central_claim"))+" "+evidence)
    return not evidence or len(nums)>=3


def _v1_prompt(name: str, fallback: str = "") -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError as exc:
            logger.warning("Prompt file unavailable (%s): %s; using embedded fallback", name, redact(str(exc)))
    return PROMPT_FALLBACKS.get(name, fallback)

def v1_agent_hard_case(candidate: dict, evidence: str) -> dict | None:
    if not EXA_API_KEY:
        return None
    schema_path = SCHEMAS_DIR / "exa_agent_hard_case_v1.json"
    schema = dict(AGENT_HARD_CASE_SCHEMA)
    if schema_path.exists():
        try:
            loaded = json.loads(schema_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                schema = loaded
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Agent schema file unavailable: %s; using embedded schema", redact(str(exc)))
    if not isinstance(schema,dict) or not isinstance(schema.get("properties"),dict) or len(schema.get("properties",{}))>10:
        logger.error("V1 Agent schema invalid or exceeds provider property limit")
        return None
    system=_v1_prompt("exa_hard_case_agent_v1.txt",
        "Resolve a difficult evergreen Sports & Games research claim. Prefer authoritative independent sources. "
        "Qualify first/last/only/origin claims precisely. Do not use current news. Do not invent facts or URLs.")
    q=(system+"\n\nCASE:\n"+json.dumps({"candidate":{k:candidate.get(k,"") for k in ("sector","normalized_subject","central_knowledge_unit","central_claim","research_focus")},"existing_evidence":evidence[:7000]},ensure_ascii=False))
    body={"query":q,"effort":"auto","outputSchema":schema}
    r=_v1_exa_request("https://api.exa.ai/agent/runs",body,timeout=90,retries=0)
    if r is None or not getattr(r,"ok",False) or not isinstance(getattr(r,"data",None),dict):
        logger.warning("V1 Exa Agent create failed status=%s error=%s",getattr(r,"status",0),redact(_v1_exa_error(r))[:180]); return None
    run_id=text(r.data.get("id"))
    if not run_id: return None
    deadline=time.monotonic()+_env_int("V1_AGENT_TIMEOUT_SECONDS",120)
    while time.monotonic()<deadline:
        g=http("exa","GET",f"https://api.exa.ai/agent/runs/{quote(run_id)}",headers={"Authorization":f"Bearer {EXA_API_KEY}"},timeout=30,retries=0)
        if g.ok and isinstance(g.data,dict):
            status=text(g.data.get("status")).lower()
            if status=="completed":
                output=g.data.get("output") or {}
                structured=output.get("structured") if isinstance(output,dict) else None
                if isinstance(structured,dict):
                    structured["grounding"]=output.get("grounding") if isinstance(output,dict) else []
                    structured["run_id"]=run_id
                    return structured
                return None
            if status in {"failed","cancelled"}:
                logger.warning("V1 Exa Agent terminal status=%s run=%s",status,run_id); return None
        sleep(4)
    logger.warning("V1 Exa Agent timed out run=%s",run_id)
    return None


def v1_verify_selected(selected: list[dict], target: date) -> tuple[list[dict], str]:
    urls=[]
    for c in selected:
        for src in c.get("sources",[]) if isinstance(c.get("sources"),list) else []:
            u=text(src.get("url"))
            if u: urls.append(u)
    pages,status=v1_contents_for_urls(urls)
    if status!="ok" and not pages:
        return [],status
    verified=[]; agent_cases=0
    for c in selected:
        urls=[text(s.get("url")) for s in c.get("sources",[]) if isinstance(s,dict) and text(s.get("url"))]
        evidence=v1_evidence_text(c,pages)
        available=[u for u in urls if canonical_url(u) in pages]
        # Evidence quality gate: at least one successful known-source extraction with useful text.
        if len(available)<1 or len(evidence.strip())<300:
            extra=v1_secondary_search(c,target)
            for row in extra:
                u=v1_network_url(text(row.get("url")))
                if not u or canonical_url(u) in {canonical_url(x) for x in urls}: continue
                urls.append(u)
                c.setdefault("sources",[]).append({"name":row.get("source"),"url":u,"type":"Exa secondary search","evidence_note":" ".join(row.get("highlights") or [])[:500]})
                if text(row.get("image")):
                    c.setdefault("research_images",[]).append({"url":text(row.get("image")),"source_page_url":u,"source_name":row.get("source"),"source_type":"Exa secondary search image"})
                if len(urls)>=V1_CONTENTS_URLS_PER_STORY: break
            extra_pages,_=v1_contents_for_urls(urls[:V1_CONTENTS_URLS_PER_STORY])
            pages.update(extra_pages)
            evidence=v1_evidence_text(c,pages)
            available=[u for u in urls if canonical_url(u) in pages]
        if len(available)<1 or len(evidence.strip())<300:
            reject("v1_evidence_insufficient",text(c.get("normalized_subject"))); continue
        if v1_hard_case_needed(c,pages) and agent_cases<V1_AGENT_MAX_CASES:
            agent=v1_agent_hard_case(c,evidence)
            if agent:
                agent_cases += 1
                verdict=text(agent.get("verdict")).lower()
                if verdict in {"unsupported","false","reject","insufficient"}:
                    reject("v1_agent_rejected",text(c.get("normalized_subject"))); continue
                if text(agent.get("qualified_claim")):
                    c["central_claim"]=text(agent.get("qualified_claim"))
                c["agent_evidence"]=text(agent.get("evidence_summary"))
                c["agent_sources"]= [text(x) for x in (agent.get("source_urls") or []) if text(x)][:6]
                c["agent_conflicts"]= [text(x) for x in (agent.get("conflicts") or []) if text(x)][:6]
                c["agent_grounding"]=agent.get("grounding") or []
                if c["agent_evidence"]:
                    evidence=(evidence+"\n\nAGENT VERIFICATION:\n"+c["agent_evidence"][:5000]).strip()
                # Agent may identify better sources. Add them for deterministic content retrieval, but do not let Agent evidence bypass Contents.
                for raw_u in c["agent_sources"]:
                    u=v1_network_url(raw_u)
                    if u and canonical_url(u) not in {canonical_url(x) for x in urls}:
                        urls.append(u)
                        c.setdefault("sources",[]).append({"name":source_label(u),"url":u,"type":"Exa Agent source","evidence_note":"Agent identified source; verify with Exa Contents."})
                agent_pages,_=v1_contents_for_urls(urls[:V1_CONTENTS_URLS_PER_STORY])
                pages.update(agent_pages)
                evidence=v1_evidence_text(c,pages)
                if c.get("agent_evidence"):
                    evidence=(evidence+"\n\nAGENT VERIFICATION:\n"+c["agent_evidence"][:5000]).strip()
        c["verified_pages"]={k:pages[k] for k in [canonical_url(x) for x in urls] if k in pages}
        c["research_evidence"]=evidence[:14000]
        c["research_images"]=[x for x in (c.get("research_images") or []) if isinstance(x,dict) and text(x.get("url"))]
        verified.append(c)
    if not verified:
        return [],"no_verified_candidates"
    return verified,"ok"


def v1_normalize_story(story: dict) -> dict:
    """Canonicalize evergreen publication data before the single renderer sees it."""
    out=dict(story) if isinstance(story,dict) else {}
    # Evergreen publications are card-only. Research image URLs are retained upstream for evidence
    # provenance, but are never sent to Telegram. This removes remote-image failures entirely.
    out["image"]={}
    out["sources"]=[x for x in (out.get("sources") or []) if isinstance(x,(list,tuple)) and len(x)>=2]
    out["urls"]=[text(x[1]) for x in out["sources"] if len(x)>=2 and text(x[1])]
    # Legacy point lists are intentionally ignored. V1 editorial output is one paragraph.
    out["tags"]=[text(x) for x in (out.get("tags") or []) if text(x)]
    out["people"]=out.get("people") if isinstance(out.get("people"),list) else []
    out["image_status"]="card_only"
    return out


def v1_validate_editorial(candidate: dict, editorial: dict) -> tuple[bool, str, dict]:
    """V1 editorial gate: preserve evergreen rules without inheriting overly broad legacy current-news matching."""
    if not isinstance(editorial,dict):
        return False,"editorial_not_object",{}
    headline=text(editorial.get("headline")); body=text(editorial.get("body"))
    words=re.findall(r"\b\w+[’'\w-]*\b",headline)
    body_words=re.findall(r"\b\w+[’'\w-]*\b",body)
    if not 6<=len(words)<=14: return False,"headline_word_count",{}
    if not 40<=len(body_words)<=60: return False,"body_word_count",{}
    if V1_CURRENT_RX.search(f"{headline} {body}"): return False,"current_news_language",{}
    hashtags = [text(x) for x in (editorial.get("hashtags") or []) if text(x)]
    if len(hashtags) > 3: return False,"hashtag_count",{}
    sources=candidate.get("sources")
    if not isinstance(sources,list) or not sources: return False,"no_verified_sources",{}
    evidence_numbers=set(re.findall(r"\b\d{1,4}\b",json.dumps(json_safe(candidate),ensure_ascii=False)+evidence_text_for_validation(candidate)))
    generated_numbers=set(re.findall(r"\b\d{1,4}\b",f"{headline} {body}"))
    if not generated_numbers.issubset(evidence_numbers): return False,"unsupported_number",{}
    return True,"ok",{**candidate,"editorial":editorial}


def v1_editorialize(ai: "AIClient", candidate: dict, evidence: str) -> dict | None:
    if not ai.available or ai.fatal:
        return None
    compact={
        "sector":text(candidate.get("sector")),
        "topic":text(candidate.get("topic") or candidate.get("normalized_subject")),
        "normalized_subject":text(candidate.get("normalized_subject")),
        "central_knowledge_unit":text(candidate.get("central_knowledge_unit")),
        "central_claim":text(candidate.get("central_claim")),
        "country":text(candidate.get("country")),
        "region":text(candidate.get("region")),
        "historical_date":text(candidate.get("historical_date")),
        "historical_year":text(candidate.get("historical_year")),
        "sources":[{"name":text(x.get("name")),"url":v1_network_url(text(x.get("url"))),"grade":text(x.get("grade"))} for x in (candidate.get("sources") or []) if isinstance(x,dict) and v1_network_url(text(x.get("url")))][:3],
    }
    user=json.dumps({"research":json_safe(compact),"source_evidence":text(evidence)[:5500]},ensure_ascii=False,separators=(",",":"))
    system=_v1_prompt("cerebras_editorial_v1.txt",
        "Transform one verified evergreen Sports & Games research item into a concise Telegram post. Use only supplied evidence. "
        "Do not invent facts or URLs. Return only JSON with headline, body and hashtags.")
    return ai.json("v1_editorial_post",system,user,EDITORIAL_SCHEMA,max_tokens=max(650,_env_int("V1_EDITORIAL_MAX_TOKENS",900)),temperature=0.25)


def v1_publish_evergreen(story: dict) -> dict:
    """Publish evergreen posts as one fixed 1200x675 branded card with HTML caption."""
    story=v1_normalize_story(story)
    rendered=render_evergreen_post(story)
    card_path=make_card(story)
    if not card_path:
        return {"ok":False,"description":"V1 evergreen publication failed: branded card generation failed"}
    try:
        payload={
            "chat_id":CHANNEL,
            "caption":rendered["html"],
            "parse_mode":rendered["parse_mode"],
        }
        return tg_call("sendPhoto",payload,card_path)
    finally:
        try:
            os.remove(card_path)
        except Exception:
            pass


def v1_run_once(mode: str = "live") -> int:
    global DRY_RUN
    if mode == "diagnose":
        return _run_diagnose()
    REPORT.reset(); _EXA_CACHE.clear()
    dry = bool(mode == "dry-run" or DRY_RUN)
    if not dry and not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing"); return 1
    try:
        state=load_state(); coverage=load_coverage(state)
    except Exception as exc:
        logger.error("V1 state/coverage load failed: %s", redact(str(exc))); return 1
    vs=v1_state(state)
    now=now_bd(); run_slot="AM" if now.hour < 14 else "PM"; run_id=f"{now.date().isoformat()}-{run_slot}"
    daily=vs["daily"].setdefault(now.date().isoformat(),{"published_sectors":[],"run_ids":[]})
    used_sectors=set(daily.get("published_sectors",[]))
    if run_id in daily.get("run_ids",[]) and not dry:
        logger.info("V1 run %s already completed; nothing to do.",run_id); return 0
    deadline=Deadline(RUN_DEADLINE_SECONDS)
    ai=AIClient(state.get("ai"))
    if not ai.available:
        logger.error("CEREBRAS_API_KEY is required for V1 ranking/editorial generation"); return 1
    raw=v1_discover_all_sectors(now.date(),used_sectors)
    reservoir,reasons=v1_build_reservoir(raw,coverage,used_sectors)
    represented={text(x.get("sector")) for x in reservoir}
    missing=set(V1_SECTORS)-represented
    if missing:
        logger.info("V1 recovery: %d sectors need targeted discovery",len(missing))
        raw2=v1_discover_all_sectors(now.date(),used_sectors,recovery_sectors=missing)
        r2,_=v1_build_reservoir(raw2,coverage,used_sectors)
        reservoir.extend(r2); represented={text(x.get("sector")) for x in reservoir}; missing=set(V1_SECTORS)-represented
    if missing:
        logger.warning("V1 discovery partial: %d/%d sectors remain uncovered this run: %s",len(missing),len(V1_SECTORS),", ".join(sorted(missing)))
    ranked=v1_rank(ai,reservoir,used_sectors) if reservoir else []
    if not ranked:
        logger.warning("V1 ranking produced no candidates; continuing to live pair only: %s", ai.last_error)
    pool=ranked[0].get("_candidate_pool") if ranked and isinstance(ranked[0].get("_candidate_pool"),list) else reservoir
    by_num={i+1:c for i,c in enumerate(pool)}
    ordered=[by_num[r["post_number"]] for r in ranked if r["post_number"] in by_num]
    selected=[]; selected_sectors=set()
    for prefer_unused in (True,False):
        for c in ordered:
            sec=text(c.get("sector"))
            if sec in selected_sectors: continue
            if prefer_unused and sec in used_sectors: continue
            if not prefer_unused and sec not in used_sectors: continue
            selected.append(c); selected_sectors.add(sec)
            if len(selected) >= EVERGREEN_POSTS_PER_RUN: break
        if len(selected) >= EVERGREEN_POSTS_PER_RUN: break
    if len(selected) < EVERGREEN_POSTS_PER_RUN:
        logger.info("V1 partial evergreen selection: %d/%d target posts available",len(selected),EVERGREEN_POSTS_PER_RUN)
    if deadline.expired():
        logger.warning("V1 deadline reached before evergreen verification; continuing with live pair")
    verification_targets=[]; verification_sectors=set()
    for c in ordered:
        sec=text(c.get("sector"))
        if sec not in verification_sectors:
            verification_targets.append(c); verification_sectors.add(sec)
        if len(verification_targets)>=min(16,len(ordered)): break
    verified,status=v1_verify_selected(verification_targets,now.date()) if verification_targets else ([],"skipped")
    if status!="ok":
        logger.warning("V1 verification partial/unavailable: %s; publishing only candidates that still pass editorial validation",status)
    posted=[]; selected_ids={id(x) for x in selected}
    stream=selected+[c for c in verified if id(c) not in selected_ids]
    posted_sectors=set()
    for cand in stream:
        if len(posted)>=EVERGREEN_POSTS_PER_RUN or deadline.expired(): break
        sec=text(cand.get("sector"))
        if not sec or sec in posted_sectors: continue
        evidence=text(cand.get("research_evidence"))
        editorial=v1_editorialize(ai,cand,evidence)
        if not editorial:
            logger.warning("V1 editorial generation failed for sector=%s subject=%s: %s",sec,text(cand.get("normalized_subject")),ai.last_error); continue
        ok,why,out=v1_validate_editorial(cand,editorial)
        if not ok:
            reject("v1_editorial_invalid",why); continue
        story=v1_normalize_story(build_evergreen(cand,editorial))
        story["image"]=story.get("image") if isinstance(story.get("image"),dict) else {}
        pubid=publication_id(run_id,"evergreen",story.get("normalized_subject") or story.get("topic"))
        res=publish_with_idempotency(vs,pubid,lambda st=story,do=dry: v1_publish_evergreen(st) if not do else {"ok":True,"dry_run":True,"result":{"message_id":None}})
        if not res.get("ok"): 
            if res.get("uncertain"):
                logger.error("V1 evergreen delivery uncertain for %s",story.get("headline")); break
            continue
        mid=(res.get("result") or {}).get("message_id")
        record_coverage(coverage,story,run_id,mid); coverage["updated_at"]=utc_iso(now_bd())
        used_sectors.add(sec); daily.setdefault("published_sectors",[]).append(sec); daily["published_sectors"]=list(dict.fromkeys(daily["published_sectors"]))
        posted_sectors.add(sec); posted.append(story)
        state.setdefault("posts",[]).append({
            "desk":"evergreen_v1","format":story.get("sector"),"sector":story.get("sector"),"topic":story.get("topic"),
            "normalized_subject":story.get("normalized_subject"),"central_knowledge_unit":story.get("central_knowledge_unit"),
            "claim":story.get("central_claim"),"headline":story.get("headline"),"angle":story.get("angle"),
            "urls":[canonical_url(u) for u in story.get("urls",[])],"message_id":mid,"posted_at":utc_iso(now_bd()),"run_id":run_id,
            "image_status":story.get("image_status"),
        })
        if not dry: save_state(state); save_coverage(coverage)
        if len(posted) < EVERGREEN_POSTS_PER_RUN: sleep(V1_POST_DELAY_SECONDS)
    if len(posted) != EVERGREEN_POSTS_PER_RUN:
        logger.info("V1 evergreen phase partial: %d/%d target posts published; continuing to live pair",len(posted),EVERGREEN_POSTS_PER_RUN)
    try:
        sched_meta,res_meta,next_events,past_events=live_pair(state,now)
    except Exception as exc:
        logger.error("V1 live pair generation failed: %s",redact(str(exc))); return 1
    old_sched=vs["live"]["schedule"].get("message_id"); old_res=vs["live"]["results"].get("message_id")
    def publish_or_preview(kind,target,events):
        if dry: return {"ok":True,"dry_run":True,"result":{"message_id":None}}
        return publish_with_idempotency(vs,publication_id(run_id,kind,target.isoformat()),lambda:publish_live(kind,target,events))
    sr=publish_or_preview("live_schedule",date.fromisoformat(sched_meta["target_date"]),next_events)
    if not sr.get("ok"): logger.error("New V1 live schedule failed; preserving old pair"); return 1
    rr=publish_or_preview("live_results",date.fromisoformat(res_meta["target_date"]),past_events)
    if not rr.get("ok"): logger.error("New V1 live results failed; preserving old pair"); return 1
    if not dry:
        new_sched=(sr.get("result") or {}).get("message_id"); new_res=(rr.get("result") or {}).get("message_id")
        if not new_sched or not new_res: return 1
        vs["live"]["schedule"]={"message_id":new_sched,"target_date":sched_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in next_events]}
        vs["live"]["results"]={"message_id":new_res,"target_date":res_meta["target_date"],"updated_at":utc_iso(now_bd()),"event_ids":[x.get("event_id") for x in past_events]}
        state["ai"]=ai.export(); state["last_run_at"]=utc_iso(now_bd()); save_state(state)
        for mid in (old_sched,old_res):
            if mid and int(mid) not in {int(new_sched),int(new_res)}:
                dr=delete_message(mid)
                if not dr.get("ok"): logger.warning("Could not delete old V1 live message %s: %s",mid,text(dr.get("description")))
    daily["run_ids"]=list(dict.fromkeys(daily.get("run_ids",[])+[run_id]))
    vs["last_run_at"]=utc_iso(now_bd())
    if not dry:
        vs["runs"].append({"run_id":run_id,"at":utc_iso(now_bd()),"evergreen":len(posted),"live_schedule":vs["live"]["schedule"].get("message_id"),"live_results":vs["live"]["results"].get("message_id"),"used_sectors":sorted(set(x.get("sector") for x in posted)),"exit":0})
        state["ai"]=ai.export(); state["last_run_at"]=utc_iso(now_bd()); save_state(state); save_coverage(coverage)
    logger.info("V1 RUN %s %s: %d evergreen + 2 live", "DRY" if dry else "SUCCESS", run_id, len(posted)); return 0



def v1_architecture_tests() -> int:
    failures=[]; checks=0
    def ck(ok,msg):
        nonlocal checks
        checks += 1
        if not ok: failures.append(msg)

    ck(len(V1_SECTORS) == 20, "sector count")
    ck(len(set(V1_SECTORS)) == 20, "sector uniqueness")
    ck(EVERGREEN_POSTS_PER_RUN == 10, "publication volume per run")
    ck(RUNS_PER_DAY == 2, "scheduled run count")
    ck(STATE_RETENTION_DAYS > 0, "state retention configuration")
    ck(run_once is v1_run_once, "canonical run_once wiring")
    ck(run_diagnose is run_once, "diagnose aliases canonical run entrypoint")
    ck(v1_network_url("example.com/a/?utm_source=x") == "https://example.com/a/", "network URL normalization")
    old_key=globals().get("EXA_API_KEY")
    old_req=globals().get("_v1_exa_request")
    try:
        globals()["EXA_API_KEY"]="x"
        called=[]
        def fake_req(url,body,**kw):
            called.append((url,body)); return type("R",(),{"ok":True,"status":200,"data":{"results":[]},"error":""})()
        globals()["_v1_exa_request"]=fake_req
        v1_exa_search_sector("Sport Discovery", date(2026,9,22), set(), num=6)
        payload=called[-1][1]
        ck(payload["query"] and payload["contents"] == {"highlights":True}, "search payload")
        ck("outputSchema" not in payload, "search has no outputSchema")
        ck(payload.get("moderation") is True, "search moderation")
        ck(payload["numResults"] == 6, "search numResults")
    finally:
        globals()["_v1_exa_request"] = old_req
        globals()["EXA_API_KEY"] = old_key
    called=[]; old_req=globals().get("_v1_exa_request")
    try:
        def fake_contents(url,body,**kw):
            called.append((url,body)); return type("R",(),{"ok":True,"status":200,"data":{"results":[],"statuses":[]},"error":""})()
        globals()["_v1_exa_request"]=fake_contents
        pages,status=v1_contents_for_urls(["https://example.com/a","https://example.com/a/"],text_mode=True)
        ck(pages == {}, "contents empty response")
        ck(status == "ok", "contents status")
        ck(len(called) == 1 and called[0][0].endswith("/contents"), "contents endpoint")
        ck(len(called[0][1]["urls"]) == 1 and called[0][1]["maxAgeHours"] == 720, "contents batching/freshness")
    finally:
        globals()["_v1_exa_request"] = old_req
    ck(len(AGENT_HARD_CASE_SCHEMA.get("properties",{})) == 5, "embedded agent schema")
    candidate={"sector":"Sport Origin","normalized_subject":"Football origins","central_knowledge_unit":"codified association football origins","central_claim":"Football laws were codified in 1863","sources":[{"name":"Source","url":"https://example.com"}]}
    ck(coverage_match(candidate,{"records":[]})[0] == "new", "new coverage candidate")
    story=v1_normalize_story({"image":None,"sources":None,"tags":None,"people":None})
    ck(story["image"] == {} and story["sources"] == [] and story["tags"] == [] and story["people"] == [] and "key_points" not in story, "null normalization")
    ck(set(EDITORIAL_SCHEMA["properties"]) == {"headline","body","hashtags"}, "compact editorial schema")
    ck(not (set(EDITORIAL_SCHEMA["properties"]) & {"deck","hook","key_points","why_it_matters","caption","angle","image_index","image_reason"}), "legacy editorial fields removed")
    ed={"headline":"How Football Laws Became Written Rules","body":"Football's laws became more standardized after clubs agreed on a written code. The change gave the sport a shared framework and helped distinguish association football from other traditions. That framework could travel between clubs and countries, creating a common reference for later rule development and making established practices easier to compare across communities.","hashtags":["#Football","#History"]}
    ok,why,_=v1_validate_editorial({"sector":"Sport Origin","sources":[{"name":"Source","url":"https://example.com","evidence_note":"laws were codified"}],"central_claim":"Football laws were codified","research_evidence":"laws were codified"},ed)
    ck(ok, "valid editorial accepted: "+why)
    rendered=render_evergreen_post({"headline":ed["headline"],"body":ed["body"],"sources":[("Source","https://example.com")],"tags":["#Football","#History","#Games","#Extra"],"image":{"url":"https://example.com/image.jpg"}})
    rendered_html=rendered["html"]
    ck(rendered["parse_mode"] == "HTML", "evergreen renderer declares HTML parse mode")
    ck(tg_sanitize(rendered_html) == rendered_html, "evergreen HTML is Telegram-safe")
    ck(rendered_html.splitlines()[-1] == "#Football #History #Games", "hashtags are final line and capped")
    ck("<a href=\"https://example.com\">Source</a>" in rendered_html and "https://example.com" not in re.sub(r'href=\"[^\"]+\"', '', rendered_html), "clickable source name without visible URL")
    ck("key_points" not in rendered_html and "why_it_matters" not in rendered_html, "legacy editorial blocks absent")
    ed_bad=dict(ed); ed_bad["body"]="This latest football story covers an upcoming match report and a current development. The update concerns the current season and a breaking change, so it belongs in a live news desk rather than evergreen research. The wording is intentionally current even though the underlying subject is a sport with longstanding historical context."
    ok,why,_=v1_validate_editorial({"sources":[{"name":"Source","url":"https://example.com"}]},ed_bad)
    ck((not ok) and why == "current_news_language", "current-news language blocked")
    a=make_event(sport="Football",league="UEFA Champions League",home="A",away="B",name="A vs B",start=datetime(2026,9,23,12,tzinfo=timezone.utc))
    b=make_event(sport="Football",league="UEFA Champions League",home="A",away="B",name="A-B",start=datetime(2026,9,23,12,15,tzinfo=timezone.utc))
    ck(live_event_identity(a) == live_event_identity(b), "live event identity collapse")
    normalized=v1_normalize_story({"headline":"Why This Sports Measurement Still Matters","body":"This is a verified evergreen story with enough detail to satisfy the editorial body length requirements in a real post.","sources":[("Source","https://example.com")],"tags":["#SportsFacts"],"image":{"url":"https://example.com/image.jpg"}})
    ck(normalized["image"] == {} and normalized["image_status"] == "card_only", "external images disabled for evergreen publishing")
    print(f"V1 architecture tests: {checks-len(failures)}/{checks} passed")
    for f in failures: print("  -",f)
    return 1 if failures else 0


def v1_self_test(fast: bool=False) -> int:
    return v1_architecture_tests()


def _run_diagnose() -> int:
    print(f"{APP_NAME} V1 diagnostic")
    failures=0
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM token: FAIL")
        failures += 1
    else:
        me=tg_call("getMe")
        print("TELEGRAM getMe:", "OK" if me.get("ok") else "FAIL")
        failures += not bool(me.get("ok"))
        chat=tg_call("getChat", {"chat_id": CHANNEL})
        print("TELEGRAM channel:", "OK" if chat.get("ok") else "FAIL")
        failures += not bool(chat.get("ok"))
    exa_ok=bool(EXA_API_KEY)
    print("EXA:", "OK" if exa_ok else "FAIL (missing key)")
    failures += not exa_ok
    ai=AIClient()
    print("CEREBRAS:", "OK" if ai.available else "FAIL (missing key)")
    failures += not ai.available
    try:
        coverage=load_coverage(load_state())
        print("COVERAGE INDEX:", f"OK ({len(coverage.get('records', []))} records)")
    except Exception as exc:
        print("COVERAGE INDEX: FAIL", redact(str(exc)))
        failures += 1
    print("RICH MESSAGE: enabled")
    return 1 if failures else 0


def v1_cerebras_preflight() -> tuple[bool, str]:
    if not CEREBRAS_API_KEY:
        return False, "CEREBRAS_API_KEY is empty"
    r=http("cerebras","GET",AIClient.MODELS_URL,headers={"Authorization":f"Bearer {CEREBRAS_API_KEY}","X-Cerebras-Version-Patch":CEREBRAS_API_VERSION},timeout=20,retries=1)
    if not r.ok or not isinstance(r.data,dict):
        return False, AIClient._error_detail(r)
    ids=[text(x.get("id")) for x in (r.data.get("data") or []) if isinstance(x,dict)]
    if CEREBRAS_MODEL not in ids:
        return False, f"configured model '{CEREBRAS_MODEL}' is not available"
    return True, f"model={CEREBRAS_MODEL}, api_version={CEREBRAS_API_VERSION}"


def v1_validate_config(require_secrets: bool=True) -> int:
    required=[
        "main.py","news_state.json","posted_urls.txt","coverage_index.json","requirements.txt","README.md",
        "prompts/exa_sector_discovery_v1.txt","prompts/exa_hard_case_agent_v1.txt","prompts/cerebras_rank_v1.txt",
        "prompts/cerebras_editorial_v1.txt","schemas/exa_agent_hard_case_v1.json","tests/test_v1.py",
        ".github/workflows/newbot.yml",".github/workflows/import-zip.yml",
    ]
    missing=[x for x in required if not Path(x).exists()]
    if missing:
        print("CONFIG: FAIL (missing: "+", ".join(missing)+")"); return 1
    if require_secrets:
        miss=[k for k,v in (("EXA_API_KEY",EXA_API_KEY),("CEREBRAS_API_KEY",CEREBRAS_API_KEY),("TELEGRAM_BOT_TOKEN",TELEGRAM_BOT_TOKEN)) if not v]
        if miss:
            print("CONFIG: FAIL (missing secrets: "+", ".join(miss)+")"); return 1
        ok,detail=v1_cerebras_preflight()
        if not ok:
            print("CONFIG: FAIL (Cerebras preflight: "+detail+")"); return 1
        print("CONFIG: Cerebras preflight OK ("+detail+")")
    print(f"CONFIG: OK (The Sports Newsroom V1, {len(V1_SECTORS)} sectors, single Search/Contents/Agent/Cerebras pipeline)")
    return 0


def v1_main(argv: list[str] | None=None) -> int:
    global CHANNEL, DRY_RUN
    ap=argparse.ArgumentParser(description=f"{APP_NAME} V1 {APP_VERSION}")
    ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--fast",action="store_true")
    ap.add_argument("--validate-config",action="store_true")
    ap.add_argument("--diagnose",action="store_true")
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--channel",default=os.environ.get("CHANNEL_OVERRIDE",""))
    ap.add_argument("--version",action="store_true")
    args=ap.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} V1 {APP_VERSION}"); return 0
    if args.channel.strip(): CHANNEL=args.channel.strip()
    if args.self_test: return v1_self_test(args.fast)
    if args.validate_config: return v1_validate_config(require_secrets=True)
    if args.diagnose:
        return run_once(mode="diagnose")
    if args.dry_run:
        DRY_RUN=True
        try: return run_once(mode="dry-run")
        finally: DRY_RUN=False
    DRY_RUN=False
    return run_once()


# One canonical operational entrypoint. Diagnostics and dry-runs are modes of the same engine.
run_once = v1_run_once
run_diagnose = run_once
self_test = v1_self_test
validate_config = v1_validate_config
main = v1_main

if __name__ == "__main__":
    sys.exit(main())
