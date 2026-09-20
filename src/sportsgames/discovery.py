from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from typing import Iterable

from .config import CONFIG, MAX_CANDIDATES, MAX_CLASSIFICATION_CANDIDATES, MAX_EXA_RESULTS, SEARCHES_PER_RUN
from .providers import Providers, source_tier
from .taxonomy import (
    BOARD_GAMES,
    CARD_GAMES,
    MIND_GAMES,
    PARTY_GAMES,
    SPORTS,
    SURPRISE_PATTERNS,
    TRADITIONAL_GAMES,
    VIDEO_GAME_QUERY_PATTERNS,
)
from .utils import canonical_url, iso, normalize_text, parse_dt, sha, text

logger = logging.getLogger("sports-games-hub.discovery")


def is_video_game_contaminated(value: object) -> bool:
    s = text(value).lower()
    return any(re.search(r"\b" + re.escape(term) + r"\b", s) for term in VIDEO_GAME_QUERY_PATTERNS)


def _prelim_score(item: dict) -> float:
    title = normalize_text(item.get("title"))
    excerpt = normalize_text(item.get("excerpt"))
    score = float({1: 6, 2: 5, 3: 3, 4: 0}.get(source_tier(item.get("url", "")), 3))
    curiosity_terms = ("why", "origin", "first", "only", "oldest", "strange", "unusual", "rule", "history", "forgotten")
    if any(term in title for term in curiosity_terms):
        score += 5
    if any(term in title + " " + excerpt for term in ("new game", "new board game", "new card game", "new sport")):
        score += 4
    if len(excerpt) >= 200:
        score += 2
    if is_video_game_contaminated(item.get("title", "") + " " + item.get("excerpt", "")):
        score -= 50
    return score


def make_candidate_id(url: str, title: str, query_family: str = "") -> str:
    """Stable candidate identity used across retries/runs regardless of AI output."""
    return sha(f"{canonical_url(url)}|{normalize_text(title)}|{normalize_text(query_family)}", 24)


def _dedupe(items: Iterable[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    out = []
    for item in items:
        url = text(item.get("url"))
        title = text(item.get("title"))
        if not url or not title:
            continue
        key = canonical_url(url)
        if not key or key in seen_urls:
            continue
        combined = " ".join([title, text(item.get("excerpt")), url])
        if is_video_game_contaminated(combined):
            continue
        seen_urls.add(key)
        dt = parse_dt(item.get("published_date"))
        candidate_id = make_candidate_id(url, title, text(item.get("query_family")) or "other")
        out.append({
            **item,
            "candidate_id": candidate_id,
            "url": url,
            "canonical": key,
            "title": re.sub(r"\s+", " ", title).strip(),
            "excerpt": re.sub(r"\s+", " ", text(item.get("excerpt"))).strip(),
            "published_date": iso(dt),
            "query_family": text(item.get("query_family")) or "other",
            "prelim_score": _prelim_score(item),
        })
    return out


def discover_next_sports(providers: Providers, target: date) -> list[dict]:
    label = target.strftime("%d %B %Y")
    queries = [
        ("sports_now", f"sports events {label} schedule official fixtures tournaments championships"),
        ("sports_now", f"{label} football fixtures schedule matches"),
        ("sports_now", f"{label} cricket fixtures schedule matches"),
        ("sports_now", f"{label} tennis badminton basketball schedule matches"),
        ("sports_now", f"{label} formula 1 motogp motorsport race schedule"),
        ("sports_now", f"{label} athletics swimming cycling rowing sports events"),
        ("sports_now", f"{label} rugby hockey volleyball handball events"),
        ("sports_now", f"{label} snooker darts squash bowling kabaddi sepak takraw events"),
    ]
    rows = []
    for family, query in queries:
        rows.extend(providers.exa_search(query, num=MAX_EXA_RESULTS, family=family))
    return _dedupe(rows)[:MAX_CANDIDATES]


def discover_past_sports(providers: Providers, target: date) -> list[dict]:
    label = target.strftime("%d %B %Y")
    queries = [
        ("sports_past", f"sports results {label} major results championships finals records"),
        ("sports_past", f"{label} football results match reports"),
        ("sports_past", f"{label} cricket results scorecard match report"),
        ("sports_past", f"{label} tennis badminton basketball results finals"),
        ("sports_past", f"{label} formula 1 motorsport results"),
        ("sports_past", f"{label} athletics swimming cycling records results"),
        ("sports_past", f"{label} rugby hockey volleyball handball results"),
        ("sports_past", f"{label} snooker darts squash bowling kabaddi results"),
    ]
    rows = []
    for family, query in queries:
        rows.extend(providers.exa_search(query, num=MAX_EXA_RESULTS, family=family))
    return _dedupe(rows)[:MAX_CANDIDATES]


def discover_historical_date(providers: Providers, target: date, day_index: int = 0) -> list[dict]:
    years = [target.year - 100, target.year - 50, target.year - 25, target.year - 75, target.year - 125]
    rotating = years[day_index % len(years)]
    exact_date = target.strftime("%d %B")
    queries = [
        ("on_this_date", f'"{exact_date}" sports history on this date records championships unusual events'),
        ("century_ago", f'"{target.day} {target.strftime("%B")} {target.year - 100}" sports history games'),
        ("historical", f'"{target.day} {target.strftime("%B")} {rotating}" sports games history milestone record'),
    ]
    rows = []
    for family, query in queries:
        # Important: do not use Exa publication-date filters here. Modern pages can describe 100-year-old events.
        rows.extend(providers.exa_search(query, num=min(8, MAX_EXA_RESULTS), family=family))
    return _dedupe(rows)[:25]


def _game_terms() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for bucket, pool in [
        ("games", BOARD_GAMES),
        ("games", CARD_GAMES),
        ("games", PARTY_GAMES),
        ("games", TRADITIONAL_GAMES),
        ("games", MIND_GAMES),
        ("sports", SPORTS),
    ]:
        for name in pool:
            result.append((bucket, name))
    return result


def build_evergreen_queries(day_index: int) -> list[tuple[str, str]]:
    pools: dict[str, list[str]] = defaultdict(list)
    for pattern in SURPRISE_PATTERNS:
        pools["facts"].append(f'"{pattern}" sports games physical')
    for bucket, name in _game_terms():
        pools[bucket].append(f"{name} official rules history origin unusual fact")
        pools[bucket].append(f"{name} why called origin first oldest rule history")
    pools["games_new"].extend([
        "new board game 2026 announced physical tabletop",
        "new card game 2026 announced physical tabletop",
        "new tabletop game 2026 announced physical game",
        "new party game 2026 announced physical game",
        "new traditional game discovered history",
        "new physical sport invented 2025 2026 rules competition",
    ])
    pools["rules"].extend([
        "official board game rulebook clarification common mistake",
        "official card game rule clarification house rule",
        "sports rule change 2026 official rules",
        "unusual sports rules explained official",
        "historical sports rule changed origin",
    ])
    pools["history"].extend([
        "sports history forgotten event on this date",
        "board game history oldest known physical game",
        "card game history origin traditional game",
        "forgotten sport history unusual competition",
        "sports equipment history why designed this way",
    ])
    pools["discovery"].extend([
        "unusual traditional games around the world history rules",
        "obscure physical sports around the world rules",
        "games people may not know traditional regional",
        "strange board games history mechanics",
        "interesting card games around the world history",
    ])

    weights = [
        ("games", 4),
        ("facts", 4),
        ("rules", 3),
        ("history", 3),
        ("games_new", 2),
        ("discovery", 2),
        ("sports", 2),
    ]
    selected: list[tuple[str, str]] = []
    for family, quota in weights:
        values = pools[family]
        if not values:
            continue
        start = (day_index + len(family) * 17) % len(values)
        take = min(quota, len(values))
        for i in range(take):
            selected.append((family, values[(start + i * 3) % len(values)]))
    return selected[:SEARCHES_PER_RUN]


def discover_evergreen(providers: Providers, day_index: int) -> list[dict]:
    rows = []
    for family, query in build_evergreen_queries(day_index):
        rows.extend(providers.exa_search(query, num=5, family=family))
    deduped = _dedupe(rows)
    deduped.sort(key=lambda item: (item.get("prelim_score", 0), item.get("published_date", "")), reverse=True)
    return deduped[:MAX_CANDIDATES]
