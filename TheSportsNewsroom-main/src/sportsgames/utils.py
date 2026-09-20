from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit
from difflib import SequenceMatcher

from .config import TRACKING_PARAMS


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_text(value: object) -> str:
    s = text(value).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokenize(value: object) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "from", "with",
        "by", "at", "as", "is", "are", "was", "were", "be", "been", "this", "that", "it",
        "its", "new", "game", "games", "sport", "sports", "news", "why", "how", "what",
        "did", "does", "do", "has", "have", "had", "about", "after", "before", "into",
        "than", "over", "their", "they", "them", "who", "which", "when", "where",
    }
    return {t for t in normalize_text(value).split() if len(t) >= 3 and t not in stop}


def similarity(a: object, b: object) -> float:
    aa, bb = normalize_text(a), normalize_text(b)
    if not aa or not bb:
        return 0.0
    seq = SequenceMatcher(None, aa, bb).ratio()
    ta, tb = tokenize(aa), tokenize(bb)
    jac = len(ta & tb) / max(1, len(ta | tb))
    return 0.55 * seq + 0.45 * jac


def canonical_url(url: str) -> str:
    raw = text(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower().removeprefix("www.").split(":")[0]
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return host + path + (("?" + urlencode(query)) if query else "")


def domain_of(url: str) -> str:
    raw = text(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    return urlsplit(raw).netloc.lower().removeprefix("www.").split(":")[0]


def parse_dt(value: object) -> datetime | None:
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


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def sentence_count(value: object) -> int:
    s = text(value)
    if not s:
        return 0
    return len(re.findall(r"(?<=[.!?])\s+", s)) + (1 if s[-1:] in ".!?" else 0)


def complete_sentence(value: object) -> bool:
    s = text(value)
    return bool(s and s[-1:] in ".!?")


def clamp(value: object, n: int) -> str:
    s = text(value)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def safe_filename(value: str) -> str:
    s = normalize_text(value).replace(" ", "-")
    return re.sub(r"[^a-z0-9_-]", "", s)[:80] or "item"


def sha(value: object, length: int = 32) -> str:
    return hashlib.sha256(text(value).encode("utf-8")).hexdigest()[:length]


def escape_html(value: object) -> str:
    return html.escape(text(value), quote=False)


def strip_tags(value: object) -> str:
    return re.sub(r"<[^>]+>", " ", text(value)).strip()
