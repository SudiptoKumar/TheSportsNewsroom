from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from .config import (
    CEREBRAS_API_KEY,
    CEREBRAS_MODEL,
    EXA_API_KEY,
    HEADERS,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_RETRY_COUNT,
    TELEGRAM_BOT_TOKEN,
)
from .taxonomy import LEAD_ONLY_DOMAINS, PRIMARY_DOMAINS, REFERENCE_DOMAINS, SECONDARY_DOMAINS
from .utils import canonical_url, domain_of, iso, parse_dt, strip_tags, text

try:
    import requests
except ImportError:
    requests = None
try:
    import feedparser
except ImportError:
    feedparser = None
try:
    import trafilatura
except ImportError:
    trafilatura = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
try:
    from exa_py import Exa
except ImportError:
    Exa = None
try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:
    Cerebras = None

logger = logging.getLogger("sports-games-hub.providers")
HTTP = requests.Session() if requests is not None else None


@dataclass
class Providers:
    exa: Any
    cerebras: Any
    report: Any = None
    ai_budget: Any = None

    @classmethod
    def from_env(cls, require: bool = True, report=None, ai_budget=None) -> "Providers":
        missing = []
        if not EXA_API_KEY:
            missing.append("EXA_API_KEY")
        if not CEREBRAS_API_KEY:
            missing.append("CEREBRAS_API_KEY")
        if require and not TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
        if Exa is None or Cerebras is None:
            raise RuntimeError("API SDKs are unavailable. Run: pip install -r requirements.txt")
        return cls(Exa(api_key=EXA_API_KEY), Cerebras(api_key=CEREBRAS_API_KEY), report, ai_budget)

    def ai(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict,
        max_tokens: int = 2200,
        temperature: float = 0.15,
        lane: str = "discovery",
    ) -> dict:
        last_exc: Exception | None = None
        reasoning_effort = os.getenv("CEREBRAS_REASONING_EFFORT", "low").strip().lower()
        if reasoning_effort not in {"low", "medium", "high"}:
            reasoning_effort = "low"
        for attempt in range(1, 3):
            # AI_MAX_CALLS_PER_RUN is an API-call budget, so each actual Cerebras request
            # consumes one unit, including a retry.
            if self.ai_budget is not None and not self.ai_budget.take(lane):
                if self.report:
                    self.report.count(f"ai.budget_exhausted.{lane}")
                raise RuntimeError(f"AI budget exhausted for lane={lane}")
            if self.report:
                self.report.count("ai.api_attempts")
                self.report.count(f"ai.api_attempts.{lane}")
            try:
                t0 = time.monotonic()
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
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    max_completion_tokens=max_tokens,
                )
                elapsed = time.monotonic() - t0
                finish_reason = text(getattr(response.choices[0], "finish_reason", ""))
                logger.info(
                    "Cerebras success schema=%s lane=%s finish_reason=%s elapsed=%.2fs",
                    schema_name,
                    lane,
                    finish_reason or "unknown",
                    elapsed,
                )
                if self.report:
                    self.report.count("ai.calls")
                    self.report.count(f"ai.finish.{finish_reason or 'unknown'}")
                if finish_reason in {"length", "max_tokens"}:
                    raise RuntimeError(f"Cerebras output truncated: finish_reason={finish_reason}")
                content = text(response.choices[0].message.content)
                if content.startswith("```"):
                    content = content.strip().strip("`").replace("json\n", "", 1)
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("AI response is not an object")
                return value
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Cerebras attempt %d failed schema=%s lane=%s: %s",
                    attempt,
                    schema_name,
                    lane,
                    exc,
                )
                if self.report:
                    self.report.count("ai.failures")
                if attempt < 2:
                    retry_after = 0
                    try:
                        response_obj = getattr(exc, "response", None)
                        retry_after = int(response_obj.headers.get("retry-after", 0)) if response_obj else 0
                    except Exception:
                        retry_after = 0
                    time.sleep(max(1.0, min(float(retry_after or 1.5), 10.0)))
        raise RuntimeError(f"Cerebras failed after 2 attempts: {last_exc}")

    def exa_search(
        self,
        query: str,
        *,
        start=None,
        end=None,
        domains: list[str] | None = None,
        num: int = 6,
        family: str = "",
    ) -> list[dict]:
        kwargs: dict[str, Any] = {
            "type": "auto",
            "num_results": num,
            "contents": {"highlights": {"max_characters": 1800}},
        }
        if domains:
            kwargs["include_domains"] = domains
        if start:
            kwargs["start_published_date"] = start.isoformat()
        if end:
            kwargs["end_published_date"] = end.isoformat()
        t0 = time.monotonic()
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
                    "excerpt": " ".join(text(x) for x in highlights)[:3600],
                    "discovery": "exa",
                    "query": query,
                    "query_family": family,
                })
            elapsed = time.monotonic() - t0
            logger.info(
                "Exa search family=%s results=%d elapsed=%.2fs query=%s",
                family,
                len(output),
                elapsed,
                query[:120],
            )
            if self.report:
                self.report.count("discovery.exa_searches")
                self.report.count("discovery.exa_results", len(output))
            return output
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning(
                "Exa search failed family=%s elapsed=%.2fs query=%s error=%s",
                family,
                elapsed,
                query[:120],
                exc,
            )
            if self.report:
                self.report.reject("discovery", "exa_failed", query, str(exc))
            return []


def fetch_article(url: str, fallback_excerpt: str = "", report=None) -> dict:
    """Fast-fail source extraction. Search evidence remains usable when pages block scraping."""
    canonical = canonical_url(url)
    if HTTP is None:
        fallback = text(fallback_excerpt)
        if report:
            report.source(url, bool(fallback), len(fallback), source_tier(url), "requests unavailable", 0, True)
        return {
            "text": fallback,
            "image_url": "",
            "canonical": canonical,
            "fallback": bool(fallback),
            "ok": bool(fallback),
        }

    attempts = max(1, HTTP_RETRY_COUNT + 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        t0 = time.monotonic()
        try:
            response = HTTP.get(
                url,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
                headers=HEADERS,
                allow_redirects=True,
            )
            status = response.status_code
            if status in {403, 404}:
                last_error = f"HTTP {status}"
                break
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                if attempt < attempts:
                    retry_after = int(response.headers.get("Retry-After", "0") or 0)
                    time.sleep(max(0.8, min(retry_after or 1.0, 4.0)))
                    continue
                break
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
            final_text = (extracted or strip_tags(raw))[:24000]
            elapsed = time.monotonic() - t0
            if report:
                report.source(url, bool(final_text), len(final_text), source_tier(url), "", elapsed, False)
            return {
                "text": final_text,
                "image_url": image_url,
                "canonical": canonical_url(response.url or url),
                "fallback": False,
                "ok": True,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(0.6)
                continue
            break

    fallback = text(fallback_excerpt)
    elapsed = time.monotonic() - t0
    if report:
        report.source(url, bool(fallback), len(fallback), source_tier(url), last_error, elapsed, bool(fallback))
    if fallback:
        logger.info("Using search evidence fallback for %s (%s)", url, last_error)
    else:
        logger.warning("Article extraction failed %s (%s)", url, last_error)
    return {
        "text": fallback,
        "image_url": "",
        "canonical": canonical,
        "fallback": bool(fallback),
        "ok": bool(fallback),
        "error": last_error,
    }


def source_label(url: str, fallback: str = "Source") -> str:
    labels = {
        "bbc.com": "BBC Sport",
        "espn.com": "ESPN",
        "skysports.com": "Sky Sports",
        "theguardian.com": "The Guardian",
        "reuters.com": "Reuters",
        "apnews.com": "AP",
        "fifa.com": "FIFA",
        "uefa.com": "UEFA",
        "icc-cricket.com": "ICC",
        "worldathletics.org": "World Athletics",
        "fide.com": "FIDE",
        "itftennis.com": "ITF",
        "atptour.com": "ATP",
        "wtatennis.com": "WTA",
        "formula1.com": "Formula 1",
        "fia.com": "FIA",
        "olympics.com": "Olympics.com",
        "world.rugby": "World Rugby",
        "boardgamegeek.com": "BoardGameGeek",
        "britannica.com": "Britannica",
        "guinnessworldrecords.com": "Guinness World Records",
        "wikipedia.org": "Wikipedia",
        "atlasobscura.com": "Atlas Obscura",
    }
    return labels.get(domain_of(url), fallback or domain_of(url) or "Source")


def source_tier(url: str) -> int:
    domain = domain_of(url)
    if any(domain == item or domain.endswith("." + item) for item in PRIMARY_DOMAINS):
        return 1
    if any(domain == item or domain.endswith("." + item) for item in SECONDARY_DOMAINS):
        return 2
    if any(domain == item or domain.endswith("." + item) for item in REFERENCE_DOMAINS):
        return 3
    if any(domain == item or domain.endswith("." + item) for item in LEAD_ONLY_DOMAINS):
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
                rows.append({
                    "url": url,
                    "canonical": canonical_url(url),
                    "title": title,
                    "published_date": iso(parse_dt(published)),
                    "source": feed["name"],
                    "excerpt": strip_tags(entry.get("summary", ""))[:2500],
                    "discovery": "rss",
                })
        except Exception as exc:
            logger.warning("RSS failed %s: %s", feed.get("name"), exc)
    return rows


def telegram_call(method: str, data: dict | None = None, files: dict | None = None) -> dict:
    """Call Telegram with duplicate-safe retry semantics for send operations.

    HTTP 429 is safe to retry because Telegram explicitly rejected the request.
    HTTP 5xx and transport exceptions are treated as delivery-uncertain and are NOT retried: a
    server/network failure can happen after Telegram accepted a non-idempotent send, and retrying
    it here could create a duplicate before the persisted publish-intent guard can intervene.
    """
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
    if HTTP is None:
        return {"ok": False, "description": "requests unavailable"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in range(1, 4):
        # requests multipart uploads advance file handles. A Telegram 429 is explicitly
        # rejected, so retrying is safe, but only if the same file is rewound first.
        for upload in (files or {}).values():
            handle = upload[1] if isinstance(upload, tuple) and len(upload) > 1 else upload
            if hasattr(handle, "seek"):
                try:
                    handle.seek(0)
                except Exception:
                    pass
        try:
            response = HTTP.post(url, data=data or {}, files=files, timeout=60)
            result = response.json()
            if result.get("ok"):
                logger.info("Telegram %s success", method)
                return result
            if response.status_code == 429:
                retry_after = int(result.get("parameters", {}).get("retry_after", 5))
                if attempt < 3:
                    time.sleep(max(1, min(retry_after, 20)))
                    continue
                return result
            if response.status_code >= 500:
                result["_delivery_uncertain"] = True
                logger.warning("Telegram %s returned %s; delivery is uncertain, not retrying", method, response.status_code)
                return result
            return result
        except Exception as exc:
            logger.warning("Telegram %s transport failure; delivery is uncertain, not retrying: %s", method, exc)
            return {"ok": False, "description": str(exc), "_delivery_uncertain": True}
    return {"ok": False, "description": "Telegram request exhausted retries"}
