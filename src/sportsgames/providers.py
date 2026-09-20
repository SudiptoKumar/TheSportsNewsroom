from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urljoin

from .config import (
    CEREBRAS_API_KEY, CEREBRAS_MODEL, EXA_API_KEY, HEADERS, HTTP_TIMEOUT,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL,
)
from .taxonomy import LEAD_ONLY_DOMAINS, PRIMARY_DOMAINS, REFERENCE_DOMAINS, SECONDARY_DOMAINS
from .utils import canonical_url, domain_of, iso, parse_dt, strip_tags, text

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover
    requests = None
    HTTPAdapter = None
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None
try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None
try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None
try:
    from exa_py import Exa
except ImportError:  # pragma: no cover
    Exa = None
try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:  # pragma: no cover
    Cerebras = None

logger = logging.getLogger("sports-games-hub.providers")


if requests is not None:
    HTTP = requests.Session()
    if Retry is not None and HTTPAdapter is not None:
        retry = Retry(
            total=3, connect=3, read=3, status=3, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
        )
        HTTP.mount("https://", HTTPAdapter(max_retries=retry))
        HTTP.mount("http://", HTTPAdapter(max_retries=retry))
else:
    HTTP = None


@dataclass
class Providers:
    exa: Any
    cerebras: Any

    @classmethod
    def from_env(cls, require: bool = True) -> "Providers":
        missing = []
        if not EXA_API_KEY: missing.append("EXA_API_KEY")
        if not CEREBRAS_API_KEY: missing.append("CEREBRAS_API_KEY")
        if require and not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        if Exa is None or Cerebras is None:
            raise RuntimeError("API SDKs are unavailable. Run: pip install -r requirements.txt")
        return cls(Exa(api_key=EXA_API_KEY), Cerebras(api_key=CEREBRAS_API_KEY))

    def ai(self, *, system: str, user: str, schema_name: str, schema: dict,
           max_tokens: int = 2200, temperature: float = 0.15) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.cerebras.chat.completions.create(
                    model=CEREBRAS_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    },
                    reasoning_effort="low",
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                )
                content = text(response.choices[0].message.content)
                if content.startswith("```"):
                    content = content.strip().strip("`").replace("json\n", "", 1)
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("AI response is not an object")
                return value
            except Exception as exc:  # retry transient model/API failures
                last_exc = exc
                logger.warning("Cerebras attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"Cerebras failed after 3 attempts: {last_exc}")

    def exa_search(self, query: str, *, start=None, end=None,
                   domains: list[str] | None = None, num: int = 8) -> list[dict]:
        kwargs: dict[str, Any] = {"type": "auto", "num_results": num,
                                  "contents": {"highlights": {"max_characters": 1600}}}
        if domains:
            kwargs["include_domains"] = domains
        if start:
            kwargs["start_published_date"] = start.isoformat()
        if end:
            kwargs["end_published_date"] = end.isoformat()
        try:
            results = self.exa.search_and_contents(query, **kwargs)
            output = []
            for result in getattr(results, "results", []):
                url = text(getattr(result, "url", ""))
                title = text(getattr(result, "title", ""))
                if not url or not title:
                    continue
                highlights = getattr(result, "highlights", None) or []
                output.append({
                    "url": url,
                    "canonical": canonical_url(url),
                    "title": title,
                    "published_date": iso(parse_dt(getattr(result, "published_date", ""))),
                    "source": source_label(url),
                    "excerpt": " ".join(text(x) for x in highlights)[:3000],
                    "discovery": "exa",
                })
            return output
        except Exception as exc:
            logger.warning("Exa search failed for %s: %s", query, exc)
            return []


def fetch_article(url: str, fallback_excerpt: str = "") -> dict:
    if HTTP is None:
        return {"text": fallback_excerpt, "image_url": "", "canonical": canonical_url(url)}
    try:
        response = HTTP.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS)
        response.raise_for_status()
        raw = response.text
        extracted = trafilatura.extract(raw, include_comments=False, include_tables=True) if trafilatura else ""
        soup = BeautifulSoup(raw, "html.parser") if BeautifulSoup else None
        image_url = ""
        if soup:
            for selector in [("meta", {"property": "og:image"}), ("meta", {"name": "twitter:image"})]:
                tag = soup.find(*selector)
                if tag and tag.get("content"):
                    image_url = urljoin(url, tag["content"])
                    break
        return {
            "text": (extracted or strip_tags(raw))[:24000],
            "image_url": image_url,
            "canonical": canonical_url(response.url or url),
        }
    except Exception as exc:
        logger.warning("Article extraction failed %s: %s", url, exc)
        return {"text": fallback_excerpt, "image_url": "", "canonical": canonical_url(url)}


def source_label(url: str, fallback: str = "Source") -> str:
    labels = {
        "bbc.com": "BBC Sport", "espn.com": "ESPN", "skysports.com": "Sky Sports",
        "theguardian.com": "The Guardian", "reuters.com": "Reuters", "apnews.com": "AP",
        "fifa.com": "FIFA", "uefa.com": "UEFA", "icc-cricket.com": "ICC",
        "worldathletics.org": "World Athletics", "fide.com": "FIDE", "itftennis.com": "ITF",
        "atptour.com": "ATP", "wtatennis.com": "WTA", "formula1.com": "Formula 1",
        "fia.com": "FIA", "olympics.com": "Olympics.com", "world.rugby": "World Rugby",
        "boardgamegeek.com": "BoardGameGeek", "britannica.com": "Britannica",
        "guinnessworldrecords.com": "Guinness World Records", "wikipedia.org": "Wikipedia",
        "atlasobscura.com": "Atlas Obscura",
    }
    return labels.get(domain_of(url), fallback or domain_of(url) or "Source")


def source_tier(url: str) -> int:
    d = domain_of(url)
    if any(d == x or d.endswith("." + x) for x in PRIMARY_DOMAINS):
        return 1
    if any(d == x or d.endswith("." + x) for x in SECONDARY_DOMAINS):
        return 2
    if any(d == x or d.endswith("." + x) for x in REFERENCE_DOMAINS):
        return 3
    if any(d == x or d.endswith("." + x) for x in LEAD_ONLY_DOMAINS):
        return 4
    return 3


def fetch_rss(feeds: list[dict], limit_per_feed: int = 30) -> list[dict]:
    if feedparser is None:
        logger.warning("feedparser unavailable, skipping RSS")
        return []
    rows = []
    for feed in feeds:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:limit_per_feed]:
                url = text(entry.get("link"))
                title = text(entry.get("title"))
                if not url or not title:
                    continue
                published = entry.get("published") or entry.get("updated") or ""
                dt = parse_dt(published)
                rows.append({
                    "url": url,
                    "canonical": canonical_url(url),
                    "title": title,
                    "published_date": iso(dt),
                    "source": feed["name"],
                    "excerpt": strip_tags(entry.get("summary", ""))[:2500],
                    "discovery": "rss",
                })
        except Exception as exc:
            logger.warning("RSS failed %s: %s", feed.get("name"), exc)
    return rows


def telegram_call(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    if HTTP is None:
        return {"ok": False, "description": "requests unavailable"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last = {"ok": False, "description": "Unknown error"}
    for attempt in range(1, 6):
        try:
            response = HTTP.post(url, data=data or {}, files=files, timeout=90)
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
