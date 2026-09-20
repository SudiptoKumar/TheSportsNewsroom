from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import CONFIG, MAX_DISCOVERY_POSTS_PER_RUN, MAX_DISCOVERY_POSTS_PER_DAY, MAX_EDITORIAL_SHORTLIST
from .discovery import is_video_game_contaminated
from .providers import Providers
from .schemas import CANDIDATE_SCHEMA, EDITORIAL_SCHEMA
from .state import discovery_posts_today
from .taxonomy import ANGLES, CATEGORIES
from .utils import normalize_text, text
from .verification import is_claim_duplicate

logger = logging.getLogger("sports-games-hub.editorial")


def classify_candidates(providers: Providers, candidates: list[dict], mode: str) -> list[dict]:
    blocks = []
    limited = candidates[:70]
    for idx, item in enumerate(limited, 1):
        blocks.append(
            f"ID: {idx}\nTITLE: {item.get('title')}\nSOURCE: {item.get('source')}\nURL: {item.get('url')}\nEXCERPT: {item.get('excerpt','')[:1400]}"
        )
    if not blocks:
        return []
    system = f"""
You are the discovery classifier for The Sports Newsroom.
Mode: {mode}.
Cover ONLY real-world sports and physical games: board, card, tabletop, party, mind and traditional games.
Never return video games, esports, consoles, gaming hardware or gaming-industry news.
Identify the smallest defensible factual claim/event supported by the candidate source.
Choose a category and angle that accurately describe the content. Do not invent details.
For new-game discovery, a physical game's announcement/release is allowed. The purpose is discovery, not video-game coverage.
Use an empty date_anchor unless the candidate genuinely refers to a specific historical/current event date.
Return only JSON.
""".strip()
    try:
        data = providers.ai(system=system, user="\n\n".join(blocks), schema_name="candidate_classifier_v2", schema=CANDIDATE_SCHEMA, max_tokens=3200)
    except Exception as exc:
        logger.warning("Candidate classification failed: %s", exc)
        return []
    by_id = {i + 1: c for i, c in enumerate(limited)}
    result = []
    for row in data.get("items", []):
        try:
            original = by_id[int(row.get("id", 0))]
        except Exception:
            continue
        merged = {**original, **{k: row.get(k) for k in row if k != "id"}}
        if merged.get("category") not in CATEGORIES:
            merged["category"] = "evergreen_fact"
        if merged.get("angle") not in ANGLES:
            merged["angle"] = "discovery"
        if is_video_game_contaminated(str(merged)):
            continue
        if not merged.get("source_urls"):
            merged["source_urls"] = [original.get("url", "")]
        result.append(merged)
    return result


def score_candidate(candidate: dict, verification: dict, state: dict) -> tuple[int, dict]:
    status = text(verification.get("status"))
    confidence = int(verification.get("confidence", 0) or 0)
    verified = 5 if status == "verified" and confidence >= 90 else 4 if status == "verified" else 3
    surprise = 5 if text(candidate.get("why_interesting")) else 2
    category = text(candidate.get("category"))
    evergreen = 5 if category not in {"sports_daily_next", "sports_daily_past"} else 2
    novelty = 5 if not is_claim_duplicate(state, candidate) else 0
    clarity = 5 if len(text(candidate.get("claim_or_event"))) <= 180 else 4
    curiosity_angles = {"origin", "etymology", "why", "weird", "myth", "only", "first", "last", "oldest", "rare", "rule", "house_rule", "accident"}
    curiosity = 5 if text(candidate.get("angle")) in curiosity_angles else 3
    shareability = 5 if category in {"game_discovery", "new_board_game", "new_card_game", "rule_check", "why_explained", "evergreen_fact", "on_this_date", "century_ago"} else 4

    recent_categories = [text(x.get("category")) for x in state.get("category_history", [])[-8:]]
    recent_angles = [text(x.get("angle")) for x in state.get("angle_history", [])[-10:]]
    subjects = {normalize_text(x.get("subject")) for x in state.get("subject_history", [])[-25:]}
    penalty = 0
    if recent_categories and recent_categories[-1] == category: penalty += 4
    if recent_angles[-1:] == [text(candidate.get("angle"))]: penalty += 3
    if recent_angles.count(text(candidate.get("angle"))) >= 3: penalty += 6
    if normalize_text(candidate.get("subject")) in subjects: penalty += 6
    score = surprise + verified + evergreen + novelty + clarity + curiosity + shareability - penalty
    return score, {
        "surprise": surprise, "verification": verified, "evergreen": evergreen,
        "novelty": novelty, "clarity": clarity, "curiosity": curiosity,
        "shareability": shareability, "penalty": penalty, "total": score,
    }


def select_candidates(providers: Providers, candidates: list[dict], state: dict, max_items: int | None = None) -> list[dict]:
    remaining = max(0, MAX_DISCOVERY_POSTS_PER_DAY - discovery_posts_today(state))
    limit = min(MAX_DISCOVERY_POSTS_PER_RUN, remaining) if max_items is None else min(max_items, remaining)
    if limit <= 0:
        return []

    prepared = []
    for c in candidates:
        verification = c.get("verification", {})
        if verification.get("status") not in {"verified", "disputed"}:
            continue
        if is_claim_duplicate(state, c):
            continue
        score, breakdown = score_candidate(c, verification, state)
        if score < 22:
            continue
        prepared.append({**c, "editor_score": score, "score_breakdown": breakdown})
    prepared.sort(key=lambda x: (x["editor_score"], int(x.get("verification", {}).get("confidence", 0))), reverse=True)
    shortlist = prepared[:MAX_EDITORIAL_SHORTLIST]
    if not shortlist:
        return []

    blocks = []
    for i, c in enumerate(shortlist, 1):
        blocks.append(
            f"ID: {i}\nCATEGORY: {c.get('category')}\nANGLE: {c.get('angle')}\nSUBJECT: {c.get('subject')}\nCLAIM: {c.get('claim_or_event')}\nWHY: {c.get('why_interesting')}\nSCORE: {c.get('editor_score')}"
        )
    system = """
You are the final editor for The Sports Newsroom.
Select only high-value, verified candidates that will interest readers years after publication.
Prefer variety across content type, sport/game, country/region and angle.
Do not select near-duplicates.
Do not select anything about video games or esports.
Do not optimize for quantity. Return only JSON.
""".strip()
    try:
        result = providers.ai(system=system, user="\n\n".join(blocks), schema_name="editorial_selection_v2", schema=EDITORIAL_SCHEMA, max_tokens=1800)
        selected_ids = [int(x) for x in result.get("selected_ids", [])]
    except Exception as exc:
        logger.warning("Editorial AI unavailable, using deterministic selection: %s", exc)
        selected_ids = list(range(1, min(limit, len(shortlist)) + 1))

    output = []
    used_categories: set[str] = set()
    used_angles: set[str] = set()
    used_subjects: set[str] = set()
    for sid in selected_ids:
        if len(output) >= limit or sid < 1 or sid > len(shortlist):
            break
        c = shortlist[sid - 1]
        cat, angle, subject = text(c.get("category")), text(c.get("angle")), normalize_text(c.get("subject"))
        if subject in used_subjects:
            continue
        # Avoid using the exact same category repeatedly when alternatives exist.
        if cat in used_categories and len(shortlist) >= limit * 2:
            continue
        if angle in used_angles and len(shortlist) >= limit * 2:
            continue
        used_categories.add(cat); used_angles.add(angle); used_subjects.add(subject)
        output.append(c)
    return output
