# Sports & Games Discovery Bot
# Single-file production build using the same core stack as the previous bot:
# Exa + Cerebras + requests/feedparser/BeautifulSoup/trafilatura/Pillow + GitHub Actions.
#
# Design principle:
#   discover -> verify -> understand -> dedupe by underlying claim/event -> editorially select -> publish
#
# The project deliberately excludes video games, esports, gaming hardware and gaming-industry news.

from __future__ import annotations

import argparse
import html
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

try:
    import feedparser
except ImportError:
    feedparser = None
try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    requests = None
    HTTPAdapter = None
try:
    import trafilatura
except ImportError:
    trafilatura = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    Image = ImageDraw = ImageFont = ImageOps = None
try:
    from urllib3.util.retry import Retry
except ImportError:
    Retry = None
try:
    from exa_py import Exa
except ImportError:
    Exa = None
try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:
    Cerebras = None


APP_NAME = "Sports & Games Discovery Bot"
APP_VERSION = "1.0.0"
STATE_FILE = "knowledge_state.json"
POSTED_FILE = "published_urls.txt"
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@SportsGamesHub").strip()
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
BD_TZ = ZoneInfo("Asia/Dhaka")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

MAX_RICH_CHARACTERS = 32768
MAX_DISCOVERY_POSTS_PER_DAY = int(os.environ.get("MAX_DISCOVERY_POSTS_PER_DAY", "5"))
MAX_DISCOVERY_CANDIDATES = int(os.environ.get("MAX_DISCOVERY_CANDIDATES", "60"))
MAX_HISTORY_CANDIDATES = int(os.environ.get("MAX_HISTORY_CANDIDATES", "30"))
MAX_SPORT_EVENT_CANDIDATES = int(os.environ.get("MAX_SPORT_EVENT_CANDIDATES", "70"))
POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "3.0"))
DISCOVERY_COOLDOWN_HOURS = float(os.environ.get("DISCOVERY_COOLDOWN_HOURS", "3.5"))
STATE_RETENTION_DAYS = int(os.environ.get("STATE_RETENTION_DAYS", "120"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))

logger = logging.getLogger("sports-games-hub")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref",
}

VIDEO_GAME_TERMS = {
    "video game", "video games", "videogame", "gaming", "esports", "e-sports",
    "playstation", "xbox", "nintendo switch", "steam", "epic games store",
    "dlc", "patch notes", "game patch", "console", "ps5", "ps4", "xbox series",
    "pc gaming", "mobile gaming", "game trailer", "game studio", "publisher", "gpu",
}
VIDEO_GAME_QUERY_TERMS = re.compile(
    r"\b(playstation|xbox|steam|epic games|console|ps5|ps4|xbox series|dlc|patch notes|esports|e-sports|video game|videogame|pc gaming|mobile gaming)\b",
    re.I,
)

SURPRISE_ANGLES = [
    "why is it called",
    "origin of",
    "oldest known",
    "first ever",
    "only time ever",
    "never been broken",
    "invented by accident",
    "originally called",
    "originally meant",
    "rule most people get wrong",
    "official rule",
    "house rule",
    "myth about",
    "banned in",
    "strange rule",
    "unusual tradition",
    "forgotten history",
    "why does it use",
    "history of",
    "where did it come from",
    "what changed",
    "then vs now",
    "first to",
    "only to",
]

CATEGORIES = [
    "evergreen_fact",
    "game_discovery",
    "rule_check",
    "how_to_play",
    "game_history",
    "on_this_date",
    "century_ago",
    "why_explained",
    "first_last_only",
    "then_vs_now",
    "forgotten_game",
    "forgotten_sport",
    "sport_discovery",
    "new_board_game",
    "new_card_game",
    "new_tabletop_game",
    "new_sport",
    "sports_daily_next",
    "sports_daily_past",
]

ANGLES = [
    "origin", "etymology", "rule", "myth", "number", "record", "accident", "banned",
    "weird", "design", "symbol", "tradition", "equipment", "measurement", "terminology",
    "scoring", "strategy", "geography", "culture", "evolution", "first", "last", "only",
    "rare", "historical_connection", "new_mechanic", "rules_change", "discovery", "timeline",
]

TOP_LEVEL_SPORTS = [
    "football", "cricket", "basketball", "tennis", "badminton", "table tennis", "volleyball",
    "baseball", "rugby", "golf", "boxing", "mma", "formula 1", "athletics", "swimming",
    "cycling", "gymnastics", "wrestling", "fencing", "archery", "shooting", "rowing",
    "canoeing", "sailing", "surfing", "skateboarding", "sport climbing", "water polo",
    "hockey", "field hockey", "ice hockey", "snooker", "billiards", "darts", "squash",
    "bowling", "handball", "netball", "kabaddi", "kho kho", "sepak takraw", "sumo",
    "judo", "karate", "taekwondo", "weightlifting", "triathlon", "modern pentathlon",
    "equestrian", "curling", "biathlon", "bobsleigh", "luge", "skeleton",
]

GAME_CATEGORIES = {
    "board": [
        "chess", "go", "shogi", "backgammon", "monopoly", "catan", "carrom", "mahjong",
        "scrabble", "risk", "ticket to ride", "snakes and ladders", "pachisi", "mancala",
    ],
    "card": [
        "uno", "rummy", "hearts", "spades", "bridge", "cribbage", "exploding kittens",
        "phase 10", "skip-bo", "traditional playing cards",
    ],
    "party": ["mafia", "werewolf", "codenames", "pictionary", "charades", "just one"],
    "traditional": ["ludo", "pachisi", "carrom", "kabaddi", "kho kho", "sepak takraw", "mancala"],
    "mind": ["chess", "go", "shogi", "xiangqi", "sudoku", "rubik's cube", "nonogram"],
}

OFFICIAL_DOMAINS = [
    "fifa.com", "uefa.com", "icc-cricket.com", "worldathletics.org", "fide.com", "itftennis.com",
    "worldbadminton.com", "badmintonworld.tv", "formula1.com", "fia.com", "world.rugby",
    "olympics.com", "paralympic.org", "worldboxing.org", "ijf.org", "worldarchery.sport",
    "worldrowing.com", "worldaquatics.com", "uci.org", "fiba.basketball",
]

SECONDARY_DOMAINS = [
    "bbc.com", "espn.com", "skysports.com", "theguardian.com", "reuters.com", "apnews.com",
    "nbcsports.com", "cbssports.com", "foxsports.com", "si.com",
    "boardgamegeek.com", "dicebreaker.com", "tabletopgaming.co.uk",
]

REFERENCE_DOMAINS = [
    "wikipedia.org", "britannica.com", "guinnessworldrecords.com", "atlasobscura.com",
]

LEAD_ONLY_DOMAINS = ["reddit.com", "quora.com"]

RSS_FEEDS = [
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPN", "url": "https://www.espn.com/espn/rss/news"},
    {"name": "Guardian Sport", "url": "https://www.theguardian.com/uk/sport/rss"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
]

HEADERS = {
    "User-Agent": "SportsGamesDiscoveryBot/1.0 (+https://github.com/)",
    "Accept-Language": "en-US,en;q=0.8",
}

if requests is not None:
    session = requests.Session()
    if Retry is not None and HTTPAdapter is not None:
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
else:
    session = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def normalize_text(v: Any) -> str:
    s = text(v).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(v: Any) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "from", "with", "by",
        "at", "as", "is", "are", "was", "were", "be", "been", "this", "that", "it", "its",
        "new", "game", "games", "sport", "sports", "news", "why", "how", "what", "did", "does",
        "do", "has", "have", "had", "about", "after", "before", "into", "than", "over",
    }
    return {x for x in normalize_text(v).split() if len(x) >= 3 and x not in stop}


def similarity(a: Any, b: Any) -> float:
    aa, bb = normalize_text(a), normalize_text(b)
    if not aa or not bb:
        return 0.0
    seq = SequenceMatcher(None, aa, bb).ratio()
    ta, tb = tokens(aa), tokens(bb)
    jac = len(ta & tb) / max(1, len(ta | tb))
    return 0.55 * seq + 0.45 * jac


def canonical_url(url: str) -> str:
    raw = text(url)
    if not raw:
        return ""
    parts = urlsplit(raw)
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return host + path + (("?" + urlencode(query)) if query else "")


def now_bd() -> datetime:
    return datetime.now(BD_TZ)


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = text(value)
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


def is_video_game_contaminated(value: str) -> bool:
    s = text(value).lower()
    return bool(VIDEO_GAME_QUERY_TERMS.search(s))


def domain_of(url: str) -> str:
    raw = text(url).lower()
    if "://" not in raw:
        raw = "https://" + raw
    return urlsplit(raw).netloc.removeprefix("www.").split(":")[0]


def source_tier(url: str, source_name: str = "") -> int:
    d = domain_of(url)
    if any(d == x or d.endswith("." + x) for x in OFFICIAL_DOMAINS):
        return 1
    if any(d == x or d.endswith("." + x) for x in SECONDARY_DOMAINS):
        return 2
    if any(d == x or d.endswith("." + x) for x in REFERENCE_DOMAINS):
        return 3
    if any(d == x or d.endswith("." + x) for x in LEAD_ONLY_DOMAINS):
        return 4
    if source_name.lower() in {"wikipedia", "britannica", "guinness world records"}:
        return 3
    return 3


def source_label(url: str, fallback: str = "Source") -> str:
    d = domain_of(url)
    labels = {
        "bbc.com": "BBC Sport",
        "espn.com": "ESPN",
        "skysports.com": "Sky Sports",
        "theguardian.com": "The Guardian",
        "olympics.com": "Olympics.com",
        "reuters.com": "Reuters",
        "apnews.com": "AP",
        "fifa.com": "FIFA",
        "uefa.com": "UEFA",
        "icc-cricket.com": "ICC",
        "worldathletics.org": "World Athletics",
        "fide.com": "FIDE",
        "itftennis.com": "ITF",
        "formula1.com": "Formula 1",
        "fia.com": "FIA",
        "boardgamegeek.com": "BoardGameGeek",
        "britannica.com": "Britannica",
        "atlasobscura.com": "Atlas Obscura",
        "guinnessworldrecords.com": "Guinness World Records",
        "wikipedia.org": "Wikipedia",
    }
    return labels.get(d, fallback or d or "Source")


def safe_json_loads(raw: str) -> Any:
    s = text(raw)
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    return json.loads(s)


def strip_tags(value: str) -> str:
    if BeautifulSoup is None:
        return re.sub(r"<[^>]+>", " ", text(value)).strip()
    return BeautifulSoup(text(value), "html.parser").get_text(" ", strip=True)


def sentence_count(value: str) -> int:
    s = text(value)
    if not s:
        return 0
    return len(re.findall(r"(?<=[.!?])\s+", s)) + (1 if s[-1:] in ".!?" else 0)


def complete_sentence(value: str) -> bool:
    s = text(value)
    return bool(s and s[-1] in ".!?")


def clamp(s: str, n: int) -> str:
    s = text(s)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def default_state() -> dict:
    return {
        "schema_version": 1,
        "created_at": iso(now_bd()),
        "last_run_at": "",
        "queue": {},
        "posts": [],
        "claims": {},
        "entities": {},
        "historical_events": {},
        "daily_flags": {},
        "angle_history": [],
        "category_history": [],
        "source_health": {},
    }


def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists() or p.stat().st_size == 0:
        return default_state()
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        merged = default_state()
        merged.update(state)
        return merged
    except Exception as exc:
        logger.warning("State load failed, starting fresh: %s", exc)
        return default_state()


def save_state(state: dict) -> None:
    tmp = Path(STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_published_urls() -> set[str]:
    p = Path(POSTED_FILE)
    if not p.exists():
        return set()
    return {x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}


def append_published_url(url: str) -> None:
    canonical = canonical_url(url)
    if not canonical:
        return
    with Path(POSTED_FILE).open("a", encoding="utf-8") as f:
        f.write(canonical + "\n")


def prune_state(state: dict) -> None:
    cutoff = now_bd() - timedelta(days=STATE_RETENTION_DAYS)
    # Queue is temporary; knowledge records are permanent.
    for key, item in list(state.get("queue", {}).items()):
        seen = parse_dt(item.get("first_seen_at")) or parse_dt(item.get("published_date"))
        if seen and seen < cutoff:
            state["queue"].pop(key, None)
    state["posts"] = state.get("posts", [])[-5000:]
    state["angle_history"] = state.get("angle_history", [])[-1000:]
    state["category_history"] = state.get("category_history", [])[-1000:]
    for key, value in list(state.get("source_health", {}).items()):
        last = parse_dt(value.get("updated_at")) if isinstance(value, dict) else None
        if last and last < cutoff:
            state["source_health"].pop(key, None)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def require_credentials() -> None:
    missing = [name for name, value in [
        ("EXA_API_KEY", EXA_API_KEY),
        ("CEREBRAS_API_KEY", CEREBRAS_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ] if not value]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


class Clients:
    def __init__(self):
        require_credentials()
        if Exa is None or Cerebras is None:
            raise RuntimeError("Required API SDKs are not installed. Run: pip install -r requirements.txt")
        self.exa = Exa(api_key=EXA_API_KEY)
        self.cerebras = Cerebras(api_key=CEREBRAS_API_KEY)

    def ai(self, *, system: str, user: str, schema_name: str, schema: dict, max_tokens: int = 1800, temperature: float = 0.2) -> dict:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self.cerebras.chat.completions.create(
                    model=CEREBRAS_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                    reasoning_effort="low",
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                )
                content = response.choices[0].message.content
                data = safe_json_loads(content)
                if not isinstance(data, dict):
                    raise ValueError("AI response is not an object")
                return data
            except Exception as exc:
                last_exc = exc
                logger.warning("Cerebras attempt %d failed: %s", attempt + 1, exc)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Cerebras failed after 3 attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "kind": {"type": "string", "enum": ["fact", "game", "rule", "history", "sport_event", "new_game", "howto"]},
                    "category": {"type": "string"},
                    "angle": {"type": "string"},
                    "game_or_sport": {"type": "string"},
                    "subject": {"type": "string"},
                    "claim_or_event": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "why_interesting": {"type": "string"},
                },
                "required": ["id", "kind", "category", "angle", "game_or_sport", "subject", "claim_or_event", "source_urls", "why_interesting"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["verified", "disputed", "unverified", "reject"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "supported_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "source_roles": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "reason": {"type": "string"},
    },
    "required": ["status", "confidence", "supported_claims", "unsupported_claims", "source_roles", "reason"],
    "additionalProperties": False,
}

EDITORIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 12},
        "reason_by_id": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["selected_ids", "reason_by_id"],
    "additionalProperties": False,
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": [
            "fact", "game_discovery", "rule_check", "how_to_play", "history", "on_this_date",
            "century_ago", "why", "first_last_only", "then_vs_now", "forgotten", "daily_next", "daily_past"
        ]},
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "body": {"type": "string"},
        "why_interesting": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "date_anchor": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "required": ["format", "headline", "dek", "body", "why_interesting", "key_points", "date_anchor", "sources", "tags"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def exa_search(client: Clients, query: str, *, start: datetime | None = None, end: datetime | None = None, domains: list[str] | None = None, num: int = 8, contents: bool = True) -> list[dict]:
    kwargs: dict[str, Any] = {
        "type": "auto",
        "num_results": num,
    }
    if domains:
        kwargs["include_domains"] = domains
    if start:
        kwargs["start_published_date"] = start.astimezone(timezone.utc).isoformat()
    if end:
        kwargs["end_published_date"] = end.astimezone(timezone.utc).isoformat()
    if contents:
        kwargs["contents"] = {"highlights": {"max_characters": 1200}}
    try:
        results = client.exa.search_and_contents(query, **kwargs)
        out = []
        for r in getattr(results, "results", []):
            url = text(getattr(r, "url", ""))
            title = text(getattr(r, "title", ""))
            if not url or not title:
                continue
            published = parse_dt(getattr(r, "published_date", ""))
            highlights = getattr(r, "highlights", None) or []
            out.append({
                "url": url,
                "canonical": canonical_url(url),
                "title": title,
                "published_date": iso(published),
                "source": source_label(url),
                "excerpt": " ".join(text(x) for x in highlights)[:2500],
                "discovery": "exa",
            })
        return out
    except Exception as exc:
        logger.warning("Exa search failed for %s: %s", query, exc)
        return []


def rss_discovery() -> list[dict]:
    found = []
    if feedparser is None:
        logger.warning("feedparser is not installed; RSS discovery skipped")
        return []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            healthy = bool(parsed.entries)
            for entry in parsed.entries[:25]:
                url = text(entry.get("link"))
                title = text(entry.get("title"))
                if not url or not title:
                    continue
                dt = feed_entry_dt(entry)
                found.append({
                    "url": url,
                    "canonical": canonical_url(url),
                    "title": title,
                    "published_date": iso(dt),
                    "source": feed["name"],
                    "excerpt": strip_tags(text(entry.get("summary")))[:1800],
                    "discovery": "rss",
                })
            logger.info("RSS %s: %s", feed["name"], "ok" if healthy else "empty")
        except Exception as exc:
            logger.warning("RSS failed %s: %s", feed["name"], exc)
    return found


def feed_entry_dt(entry: Any) -> datetime | None:
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def date_window_for_next_day() -> tuple[datetime, datetime]:
    target = now_bd().date() + timedelta(days=1)
    start = datetime.combine(target, datetime.min.time(), tzinfo=BD_TZ)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


def date_window_for_previous_day() -> tuple[datetime, datetime]:
    target = now_bd().date() - timedelta(days=1)
    start = datetime.combine(target, datetime.min.time(), tzinfo=BD_TZ)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


def build_discovery_queries() -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for sport in TOP_LEVEL_SPORTS:
        queries.append(("sports", f"interesting {sport} history rule record origin unusual fact"))
    for category, games in GAME_CATEGORIES.items():
        for game in games:
            queries.append(("games", f"{game} rules origin history unusual fact official rule"))
            if category in {"board", "card", "party"}:
                queries.append(("new_games", f"new {category} game 2026 tabletop release announcement review"))
    for angle in SURPRISE_ANGLES:
        queries.append(("surprise", f"{angle} sport game physical board card traditional"))
    return queries


def search_daily_sports(client: Clients, which: str) -> list[dict]:
    if which == "next":
        start, end = date_window_for_next_day()
        label = start.strftime("%d %B %Y")
        query_templates = [
            f"sports matches fixtures {label} football cricket tennis badminton basketball formula 1",
            f"sport events {label} tournaments finals races championships",
            f"{label} cricket schedule fixtures matches",
            f"{label} football fixtures matches schedule",
            f"{label} tennis tournament schedule matches",
            f"{label} badminton tournament matches schedule",
            f"{label} basketball games schedule",
            f"{label} motorsport race schedule formula 1",
            f"{label} athletics swimming cycling sports events",
        ]
    else:
        start, end = date_window_for_previous_day()
        label = start.strftime("%d %B %Y")
        query_templates = [
            f"sports results {label} football cricket tennis badminton basketball formula 1",
            f"major sports results {label} championships finals records upsets",
            f"{label} cricket results scorecards",
            f"{label} football results fixtures completed",
            f"{label} tennis results finals",
            f"{label} badminton results finals",
            f"{label} basketball results",
            f"{label} motorsport results",
            f"{label} athletics records results",
        ]
    out = []
    for q in query_templates:
        out.extend(exa_search(client, q, start=start, end=end, num=8))
    return normalize_candidates(out, current_only=False)


def search_history(client: Clients, target: date) -> list[dict]:
    label = target.strftime("%d %B")
    years = [target.year - 25, target.year - 50, target.year - 75, target.year - 100, target.year - 125]
    out = []
    for year in years:
        ydate = target.replace(year=year) if not (target.month == 2 and target.day == 29) else target.replace(year=year, day=28)
        q = f"sports games history {ydate.strftime('%d %B %Y')} on this date record championship first last unusual"
        out.extend(exa_search(client, q, start=datetime(year, target.month, 1, tzinfo=timezone.utc), end=datetime(year, target.month, 28 if target.month == 2 else 31, 23, 59, tzinfo=timezone.utc), num=8))
    out.extend(exa_search(client, f"\"{label}\" sports history on this day Olympics games rules", num=10))
    return normalize_candidates(out, current_only=False)


def search_evergreen(client: Clients) -> list[dict]:
    out = []
    queries = build_discovery_queries()
    # Rotate deterministically by calendar day so the same query set is not hammered every run.
    day_index = now_bd().timetuple().tm_yday
    stride = max(1, len(queries) // 24)
    start_index = (day_index * stride) % max(1, len(queries))
    selected = queries[start_index:start_index + 10]
    if len(selected) < 10:
        selected += queries[:10 - len(selected)]
    for bucket, q in selected:
        out.extend(exa_search(client, q, num=5))
    return normalize_candidates(out, current_only=False)


def normalize_candidates(items: Iterable[dict], current_only: bool = False) -> list[dict]:
    now = now_bd()
    result = []
    seen = set()
    for item in items:
        url = text(item.get("url"))
        title = text(item.get("title"))
        if not url or not title:
            continue
        canon = canonical_url(url)
        if not canon or canon in seen:
            continue
        # Hard exclusion at ingestion.
        combined = f"{title} {item.get('excerpt', '')} {url}"
        if is_video_game_contaminated(combined):
            continue
        dt = parse_dt(item.get("published_date"))
        if current_only and dt and dt < now - timedelta(days=4):
            continue
        seen.add(canon)
        result.append({
            "url": url,
            "canonical": canon,
            "title": re.sub(r"\s+", " ", title).strip(),
            "published_date": iso(dt),
            "source": text(item.get("source")) or source_label(url),
            "excerpt": re.sub(r"\s+", " ", text(item.get("excerpt"))).strip(),
            "discovery": text(item.get("discovery")) or "exa",
            "source_tier": source_tier(url, text(item.get("source"))),
        })
    return result


# ---------------------------------------------------------------------------
# Article extraction
# ---------------------------------------------------------------------------


def fetch_article(item: dict) -> dict:
    url = item["url"]
    if session is None:
        return {"text": text(item.get("excerpt")), "image_url": "", "canonical": canonical_url(url)}
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        raw_html = r.text
        text_content = trafilatura.extract(raw_html, include_comments=False, include_tables=True) or ""
        soup = BeautifulSoup(raw_html, "html.parser")
        image_url = ""
        for selector in [
            ("meta", {"property": "og:image"}),
            ("meta", {"name": "twitter:image"}),
        ]:
            tag = soup.find(*selector)
            if tag and tag.get("content"):
                image_url = urljoin(url, tag["content"])
                break
        if not text_content:
            text_content = strip_tags(raw_html)
        return {
            "text": text_content[:18000],
            "image_url": image_url,
            "canonical": canonical_url(r.url or url),
        }
    except Exception as exc:
        logger.warning("Article extraction failed %s: %s", url, exc)
        return {"text": text(item.get("excerpt")), "image_url": "", "canonical": canonical_url(url)}


# ---------------------------------------------------------------------------
# Candidate classification + verification
# ---------------------------------------------------------------------------


def classify_candidates(client: Clients, candidates: list[dict], *, mode: str) -> list[dict]:
    if not candidates:
        return []
    blocks = []
    for idx, c in enumerate(candidates[:50], start=1):
        blocks.append(
            f"ID: {idx}\nTITLE: {c['title']}\nSOURCE: {c['source']}\nURL: {c['url']}\nEXCERPT: {c['excerpt'][:1000]}"
        )
    system = f"""
You are the discovery editor for a factual Sports & Games knowledge channel.
Mode: {mode}.
Exclude every form of video gaming and esports. A result about a physical tabletop/card/board game is allowed; a result about a video game is not.
Classify candidates by what they can legitimately support. Do not invent claims. Prefer concrete subjects and named sources.
For new-game discovery, treat publication/news about a physical board/card/party/tabletop game as eligible. For evergreen fact/history, identify the central factual claim that could remain useful for years.
Return only the JSON schema.
""".strip()
    user = "\n\n".join(blocks)
    data = client.ai(system=system, user=user, schema_name="sports_games_candidates_v1", schema=CANDIDATE_SCHEMA, max_tokens=2400)
    by_id = {i + 1: c for i, c in enumerate(candidates[:50])}
    out = []
    for row in data.get("items", []):
        c = by_id.get(int(row.get("id", 0)))
        if not c:
            continue
        if is_video_game_contaminated(str(row)):
            continue
        merged = dict(c)
        merged.update({k: row.get(k) for k in row if k != "id"})
        merged["candidate_id"] = int(row["id"])
        out.append(merged)
    return out


def verify_candidate(client: Clients, candidate: dict) -> dict:
    # One source is not enough unless it is primary/official.
    urls = list(dict.fromkeys(text(x) for x in candidate.get("source_urls", []) if text(x)))
    needs_corroboration = not any(source_tier(u) == 1 for u in urls) or len({domain_of(u) for u in urls}) < 2
    if needs_corroboration:
        corroboration_query = f"{candidate.get('subject', '')} {candidate.get('claim_or_event', '')} official history rules facts".strip()
        extra = exa_search(client, corroboration_query, num=6)
        for item in extra:
            u = text(item.get("url"))
            if not u or domain_of(u) in {domain_of(x) for x in urls}:
                continue
            if source_tier(u) == 4 or is_video_game_contaminated(f"{item.get('title', '')} {item.get('excerpt', '')}"):
                continue
            urls.append(u)
            if len(urls) >= 4:
                break
        candidate["source_urls"] = urls[:5]

    source_lines = []
    for url in candidate.get("source_urls", [])[:5]:
        source_lines.append(f"SOURCE URL: {url}")
    source_lines.append(f"DISCOVERY TITLE: {candidate.get('title', '')}")
    source_lines.append(f"DISCOVERY EXCERPT: {candidate.get('excerpt', '')}")
    source_lines.append(f"CANDIDATE CLAIM/EVENT: {candidate.get('claim_or_event', '')}")
    evidence = []
    for url in candidate.get("source_urls", [])[:3]:
        item = fetch_article({"url": url, "excerpt": ""})
        if item["text"]:
            evidence.append(f"URL: {url}\nTEXT:\n{item['text'][:9000]}")
    if not evidence:
        evidence.append("No source text could be extracted.")
    system = """
You are a strict fact verifier for a Sports & Games knowledge archive.
Rules:
1. A primary/official source can verify a claim on its own.
2. Without a primary source, require two genuinely independent credible sources.
3. Reddit, Quora, blogs and forums are lead-only. They cannot verify a claim by themselves.
4. Every date, number, name, origin, rule, record and historical assertion must be supported by the supplied evidence.
5. If the evidence conflicts, status must be disputed.
6. If evidence is missing or too weak, status must be unverified.
7. Never fill a missing detail from memory.
Return only the JSON schema.
""".strip()
    user = "\n\n".join(source_lines + evidence)
    return client.ai(system=system, user=user, schema_name="sports_games_verification_v1", schema=VERIFY_SCHEMA, max_tokens=1800)


def independent_source_count(candidate: dict) -> int:
    urls = list(dict.fromkeys(text(x) for x in candidate.get("source_urls", []) if text(x)))
    return len({domain_of(u) for u in urls})


def verify_hard_gates(candidate: dict, verification: dict) -> tuple[bool, str]:
    status = text(verification.get("status"))
    if status not in {"verified", "disputed"}:
        return False, "not_verified"
    if status == "disputed" and candidate.get("kind") not in {"history", "fact", "rule"}:
        return False, "disputed_current_item"
    confidence = int(verification.get("confidence", 0) or 0)
    sources = independent_source_count(candidate)
    has_tier1 = any(source_tier(u) == 1 for u in candidate.get("source_urls", []))
    if not has_tier1 and sources < 2:
        return False, "insufficient_independent_sources"
    if confidence < 75:
        return False, "low_confidence"
    if candidate.get("angle") in {"origin", "first", "last", "only", "record", "etymology"} and not candidate.get("source_urls"):
        return False, "missing_sources"
    return True, "ok"


# ---------------------------------------------------------------------------
# Novelty / scoring / editorial selection
# ---------------------------------------------------------------------------


def normalized_claim_key(subject: str, claim: str, angle: str) -> str:
    return hashlib.sha256(f"{normalize_text(subject)}|{normalize_text(claim)}|{normalize_text(angle)}".encode()).hexdigest()[:24]


def semantic_claim_duplicate(state: dict, candidate: dict) -> bool:
    subject = text(candidate.get("subject")) or text(candidate.get("game_or_sport"))
    claim = text(candidate.get("claim_or_event"))
    angle = text(candidate.get("angle"))
    if not subject or not claim:
        return False
    key = normalized_claim_key(subject, claim, angle)
    if key in state.get("claims", {}):
        return True
    for old in state.get("claims", {}).values():
        if normalize_text(old.get("subject")) == normalize_text(subject):
            same_claim = similarity(old.get("claim"), claim) >= 0.78
            same_angle = normalize_text(old.get("angle")) == normalize_text(angle)
            if same_claim and (same_angle or similarity(old.get("claim"), claim) >= 0.90):
                return True
    for post in state.get("posts", [])[-1000:]:
        if normalize_text(post.get("subject")) == normalize_text(subject) and similarity(post.get("claim"), claim) >= 0.82:
            return True
    return False


def diversity_penalty(state: dict, category: str, angle: str, subject: str) -> int:
    penalty = 0
    recent_categories = [x.get("category") for x in state.get("category_history", [])[-6:]]
    recent_angles = [x.get("angle") for x in state.get("angle_history", [])[-8:]]
    if recent_categories and recent_categories[-1] == category:
        penalty += 5
    if recent_angles[-1:] == [angle]:
        penalty += 4
    if recent_angles.count(angle) >= 3:
        penalty += 7
    subject_recent = [x.get("subject") for x in state.get("posts", [])[-20:]]
    if subject and subject in subject_recent:
        penalty += 6
    return penalty


def game_month_cap_hit(state: dict, game_or_sport: str) -> bool:
    key = normalize_text(game_or_sport)
    if not key:
        return False
    cutoff = now_bd() - timedelta(days=30)
    count = 0
    for post in state.get("posts", []):
        dt = parse_dt(post.get("published_at"))
        if dt and dt >= cutoff and normalize_text(post.get("game_or_sport")) == key:
            count += 1
    return count >= 4


def candidate_score(candidate: dict, verification: dict, state: dict) -> tuple[int, dict]:
    surprise = min(5, 2 + int(bool(candidate.get("why_interesting"))))
    novelty = 5 if not semantic_claim_duplicate(state, candidate) else 0
    evergreen = 5 if candidate.get("kind") in {"fact", "game", "rule", "history", "howto", "new_game"} else 3
    verification_score = 5 if verification.get("status") == "verified" and int(verification.get("confidence", 0)) >= 90 else 4
    simplicity = 5 if len(text(candidate.get("claim_or_event"))) <= 180 else 4
    curiosity = 4 if candidate.get("angle") in {"origin", "why", "weird", "myth", "only", "first", "rare", "record"} else 3
    score = surprise + novelty + evergreen + verification_score + simplicity + curiosity
    score -= diversity_penalty(state, text(candidate.get("category")), text(candidate.get("angle")), text(candidate.get("subject")))
    if game_month_cap_hit(state, text(candidate.get("game_or_sport"))):
        score -= 7
    return score, {
        "surprise": surprise,
        "novelty": novelty,
        "evergreen": evergreen,
        "verification": verification_score,
        "simplicity": simplicity,
        "curiosity": curiosity,
        "penalty": diversity_penalty(state, text(candidate.get("category")), text(candidate.get("angle")), text(candidate.get("subject"))),
    }


def editorial_select(client: Clients, candidates: list[dict], state: dict, max_items: int = 6) -> list[dict]:
    if not candidates:
        return []
    prepared = []
    for c in candidates:
        if semantic_claim_duplicate(state, c):
            continue
        score, breakdown = candidate_score(c, c.get("verification", {}), state)
        if score < 15:
            continue
        prepared.append({**c, "editor_score": score, "score_breakdown": breakdown})
    prepared.sort(key=lambda x: (x.get("editor_score", 0), x.get("verification", {}).get("confidence", 0)), reverse=True)
    shortlist = prepared[:20]
    if not shortlist:
        return []
    blocks = []
    for i, c in enumerate(shortlist, 1):
        blocks.append(
            f"ID: {i}\nCATEGORY: {c.get('category')}\nANGLE: {c.get('angle')}\nSUBJECT: {c.get('subject')}\nCLAIM/EVENT: {c.get('claim_or_event')}\nWHY: {c.get('why_interesting')}\nSCORE: {c.get('editor_score')}\nVERIFICATION: {c.get('verification', {}).get('confidence')}"
        )
    system = """
You are the final editorial selector for a sports and physical-games channel.
Select only candidates that are interesting, factually verified and worth sharing.
Prefer diversity across sports/games, categories and angles. Do not select multiple items that are effectively the same fact.
Never select anything that mentions or concerns video games or esports.
Do not optimize for maximum quantity. Quality is the gate.
Return only JSON.
""".strip()
    try:
        data = client.ai(system=system, user="\n\n".join(blocks), schema_name="sports_games_editorial_v1", schema=EDITORIAL_SCHEMA, max_tokens=1600)
        selected_ids = [int(x) for x in data.get("selected_ids", [])]
    except Exception as exc:
        logger.warning("Editorial AI unavailable, using deterministic selection: %s", exc)
        selected_ids = list(range(1, min(max_items, len(shortlist)) + 1))
    selected = []
    used_cat, used_angle, used_subject = set(), set(), set()
    for sid in selected_ids:
        if len(selected) >= max_items:
            break
        if not 1 <= sid <= len(shortlist):
            continue
        c = shortlist[sid - 1]
        cat, angle, subject = text(c.get("category")), text(c.get("angle")), normalize_text(c.get("subject"))
        if cat in used_cat and angle in used_angle:
            continue
        if subject in used_subject:
            continue
        if game_month_cap_hit(state, text(c.get("game_or_sport"))):
            continue
        used_cat.add(cat)
        used_angle.add(angle)
        used_subject.add(subject)
        selected.append(c)
    return selected


# ---------------------------------------------------------------------------
# Post generation / grounding
# ---------------------------------------------------------------------------


def post_format_for(candidate: dict) -> str:
    kind = text(candidate.get("kind"))
    cat = text(candidate.get("category"))
    mapping = {
        "game_discovery": "game_discovery",
        "new_game": "game_discovery",
        "rule_check": "rule_check",
        "how_to_play": "how_to_play",
        "on_this_date": "on_this_date",
        "history": "history",
        "century_ago": "century_ago",
        "why_explained": "why",
        "first_last_only": "first_last_only",
        "then_vs_now": "then_vs_now",
        "forgotten_game": "forgotten",
        "forgotten_sport": "forgotten",
        "sport_discovery": "game_discovery",
        "sports_daily_next": "daily_next",
        "sports_daily_past": "daily_past",
    }
    if cat in mapping:
        return mapping[cat]
    return "fact" if kind == "fact" else "history" if kind == "history" else "fact"


def generate_post(client: Clients, candidate: dict) -> dict | None:
    article = fetch_article(candidate)
    source_text = article["text"]
    if not source_text:
        return None
    fmt = post_format_for(candidate)
    system = f"""
You are the senior editor for a factual Sports & Games Telegram channel.
Create one durable post in the format: {fmt}.
The channel covers real-world sports and physical games only. Never write about video games, esports, consoles, Steam, DLC, patches or gaming hardware.
Use only facts supported by the provided evidence. Do not add details from memory. Preserve exact dates/numbers/names only when supported.
The post should remain understandable years later. Avoid disposable wording such as latest, today, yesterday, tomorrow, tonight, just now unless it is part of a dated historical phrase. Prefer exact dates.
Curiosity is allowed; clickbait is not. The question/headline should be accurate and not exaggerate.
Rewrite everything in original wording. No copied sentences.
For disputed historical claims, clearly label the uncertainty instead of presenting one version as settled.
Return only JSON.
""".strip()
    user = f"""
CANDIDATE CATEGORY: {candidate.get('category')}
ANGLE: {candidate.get('angle')}
GAME/SPORT: {candidate.get('game_or_sport')}
SUBJECT: {candidate.get('subject')}
CLAIM/EVENT: {candidate.get('claim_or_event')}
WHY INTERESTING: {candidate.get('why_interesting')}
SOURCE URLS:
{chr(10).join(candidate.get('source_urls', [])[:5])}

SOURCE TEXT:
{source_text[:15000]}
""".strip()
    try:
        data = client.ai(system=system, user=user, schema_name="sports_games_post_v1", schema=POST_SCHEMA, max_tokens=2200)
    except Exception as exc:
        logger.warning("Post generation failed: %s", exc)
        return None
    story = dict(data)
    story["candidate"] = candidate
    story["source_text"] = source_text
    story["image_url"] = article.get("image_url", "")
    story["game_or_sport"] = candidate.get("game_or_sport", "")
    story["subject"] = candidate.get("subject", "")
    story["claim"] = candidate.get("claim_or_event", "")
    story["category"] = candidate.get("category", "")
    story["angle"] = candidate.get("angle", "")
    story["published_at"] = iso(now_bd())
    if not validate_story(story):
        return None
    if is_video_game_contaminated(" ".join([story.get("headline", ""), story.get("dek", ""), story.get("body", ""), story.get("why_interesting", "")])):
        logger.warning("Generated post contaminated by gaming terms: %s", story.get("headline"))
        return None
    return story


def validate_story(story: dict) -> bool:
    if not text(story.get("headline")) or not text(story.get("dek")) or not text(story.get("body")):
        return False
    if not (complete_sentence(story["dek"]) and complete_sentence(story["body"]) and complete_sentence(story["why_interesting"])):
        return False
    if sentence_count(story["body"]) > 6:
        return False
    if len(story.get("key_points", [])) > 5:
        return False
    if any("..." in text(x) or "…" in text(x) for x in [story.get("headline"), story.get("dek"), story.get("body"), story.get("why_interesting")]):
        return False
    return True


def grounded_output(story: dict, candidate: dict) -> bool:
    source = text(story.get("source_text"))
    fields = " ".join([
        text(story.get("headline")), text(story.get("dek")), text(story.get("body")),
        text(story.get("why_interesting")), " ".join(text(x) for x in story.get("key_points", [])),
    ])
    # Deterministic numeric/date grounding. Names and qualitative claims are reviewed by the verification gate.
    numbers = re.findall(r"\b(?:\d{1,4}(?:[.,]\d{1,3})?|\d{4})\b", fields)
    for n in numbers:
        normalized = n.replace(",", "").replace(".", "")
        if len(normalized) >= 3 and normalized not in re.sub(r"[^0-9]", " ", source):
            return False
    return True


# ---------------------------------------------------------------------------
# Daily sports planner
# ---------------------------------------------------------------------------

DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "maxItems": 45,
            "items": {
                "type": "object",
                "properties": {
                    "sport": {"type": "string"},
                    "event": {"type": "string"},
                    "date": {"type": "string"},
                    "time_utc": {"type": "string"},
                    "competition": {"type": "string"},
                    "stage": {"type": "string"},
                    "location": {"type": "string"},
                    "importance": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                },
                "required": ["sport", "event", "date", "time_utc", "competition", "stage", "location", "importance", "reason", "source_urls"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def build_daily_candidate(client: Clients, candidates: list[dict], mode: str, target: date) -> dict | None:
    if not candidates:
        return None
    grouped = candidates[:MAX_SPORT_EVENT_CANDIDATES]
    blocks = []
    for i, c in enumerate(grouped, 1):
        blocks.append(f"ID: {i}\nTITLE: {c['title']}\nSOURCE: {c['source']}\nURL: {c['url']}\nEXCERPT: {c['excerpt'][:900]}")
    system = f"""
You are a sports desk editor building a dated reference post for {mode}.
TARGET DATE: {target.isoformat()}
Extract notable real-world sporting events only. Exclude video games and esports.
For NEXT, only include events actually scheduled for the target date. For PAST, only include events/results that actually belong to the target date.
Never invent a fixture, score, venue, competition stage or time. If a field is unknown, use an empty string rather than guess.
The output is a reference index, not a news article.
""".strip()
    user = "\n\n".join(blocks)
    try:
        data = client.ai(system=system, user=user, schema_name="sports_daily_events_v1", schema=DAILY_SCHEMA, max_tokens=3000)
    except Exception as exc:
        logger.warning("Daily event extraction failed: %s", exc)
        return None
    events = data.get("events", [])
    if not events:
        return None
    cleaned = []
    for event in events:
        if text(event.get("date")) != target.isoformat():
            continue
        urls = [u for u in event.get("source_urls", []) if text(u) and source_tier(u) <= 3 and not is_video_game_contaminated(u)]
        if not urls:
            continue
        cleaned.append({**event, "source_urls": urls[:4]})
    if not cleaned:
        return None
    return {"mode": mode, "events": cleaned, "source_candidates": grouped, "target_date": target.isoformat()}


def generate_daily_post(client: Clients, plan: dict, date_anchor: date) -> dict | None:
    mode = plan["mode"]
    events = plan["events"]
    system = f"""
You are the lead editor for a permanent sports calendar archive.
Write the {mode} post for the exact date {date_anchor.strftime('%d %B %Y')}.
This must remain understandable years later.
Do not say tomorrow, yesterday, today, tonight, latest or just now. Use the exact date.
Do not invent details. Use only supplied event records and source links.
Organize by sport. Feature the most consequential events first, then provide a compact event index.
For PAST, emphasize what happened and major results. For NEXT, emphasize what is scheduled and why it is worth following.
Keep the main post compact enough for Telegram.
""".strip()
    user = json.dumps({"mode": mode, "date": date_anchor.isoformat(), "events": events}, ensure_ascii=False)
    data = client.ai(system=system, user=user, schema_name="sports_games_post_v1", schema=POST_SCHEMA, max_tokens=2600)
    story = dict(data)
    story.update({
        "game_or_sport": "Sports",
        "subject": date_anchor.isoformat(),
        "claim": f"Sports events for {date_anchor.isoformat()}",
        "category": "sports_daily_next" if mode == "NEXT" else "sports_daily_past",
        "angle": "calendar",
        "date_anchor": date_anchor.isoformat(),
        "published_at": iso(now_bd()),
        "source_text": " ".join(e.get("reason", "") for e in events),
        "source_urls": list(dict.fromkeys(u for e in events for u in e.get("source_urls", [])))[:5],
        "tags": ["#Sports", "#SportsCalendar"],
        "why_interesting": "A dated reference to notable sporting events.",
        "key_points": [
            f"{e.get('sport')}: {e.get('event')}" for e in events[:5] if text(e.get("sport")) and text(e.get("event"))
        ],
        "image_url": "",
    })
    if not all(text(e.get("date")) == date_anchor.isoformat() and e.get("source_urls") for e in events):
        return None
    if not validate_story(story):
        return None
    return story


# ---------------------------------------------------------------------------
# Telegram rendering
# ---------------------------------------------------------------------------


def bold_terms_html(value: str, terms: list[str]) -> str:
    result = html.escape(text(value), quote=False)
    replacements: list[tuple[str, str]] = []
    for term in sorted({text(t) for t in terms if text(t)}, key=len, reverse=True):
        marker = f"\uE000{len(replacements)}\uE001"
        pattern = re.compile(re.escape(html.escape(term, quote=False)), re.I)
        result, n = pattern.subn(marker, result, count=1)
        if n:
            replacements.append((marker, term))
    for marker, original in replacements:
        result = result.replace(marker, "<b>" + html.escape(original, quote=False) + "</b>")
    return result


def story_terms(story: dict) -> list[str]:
    terms = [
        text(story.get("game_or_sport")), text(story.get("subject")),
        text(story.get("date_anchor")),
    ]
    for point in story.get("key_points", [])[:4]:
        words = re.findall(r"\b[A-Z][A-Za-z0-9'’-]{2,}\b", text(point))
        terms.extend(words[:4])
    return [x for x in terms if x]


def dynamic_rich_html(story: dict, source_urls: list[str] | None = None) -> str:
    fmt = text(story.get("format")) or "fact"
    labels = {
        "fact": "DID YOU KNOW?",
        "game_discovery": "GAME DISCOVERY",
        "rule_check": "RULE CHECK",
        "how_to_play": "HOW TO PLAY",
        "history": "GAME / SPORTS HISTORY",
        "on_this_date": "ON THIS DATE",
        "century_ago": "100 YEARS AGO",
        "why": "WHY?",
        "first_last_only": "FIRST · LAST · ONLY",
        "then_vs_now": "THEN → NOW",
        "forgotten": "FORGOTTEN",
        "daily_next": "NEXT UP",
        "daily_past": "THE DAY IN SPORTS",
    }
    label = labels.get(fmt, "SPORTS & GAMES")
    terms = story_terms(story)
    parts = [
        '<img src="tg://photo?id=sportsphoto">',
        "<h2>" + html.escape(label) + "</h2>",
        "<h1>" + html.escape(text(story.get("headline"))) + "</h1>",
        "<p>" + bold_terms_html(text(story.get("dek")), terms) + "</p>",
    ]
    if text(story.get("date_anchor")):
        parts.append("<aside>" + html.escape(text(story["date_anchor"])) + "</aside>")
    for section_title, content in [
        ("WHAT HAPPENED", story.get("body")),
        ("WHY IT'S INTERESTING", story.get("why_interesting")),
    ]:
        if text(content):
            parts.append("<h2>" + html.escape(section_title) + "</h2>")
            parts.append("<p>" + bold_terms_html(text(content), terms) + "</p>")
    points = [text(x) for x in story.get("key_points", []) if text(x)]
    if points:
        parts.append("<h2>KEY POINTS</h2>")
        parts.append("<p>" + "<br>".join("• " + bold_terms_html(p, terms) for p in points) + "</p>")
    urls = source_urls if source_urls is not None else story.get("sources", []) or story.get("source_urls", [])
    urls = list(dict.fromkeys(u for u in urls if text(u)))[:4]
    if urls:
        src_lines = []
        for u in urls:
            label2 = source_label(u)
            src_lines.append(f'<a href="{html.escape(u, quote=True)}">{html.escape(label2)}</a>')
        parts.append("<footer><b>Sources:</b> " + " · ".join(src_lines) + "</footer>")
    tags = " ".join(text(t) for t in story.get("tags", []) if text(t))
    if tags:
        parts.append("<p>" + html.escape(tags) + "</p>")
    return "\n".join(parts)


def rich_visible_length(value: str) -> int:
    stripped = re.sub(r"<[^>]+>", "", value)
    return len(html.unescape(stripped))


def fit_rich_html(story: dict) -> str:
    variants = [
        (5, 520, 5),
        (4, 420, 4),
        (3, 340, 3),
    ]
    copy = dict(story)
    for body_sentences, body_len, points_n in variants:
        copy["body"] = clamp(story.get("body", ""), body_len)
        copy["why_interesting"] = clamp(story.get("why_interesting", ""), 360 if body_sentences >= 4 else 280)
        copy["key_points"] = list(story.get("key_points", []))[:points_n]
        rendered = dynamic_rich_html(copy)
        if rich_visible_length(rendered) <= MAX_RICH_CHARACTERS:
            return rendered
    return dynamic_rich_html(copy)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        r = session.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        return img
    except Exception:
        return None


def create_visual(story: dict, index: int = 0) -> str:
    if Image is None:
        raise RuntimeError("Pillow is required for image generation. Run: pip install -r requirements.txt")
    W, H = 1200, 675
    source_img = download_image(text(story.get("image_url")))
    if source_img:
        bg = ImageOps.fit(source_img, (W, H), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle((0, 0, W, H), fill=(0, 0, 0, 95))
        draw.rounded_rectangle((45, 45, 430, 105), radius=20, fill=(255, 255, 255, 225))
        draw.text((70, 62), text(story.get("format", "SPORTS & GAMES")).upper().replace("_", " "), font=find_font(27, True), fill=(20, 20, 20))
        draw.rounded_rectangle((45, H - 105, W - 45, H - 45), radius=22, fill=(0, 0, 0, 185))
        draw.text((75, H - 90), clamp(text(story.get("headline")), 52), font=find_font(35, True), fill=(255, 255, 255))
        bg = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    else:
        bg = Image.new("RGB", (W, H), (245, 245, 245))
        draw = ImageDraw.Draw(bg)
        category = text(story.get("format", "SPORTS & GAMES")).upper().replace("_", " ")
        draw.text((65, 65), category, font=find_font(36, True), fill=(25, 25, 25))
        draw.text((65, 145), clamp(text(story.get("headline")), 54), font=find_font(52, True), fill=(0, 0, 0))
        date_anchor = text(story.get("date_anchor"))
        if date_anchor:
            draw.text((65, 545), date_anchor, font=find_font(30, False), fill=(60, 60, 60))
        draw.text((W - 280, H - 70), CHANNEL, font=find_font(28, True), fill=(40, 40, 40))
    path = f"/tmp/sports_games_{int(time.time() * 1000)}_{index}.jpg"
    bg.save(path, "JPEG", quality=90, optimize=True)
    return path


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def telegram_call(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last = {"ok": False, "description": "Unknown error"}
    for attempt in range(1, 6):
        try:
            response = session.post(url, data=data or {}, files=files, timeout=90)
            result = response.json()
            if result.get("ok"):
                return result
            last = result
            if response.status_code == 429:
                retry_after = int(result.get("parameters", {}).get("retry_after", 5))
                time.sleep(max(1, retry_after))
                continue
            if response.status_code >= 500:
                time.sleep(2 * attempt)
                continue
            break
        except Exception as exc:
            last = {"ok": False, "description": str(exc)}
            time.sleep(2 * attempt)
    return last


def send_rich_photo(image_path: str, rich_html: str) -> dict:
    rich_message = {
        "html": rich_html,
        "media": [
            {
                "id": "sportsphoto",
                "media": {"type": "photo", "media": "attach://photo"},
            }
        ],
        "skip_entity_detection": False,
    }
    with open(image_path, "rb") as photo:
        return telegram_call(
            "sendRichMessage",
            data={
                "chat_id": CHANNEL,
                "rich_message": json.dumps(rich_message, ensure_ascii=False),
            },
            files={"photo": photo},
        )


def send_bot_api_fallback(image_path: str, rich_html: str) -> dict:
    text_caption = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    text_caption = re.sub(r"</(?:p|h1|h2|footer|aside)>", "\n", text_caption, flags=re.I)
    text_caption = re.sub(r"<[^>]+>", "", text_caption)
    text_caption = html.unescape(text_caption)
    text_caption = re.sub(r"\n{3,}", "\n\n", text_caption).strip()
    if len(text_caption) > 900:
        text_caption = text_caption[:890].rsplit(" ", 1)[0] + "..."
    with open(image_path, "rb") as photo:
        return telegram_call("sendPhoto", data={"chat_id": CHANNEL, "caption": text_caption}, files={"photo": photo})


# ---------------------------------------------------------------------------
# Persistence of knowledge records
# ---------------------------------------------------------------------------


def record_post(state: dict, story: dict, message_id: Any = None) -> None:
    candidate = story.get("candidate", {})
    claim_key = normalized_claim_key(text(story.get("subject")), text(story.get("claim")), text(story.get("angle")))
    state.setdefault("claims", {})[claim_key] = {
        "subject": text(story.get("subject")),
        "claim": text(story.get("claim")),
        "angle": text(story.get("angle")),
        "category": text(story.get("category")),
        "published_at": iso(now_bd()),
    }
    entity_key = normalize_text(story.get("game_or_sport"))
    if entity_key:
        entity = state.setdefault("entities", {}).setdefault(entity_key, {
            "name": text(story.get("game_or_sport")), "post_count": 0, "categories": {}, "angles": {}
        })
        entity["post_count"] += 1
        entity["categories"][text(story.get("category"))] = entity["categories"].get(text(story.get("category")), 0) + 1
        entity["angles"][text(story.get("angle"))] = entity["angles"].get(text(story.get("angle")), 0) + 1
    state.setdefault("posts", []).append({
        "published_at": iso(now_bd()),
        "message_id": message_id,
        "format": text(story.get("format")),
        "category": text(story.get("category")),
        "angle": text(story.get("angle")),
        "game_or_sport": text(story.get("game_or_sport")),
        "subject": text(story.get("subject")),
        "claim": text(story.get("claim")),
        "headline": text(story.get("headline")),
        "date_anchor": text(story.get("date_anchor")),
        "source_urls": list(story.get("sources", []) or story.get("source_urls", []))[:5],
        "canonical_url": canonical_url(candidate.get("url", "")) if candidate else "",
    })
    state.setdefault("angle_history", []).append({"angle": text(story.get("angle")), "at": iso(now_bd())})
    state.setdefault("category_history", []).append({"category": text(story.get("category")), "at": iso(now_bd())})


def daily_flag_key(kind: str, target: date) -> str:
    return f"{kind}:{target.isoformat()}"


def already_done_today(state: dict, kind: str, target: date) -> bool:
    return bool(state.get("daily_flags", {}).get(daily_flag_key(kind, target)))


def mark_daily_done(state: dict, kind: str, target: date) -> None:
    state.setdefault("daily_flags", {})[daily_flag_key(kind, target)] = iso(now_bd())


def discovery_count_today(state: dict) -> int:
    today = now_bd().date().isoformat()
    return sum(1 for p in state.get("posts", []) if text(p.get("published_at")).startswith(today) and p.get("category") not in {"sports_daily_next", "sports_daily_past"})


def last_discovery_at(state: dict) -> datetime | None:
    rows = [parse_dt(p.get("published_at")) for p in state.get("posts", []) if p.get("category") not in {"sports_daily_next", "sports_daily_past"}]
    rows = [x for x in rows if x]
    return max(rows) if rows else None


# ---------------------------------------------------------------------------
# End-to-end modes
# ---------------------------------------------------------------------------


def process_daily_sports(client: Clients, state: dict, kind: str, target: date) -> dict | None:
    if already_done_today(state, kind, target):
        return None
    mode = "NEXT" if kind == "next" else "PAST"
    raw = search_daily_sports(client, "next" if kind == "next" else "past")
    if not raw:
        logger.warning("No daily sports candidates for %s %s", mode, target)
        return None
    plan = build_daily_candidate(client, raw, mode, target)
    if not plan:
        return None
    story = generate_daily_post(client, plan, target)
    if not story:
        return None
    story["format"] = "daily_next" if kind == "next" else "daily_past"
    return story


def process_evergreen(client: Clients, state: dict) -> list[dict]:
    if discovery_count_today(state) >= MAX_DISCOVERY_POSTS_PER_DAY:
        return []
    last = last_discovery_at(state)
    if last and (now_bd() - last).total_seconds() < DISCOVERY_COOLDOWN_HOURS * 3600:
        return []
    raw = search_evergreen(client)
    if not raw:
        return []
    raw = raw[:MAX_DISCOVERY_CANDIDATES]
    classified = classify_candidates(client, raw, mode="evergreen discovery")
    verified = []
    for c in classified:
        if is_video_game_contaminated(" ".join([text(c.get("game_or_sport")), text(c.get("subject")), text(c.get("claim_or_event"))])):
            continue
        if semantic_claim_duplicate(state, c):
            continue
        verification = verify_candidate(client, c)
        ok, reason = verify_hard_gates(c, verification)
        c["verification"] = verification
        if not ok:
            logger.info("Reject candidate %s: %s", c.get("subject"), reason)
            continue
        verified.append(c)
    selected = editorial_select(client, verified, state, max_items=min(2, MAX_DISCOVERY_POSTS_PER_DAY - discovery_count_today(state)))
    stories = []
    for candidate in selected:
        story = generate_post(client, candidate)
        if not story:
            continue
        if not grounded_output(story, candidate):
            logger.info("Reject generated story for grounding: %s", story.get("headline"))
            continue
        stories.append(story)
    return stories


def publish_story(state: dict, story: dict, index: int) -> bool:
    rich_html = fit_rich_html(story)
    if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
        logger.error("Post too long: %s", story.get("headline"))
        return False
    image_path = create_visual(story, index)
    result = send_rich_photo(image_path, rich_html)
    if not result.get("ok"):
        logger.warning("Rich Message failed, using Bot API fallback: %s", result.get("description"))
        result = send_bot_api_fallback(image_path, rich_html)
    if result.get("ok"):
        msg = result.get("result", {})
        message_id = msg.get("message_id") if isinstance(msg, dict) else None
        candidate = story.get("candidate", {})
        if candidate.get("url"):
            append_published_url(candidate["url"])
        record_post(state, story, message_id)
        return True
    logger.error("Telegram publish failed: %s", result.get("description"))
    return False


def run_once() -> None:
    require_credentials()
    state = load_state()
    prune_state(state)
    state["last_run_at"] = iso(now_bd())
    client = Clients()
    logger.info("%s v%s | channel=%s", APP_NAME, APP_VERSION, CHANNEL)
    logger.info("Local time: %s", now_bd().isoformat())

    stories: list[dict] = []

    # Two permanent daily anchors.
    current = now_bd()
    # NEXT UP is allowed once any time after 06:00 local.
    if current.hour >= 6:
        next_target = current.date() + timedelta(days=1)
        story = process_daily_sports(client, state, "next", next_target)
        if story:
            stories.append(story)
    # DAY IN SPORTS is generated after the sports day is mature; after 19:00 local.
    if current.hour >= 19:
        past_target = current.date() - timedelta(days=1)
        story = process_daily_sports(client, state, "past", past_target)
        if story:
            stories.append(story)

    # Evergreen stream. It is quality-gated, capped and cooled down, never forced.
    stories.extend(process_evergreen(client, state))

    published = 0
    for index, story in enumerate(stories, start=1):
        if publish_story(state, story, index):
            published += 1
            if story.get("format") == "daily_next":
                mark_daily_done(state, "next", current.date() + timedelta(days=1))
            elif story.get("format") == "daily_past":
                mark_daily_done(state, "past", current.date() - timedelta(days=1))
            save_state(state)
            time.sleep(POST_DELAY_SECONDS)
    save_state(state)
    logger.info("Completed run. Published=%d", published)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test() -> None:
    assert canonical_url("https://www.example.com/story/?utm_source=x&ref=y") == "example.com/story"
    assert is_video_game_contaminated("PlayStation patch notes")
    assert not is_video_game_contaminated("Ludo rule explanation")
    assert similarity("Why is tennis called tennis?", "Origin of the word tennis") > 0.35

    state = default_state()
    candidate = {
        "kind": "fact",
        "category": "evergreen_fact",
        "angle": "origin",
        "game_or_sport": "Tennis",
        "subject": "tennis terminology",
        "claim_or_event": "The term has a documented historical origin.",
        "why_interesting": "The familiar word has an unexpected history.",
        "source_urls": ["https://www.britannica.com/sports/tennis"],
    }
    assert not semantic_claim_duplicate(state, candidate)
    state["claims"][normalized_claim_key(candidate["subject"], candidate["claim_or_event"], candidate["angle"])] = {
        "subject": candidate["subject"], "claim": candidate["claim_or_event"], "angle": candidate["angle"]
    }
    assert semantic_claim_duplicate(state, candidate)

    fake_verify = {"status": "verified", "confidence": 95}
    ok, reason = verify_hard_gates({**candidate, "source_urls": ["https://www.britannica.com/sports/tennis", "https://www.fide.com/faq"]}, fake_verify)
    assert ok, reason

    story = {
        "format": "fact",
        "headline": "Why This Sports Word Has a Surprising Origin",
        "dek": "A familiar sports term has a history that is easy to overlook.",
        "body": "Historical sources trace the term through earlier forms before it reached the modern usage described here.",
        "why_interesting": "The word looks ordinary today, but its history connects the modern game with earlier traditions.",
        "key_points": ["Historical origin", "Modern usage"],
        "date_anchor": "",
        "sources": ["https://www.britannica.com/sports/tennis"],
        "tags": ["#Sports", "#History"],
        "game_or_sport": "Tennis",
        "subject": "tennis terminology",
        "claim": candidate["claim_or_event"],
        "category": "evergreen_fact",
        "angle": "origin",
    }
    assert validate_story(story)
    assert not is_video_game_contaminated(" ".join([story["headline"], story["body"]]))
    rendered = dynamic_rich_html(story)
    assert "DID YOU KNOW?" in rendered
    assert "<h1>" in rendered
    assert "tg://photo?id=sportsphoto" in rendered
    assert rich_visible_length(rendered) < MAX_RICH_CHARACTERS

    # Current daily posts must use date anchors instead of disposable day words.
    daily = dict(story)
    daily.update({"format": "daily_next", "date_anchor": "22 September 2026", "headline": "Scheduled Sports Events for 22 September 2026"})
    rendered_daily = dynamic_rich_html(daily)
    assert "22 September 2026" in rendered_daily

    # State pruning must remove old queue items while retaining knowledge records.
    old = iso(now_bd() - timedelta(days=400))
    state = default_state()
    state["queue"]["x"] = {"first_seen_at": old}
    state["posts"].append({"published_at": old, "subject": "Old fact", "claim": "Keep knowledge archive"})
    prune_state(state)
    assert "x" not in state["queue"]
    assert state["posts"], "Knowledge posts must remain in the archive"

    logger.info("Self-test passed for %s v%s", APP_NAME, APP_VERSION)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--self-test", action="store_true", help="Run offline regression tests")
    parser.add_argument("--version", action="store_true", help="Print version")
    args = parser.parse_args()
    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return
    if args.self_test:
        self_test()
        return
    run_once()


if __name__ == "__main__":
    main()
