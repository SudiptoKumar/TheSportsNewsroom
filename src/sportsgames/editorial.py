from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import CONFIG, ENTITY_30D_SOFT_CAP, MAX_DISCOVERY_POSTS_PER_DAY, MAX_DISCOVERY_POSTS_PER_RUN, MAX_EDITORIAL_SHORTLIST
from .discovery import is_video_game_contaminated
from .schemas import CANDIDATE_SCHEMA
from .state import discovery_posts_today
from .taxonomy import ANGLES, CATEGORIES
from .utils import normalize_text, text
from .verification import is_claim_duplicate

logger = logging.getLogger("sports-games-hub.editorial")


def classify_candidates(providers, candidates: list[dict], mode: str) -> list[dict]:
    limited = candidates[:50]
    blocks = []
    for idx, item in enumerate(limited, 1):
        blocks.append(
            f"ID: {idx}\nTITLE: {item.get('title')}\nSOURCE: {item.get('source')}\nURL: {item.get('url')}\n"
            f"SOURCE TIER: {item.get('source_tier', 5)}\nEXCERPT: {item.get('excerpt','')[:1500]}"
        )
    if not blocks:
        return []
    system = f"""
You are the discovery classifier for The Sports Newsroom.
Mode: {mode}.
Cover ONLY real-world sports and physical games: board, card, tabletop, party, mind and traditional games.
NEVER classify video games, esports, consoles, Steam, patches, DLC or gaming hardware as valid.
Identify the smallest defensible factual claim/event supported by the candidate source excerpt.
For new-game discovery, physical game announcements/releases are allowed.
Use an exact date_anchor only when the source clearly gives a date.
Score each candidate on: surprise, evergreen_fit, simplicity, curiosity, novelty_signal, usefulness (0-5).
A high score must be earned by the evidence and not by generic wording.
Return only JSON.
""".strip()
    try:
        data = providers.ai(system=system, user="\n\n".join(blocks), schema_name="candidate_classifier_v3", schema=CANDIDATE_SCHEMA, max_tokens=3600)
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
        if is_video_game_contaminated(" ".join(map(text, [merged.get("title"), merged.get("excerpt"), merged.get("claim_or_event"), merged.get("url")]))):
            continue
        if not merged.get("source_urls"):
            merged["source_urls"] = [original.get("url", "")]
        # Preserve the search-result evidence so blocked pages can still be verified from Exa's excerpt.
        merged["source_records"] = [{
            "url": original.get("url", ""),
            "canonical": original.get("canonical", ""),
            "title": original.get("title", ""),
            "excerpt": original.get("excerpt", ""),
            "source_tier": original.get("source_tier", 5),
        }]
        merged["score_dimensions"] = {
            k: int(merged.get(k, 0) or 0)
            for k in ("surprise", "evergreen_fit", "simplicity", "curiosity", "novelty_signal", "usefulness")
        }
        result.append(merged)
    return result


def preliminary_score(candidate: dict, state: dict) -> int:
    dims = candidate.get("score_dimensions", {}) or {}
    base = sum(max(0, min(5, int(dims.get(k, 0) or 0))) for k in ("surprise", "evergreen_fit", "simplicity", "curiosity", "novelty_signal", "usefulness"))
    tier = int(candidate.get("source_tier", 5) or 5)
    source_bonus = 3 if tier == 1 else 2 if tier == 2 else 1 if tier == 3 else 0
    duplicate_penalty = 10 if is_claim_duplicate(state, candidate) else 0
    return base + source_bonus - duplicate_penalty


def score_candidate(candidate: dict, verification: dict, state: dict) -> tuple[int, dict]:
    dims = candidate.get("score_dimensions", {}) or {}
    verified = 5 if text(verification.get("status")) == "verified" and int(verification.get("confidence", 0) or 0) >= 90 else 4 if text(verification.get("status")) == "verified" else 3
    values = {
        "surprise": int(dims.get("surprise", 0) or 0),
        "evergreen": int(dims.get("evergreen_fit", 0) or 0),
        "simplicity": int(dims.get("simplicity", 0) or 0),
        "curiosity": int(dims.get("curiosity", 0) or 0),
        "novelty": 0 if is_claim_duplicate(state, candidate) else int(dims.get("novelty_signal", 0) or 0),
        "usefulness": int(dims.get("usefulness", 0) or 0),
        "verification": verified,
    }
    recent_categories = [text(x.get("category")) for x in state.get("category_history", [])[-8:]]
    recent_angles = [text(x.get("angle")) for x in state.get("angle_history", [])[-10:]]
    subjects = {normalize_text(x.get("subject")) for x in state.get("subject_history", [])[-25:]}
    penalty = 0
    category = text(candidate.get("category")); angle = text(candidate.get("angle")); subject = normalize_text(candidate.get("subject"))
    if recent_categories and recent_categories[-1] == category:
        penalty += 4
    if recent_angles[-1:] == [angle]:
        penalty += 3
    if recent_angles.count(angle) >= 3:
        penalty += 6
    if subject in subjects:
        penalty += 5
    score = sum(values.values()) - penalty
    return score, {**values, "penalty": penalty, "total": score}


def _entity_recent_posts(state: dict, entity: str) -> int:
    cutoff = datetime.now(CONFIG.tz) - timedelta(days=30)
    count = 0
    for p in state.get("posts", []):
        if normalize_text(p.get("game_or_sport")) != normalize_text(entity):
            continue
        raw = text(p.get("published_at"))
        try:
            when = datetime.fromisoformat(raw)
        except Exception:
            continue
        if when >= cutoff:
            count += 1
    return count


def select_candidates(candidates: list[dict], state: dict, max_items: int | None = None) -> list[dict]:
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
        if score < 21:
            continue
        prepared.append({**c, "editor_score": score, "score_breakdown": breakdown, "pre_score": preliminary_score(c, state)})
    prepared.sort(key=lambda x: (x["editor_score"], x["pre_score"], int(x.get("verification", {}).get("confidence", 0))), reverse=True)

    # Deterministic editorial selection avoids an extra Cerebras call and is intentionally diverse.
    output = []
    used_categories: set[str] = set()
    used_angles: set[str] = set()
    used_subjects: set[str] = set()
    for c in prepared[:MAX_EDITORIAL_SHORTLIST]:
        if len(output) >= limit:
            break
        cat = text(c.get("category")); angle = text(c.get("angle")); subject = normalize_text(c.get("subject")); entity = text(c.get("game_or_sport"))
        recent_entity = _entity_recent_posts(state, entity) if entity else 0
        if recent_entity >= ENTITY_30D_SOFT_CAP and len(prepared) > limit:
            continue
        if subject in used_subjects:
            continue
        if cat in used_categories and len(prepared) >= limit * 2:
            continue
        if angle in used_angles and len(prepared) >= limit * 2:
            continue
        used_categories.add(cat); used_angles.add(angle); used_subjects.add(subject)
        output.append(c)

    # If diversity filters were too strict, fill from the remainder by score.
    if len(output) < limit:
        selected_subjects = {normalize_text(x.get("subject")) for x in output}
        for c in prepared:
            if len(output) >= limit:
                break
            subject = normalize_text(c.get("subject"))
            if subject in selected_subjects:
                continue
            output.append(c)
            selected_subjects.add(subject)
    return output
