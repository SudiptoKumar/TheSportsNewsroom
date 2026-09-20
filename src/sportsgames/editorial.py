from __future__ import annotations

import logging
from collections import Counter

from .config import MAX_CLASSIFICATION_CANDIDATES, MAX_DISCOVERY_POSTS_PER_DAY, MAX_DISCOVERY_POSTS_PER_RUN
from .discovery import is_video_game_contaminated
from .providers import Providers
from .schemas import CANDIDATE_SCHEMA
from .state import discovery_posts_today, record_candidate
from .taxonomy import ANGLES, CATEGORIES
from .utils import normalize_text, text
from .verification import is_claim_duplicate, verify_candidate

logger = logging.getLogger("sports-games-hub.editorial")


def classify_candidates(providers: Providers, candidates: list[dict], mode: str, report=None) -> list[dict]:
    if not candidates:
        if report:
            report.count("classification.input", 0)
        return []
    candidates = sorted(candidates, key=lambda x: float(x.get("prelim_score", 0)), reverse=True)[:MAX_CLASSIFICATION_CANDIDATES]
    if report:
        report.count("classification.input", len(candidates))
    blocks = []
    for idx, item in enumerate(candidates, 1):
        blocks.append(
            f"ID: {idx}\n"
            f"FAMILY: {item.get('query_family')}\n"
            f"TITLE: {item.get('title')}\n"
            f"SOURCE: {item.get('source')}\n"
            f"URL: {item.get('url')}\n"
            f"EXCERPT: {item.get('excerpt','')[:1500]}"
        )
    system = f"""
You are the discovery classifier for The Sports Newsroom.
Mode: {mode}.
Cover ONLY real-world sports and physical games: board, card, tabletop, party, mind, recreational and traditional games.
Never return video games, esports, consoles, gaming hardware or gaming-industry news.
Identify the smallest defensible factual claim/event supported by the candidate evidence.
Choose a category and angle that accurately describe the candidate. Do not invent details.
A new physical board/card/tabletop/party game is valid discovery content.
Use an empty date_anchor unless the candidate genuinely refers to a specific date.
Return only JSON.
""".strip()
    try:
        data = providers.ai(
            system=system,
            user="\n\n".join(blocks),
            schema_name="candidate_classifier_v3",
            schema=CANDIDATE_SCHEMA,
            max_tokens=3000,
            lane="discovery",
        )
    except Exception as exc:
        logger.warning("Candidate classification failed: %s", exc)
        return []
    by_id = {i + 1: c for i, c in enumerate(candidates)}
    result = []
    seen_ids = set()
    for row in data.get("items", []):
        try:
            item_id = int(row.get("id", 0))
            original = by_id[item_id]
            seen_ids.add(item_id)
        except Exception:
            if report:
                report.reject("classification", "invalid_candidate_id")
            continue
        merged = {**original, **{k: row.get(k) for k in row if k != "id"}}
        if merged.get("category") not in CATEGORIES:
            if report:
                report.reject("classification", "invalid_category", text(original.get("title")), candidate_id=text(original.get("candidate_id")))
            continue
        if merged.get("angle") not in ANGLES:
            if report:
                report.reject("classification", "invalid_angle", text(original.get("title")), candidate_id=text(original.get("candidate_id")))
            continue
        if is_video_game_contaminated(str(merged)):
            if report:
                report.reject("classification", "video_game", text(original.get("title")), candidate_id=text(original.get("candidate_id")))
            continue
        if not merged.get("source_urls"):
            merged["source_urls"] = [original.get("url", "")]
        result.append(merged)
    if report:
        report.count("classification.output", len(result))
        report.count("classification.unreturned", max(0, len(candidates) - len(seen_ids)))
    return result


def score_candidate(candidate: dict, verification: dict, state: dict) -> tuple[float, dict]:
    status = text(verification.get("status"))
    confidence = int(verification.get("confidence", 0) or 0)
    verified = 5.0 if status == "verified" and confidence >= 90 else 4.0 if status == "verified" else 3.0
    category = text(candidate.get("category"))
    angle = text(candidate.get("angle"))
    evergreen = 5.0 if category not in {"sports_daily_next", "sports_daily_past"} else 2.0
    evidence_strength = 5.0 if any(int(p.get("tier", 3)) == 1 for p in candidate.get("evidence_packets", [])) else 4.0
    curiosity_angles = {
        "origin", "etymology", "why", "weird", "myth", "only", "first", "last", "oldest",
        "rare", "rule", "house_rule", "accident", "new_mechanic",
    }
    surprise = 5.0 if angle in curiosity_angles else 3.5
    clarity = 5.0 if len(text(candidate.get("claim_or_event"))) <= 180 else 4.0
    usefulness = 5.0 if category in {
        "rule_check", "how_to_play", "game_discovery", "game_origin", "sport_origin",
        "why_explained", "game_anatomy",
    } else 4.0
    novelty = 5.0 if not is_claim_duplicate(state, candidate) else 0.0

    recent_categories = [text(x.get("category")) for x in state.get("category_history", [])[-8:]]
    recent_angles = [text(x.get("angle")) for x in state.get("angle_history", [])[-10:]]
    recent_subjects = {normalize_text(x.get("subject")) for x in state.get("subject_history", [])[-20:]}
    subject = normalize_text(candidate.get("subject"))
    penalty = 0.0
    if recent_categories and recent_categories[-1] == category:
        penalty += 3.0
    if recent_angles and recent_angles[-1] == angle:
        penalty += 2.5
    if recent_angles.count(angle) >= 3:
        penalty += 5.0
    if subject and subject in recent_subjects:
        penalty += 4.0
    score = (
        verified + evidence_strength + evergreen + surprise + clarity + usefulness + novelty
        + float(candidate.get("prelim_score", 0)) - penalty
    )
    return score, {
        "verified": verified,
        "evidence": evidence_strength,
        "evergreen": evergreen,
        "surprise": surprise,
        "clarity": clarity,
        "usefulness": usefulness,
        "novelty": novelty,
        "prelim": float(candidate.get("prelim_score", 0)),
        "penalty": penalty,
        "total": score,
    }


def select_candidates(providers: Providers, candidates: list[dict], state: dict, max_items: int | None = None) -> list[dict]:
    remaining = max(0, MAX_DISCOVERY_POSTS_PER_DAY - discovery_posts_today(state))
    limit = min(MAX_DISCOVERY_POSTS_PER_RUN, remaining) if max_items is None else min(max_items, remaining)
    if limit <= 0:
        return []

    prepared = []
    for candidate in candidates:
        verification = candidate.get("verification", {})
        if verification.get("status") not in {"verified", "disputed"}:
            record_candidate(state, candidate, "rejected", "not_verified")
            continue
        if is_claim_duplicate(state, candidate):
            record_candidate(state, candidate, "rejected", "duplicate_claim")
            continue
        score, breakdown = score_candidate(candidate, verification, state)
        if score < 20:
            record_candidate(state, candidate, "rejected", "below_quality_gate", score)
            continue
        prepared.append({**candidate, "editor_score": score, "score_breakdown": breakdown})

    prepared.sort(key=lambda x: (x["editor_score"], int(x.get("verification", {}).get("confidence", 0))), reverse=True)
    shortlist = prepared[:max(1, limit * 3)]
    if not shortlist:
        return []

    output = []
    used_categories = Counter()
    used_angles = Counter()
    used_subjects: set[str] = set()
    used_families = Counter()
    pool = list(shortlist)
    for _ in range(limit):
        if not pool:
            break
        best = None
        best_adjusted = None
        for candidate in pool:
            category = text(candidate.get("category"))
            angle = text(candidate.get("angle"))
            subject = normalize_text(candidate.get("subject"))
            family = text(candidate.get("query_family"))
            adjusted = float(candidate["editor_score"])
            adjusted -= 2.5 * used_categories[category]
            adjusted -= 2.0 * used_angles[angle]
            adjusted -= 1.5 * used_families[family]
            if subject in used_subjects:
                adjusted -= 20
            if best_adjusted is None or adjusted > best_adjusted:
                best_adjusted = adjusted
                best = candidate
        if best is None:
            break
        output.append(best)
        pool.remove(best)
        used_categories[text(best.get("category"))] += 1
        used_angles[text(best.get("angle"))] += 1
        used_families[text(best.get("query_family"))] += 1
        used_subjects.add(normalize_text(best.get("subject")))
        record_candidate(state, best, "selected", "editorial_selection", best.get("editor_score"))
    return output


def verify_shortlist(providers: Providers, candidates: list[dict], state: dict, max_items: int = 3, report=None) -> list[dict]:
    from .observability import balanced_pool

    quotas = {
        "facts": 1, "games": 1, "rules": 1, "history": 1,
        "games_new": 1, "discovery": 1, "sports": 1,
        "on_this_date": 1, "century_ago": 1,
    }
    pool = balanced_pool(
        candidates,
        lambda c: text(c.get("query_family")),
        lambda c: float(c.get("prelim_score", 0)),
        quotas,
        max_items,
    )
    if report:
        report.count("verification.pool", len(pool))
    verified = []
    for candidate in pool:
        ok, verification = verify_candidate(providers, candidate)
        candidate["verification"] = verification
        if ok:
            verified.append(candidate)
            record_candidate(
                state,
                candidate,
                "verified",
                verification.get("status", ""),
                float(verification.get("confidence", 0)),
            )
        else:
            reason = text(verification.get("reason")) or text(verification.get("status")) or "verification_failed"
            if report:
                report.reject("verification", reason[:80], text(candidate.get("title")), candidate_id=text(candidate.get("candidate_id")))
            record_candidate(
                state,
                candidate,
                "rejected",
                reason,
                float(verification.get("confidence", 0)),
            )
    return verified
