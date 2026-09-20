from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import CONFIG, MAX_CANDIDATES, MAX_EXA_RESULTS, SEARCHES_PER_RUN
from .providers import Providers
from .taxonomy import (
    BOARD_GAMES, CARD_GAMES, LEAD_ONLY_DOMAINS, MIND_GAMES, PARTY_GAMES, SPORTS,
    SURPRISE_PATTERNS, TRADITIONAL_GAMES, VIDEO_GAME_QUERY_PATTERNS,
)
from .utils import canonical_url, domain_of, iso, normalize_text, parse_dt, text

logger = logging.getLogger("sports-games-hub.discovery")


def is_video_game_contaminated(value: object) -> bool:
    s = text(value).lower()
    return any(re.search(r"\b" + re.escape(term) + r"\b", s) for term in VIDEO_GAME_QUERY_PATTERNS)


def _dedupe(items: Iterable[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        url = text(item.get("url"))
        title = text(item.get("title"))
        if not url or not title:
            continue
        key = canonical_url(url)
        if not key or key in seen:
            continue
        combined = " ".join([title, text(item.get("excerpt")), url])
        if is_video_game_contaminated(combined):
            continue
        seen.add(key)
        dt = parse_dt(item.get("published_date"))
        out.append({
            **item,
            "url": url,
            "canonical": key,
            "title": re.sub(r"\s+", " ", title).strip(),
            "excerpt": re.sub(r"\s+", " ", text(item.get("excerpt"))).strip(),
            "published_date": iso(dt),
        })
    return out


def day_window(target: date, tz: ZoneInfo = CONFIG.tz) -> tuple[datetime, datetime]:
    start = datetime.combine(target, dtime.min, tzinfo=tz)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def discover_next_sports(providers: Providers, target: date) -> list[dict]:
    start, end = day_window(target)
    label = target.strftime("%d %B %Y")
    queries = [
        f"sports fixtures {label} major matches tournaments finals races championships",
        f"sports events {label} schedule official",
        f"{label} football fixtures matches schedule",
        f"{label} cricket fixtures schedule matches",
        f"{label} tennis matches tournament schedule",
        f"{label} badminton tournament schedule matches",
        f"{label} basketball games schedule",
        f"{label} formula 1 motorsport race schedule",
        f"{label} athletics swimming cycling rowing sports events",
        f"{label} rugby hockey volleyball handball sports schedule",
        f"{label} combat sports boxing mma judo karate events",
        f"{label} snooker darts squash bowling events",
        f"{label} kabaddi kho kho sepak takraw events",
    ]
    return _dedupe(_search_many(providers, queries, start=start, end=end, num=MAX_EXA_RESULTS))


def discover_past_sports(providers: Providers, target: date) -> list[dict]:
    start, end = day_window(target)
    label = target.strftime("%d %B %Y")
    queries = [
        f"sports results {label} major results championships finals records",
        f"sports {label} results football cricket tennis badminton basketball",
        f"{label} football results",
        f"{label} cricket results scorecard",
        f"{label} tennis results finals",
        f"{label} badminton results finals",
        f"{label} basketball results",
        f"{label} formula 1 motorsport results",
        f"{label} athletics swimming cycling records results",
        f"{label} rugby hockey volleyball handball results",
        f"{label} boxing mma judo karate results",
        f"{label} snooker darts squash bowling results",
        f"{label} kabaddi kho kho sepak takraw results",
    ]
    return _dedupe(_search_many(providers, queries, start=start, end=end, num=MAX_EXA_RESULTS))


def discover_historical_date(providers: Providers, target: date) -> list[dict]:
    out = []
    for offset in (25, 50, 75, 100, 125):
        year = target.year - offset
        try:
            historical = target.replace(year=year)
        except ValueError:
            historical = target.replace(year=year, day=28)
        start = datetime(historical.year, historical.month, 1, tzinfo=timezone.utc)
        next_month = historical.month % 12 + 1
        next_year = historical.year + (1 if historical.month == 12 else 0)
        end = datetime(next_year, next_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        query = f"sports games history {historical.strftime('%d %B %Y')} on this date first last record championship unusual"
        out.extend(_search_many(providers, [query], start=start, end=end, num=MAX_EXA_RESULTS))
    out.extend(_search_many(providers, [
        f'"{target.strftime("%d %B")}" sports history on this date games Olympics records',
        f'"{target.strftime("%d %B")}" board game history card game history',
    ], num=10))
    return _dedupe(out)


def build_evergreen_queries() -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    game_pools = [
        ("board", BOARD_GAMES), ("card", CARD_GAMES), ("party", PARTY_GAMES),
        ("traditional", TRADITIONAL_GAMES), ("mind", MIND_GAMES), ("sport", SPORTS),
    ]
    for bucket, pool in game_pools:
        for item in pool:
            queries.append((bucket, f'{item} "official rules" history origin unusual fact'))
            queries.append((bucket, f'{item} why called origin oldest first rule history'))
    for pattern in SURPRISE_PATTERNS:
        queries.append(("surprise", f'"{pattern}" sports games board card traditional'))
    queries.extend([
        ("new_board_game", "new board game 2026 announced physical tabletop"),
        ("new_card_game", "new card game 2026 announced tabletop"),
        ("new_tabletop_game", "new tabletop game 2026 physical game announced"),
        ("new_sport", "new sport invented 2025 2026 physical sport rules competition"),
        ("forgotten_game", "forgotten traditional games around the world history"),
        ("forgotten_sport", "forgotten sports no longer popular history"),
        ("rule_change", "sports rule change 2026 official rules"),
        ("game_rule", "board card game rule clarification official rulebook"),
    ])
    return queries


def discover_evergreen(providers: Providers, day_index: int) -> list[dict]:
    queries = build_evergreen_queries()
    if not queries:
        return []
    # Deterministic rotation with a stride avoids repeatedly hammering the same topics.
    stride = max(1, len(queries) // max(1, SEARCHES_PER_RUN))
    start = (day_index * stride) % len(queries)
    selected = [queries[(start + i * stride) % len(queries)] for i in range(min(SEARCHES_PER_RUN, len(queries)))]
    out = []
    for bucket, query in selected:
        rows = providers.exa_search(query, num=5)
        for row in rows:
            row["query_bucket"] = bucket
        out.extend(rows)
    return _dedupe(out)[:MAX_CANDIDATES]


def _search_many(providers: Providers, queries: list[str], *, start=None, end=None, num=8) -> list[dict]:
    out: list[dict] = []
    for query in queries:
        out.extend(providers.exa_search(query, start=start, end=end, num=num))
    return out
