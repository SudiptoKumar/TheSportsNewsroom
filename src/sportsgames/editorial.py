from __future__ import annotations

import logging
from collections import Counter

from .config import MAX_CLASSIFICATION_BATCH_SIZE, MAX_CLASSIFICATION_CANDIDATES, MAX_DISCOVERY_POSTS_PER_DAY, MAX_DISCOVERY_POSTS_PER_RUN, MAX_VERIFICATION_CANDIDATES
from .discovery import is_video_game_contaminated
from .providers import Providers
from .schemas import CANDIDATE_SCHEMA
from .state import discovery_posts_today, record_candidate
from .taxonomy import ANGLES, CATEGORIES
from .utils import normalize_text, text
from .verification import is_claim_duplicate, verify_candidate

logger = logging.getLogger("sports-games-hub.editorial")


def _candidate_blocks(candidates: list[dict]) -> str:
    blocks = []
    for idx, item in enumerate(candidates, 1):
        blocks.append(
            f"ID: {idx}\n"
            f"FAMILY: {item.get('query_family')}\n"
            f"TITLE: {item.get('title')}\n"
            f"SOURCE: {item.get('source')}\n"
            f"URL: {item.get('url')}\n"
            f"EXCERPT: {item.get('excerpt', '')[:1800]}"
        )
    return "\n\n".join(blocks)


def _classify_batch(providers: Providers, batch: list[dict], mode: str) -> dict:
    system = f"""
You are the discovery classifier for The Sports Newsroom.
Mode: {mode}.
Classify ONLY real-world sports and physical games: board, card, tabletop, party, mind,
recreational and traditional games. Never classify video games, esports, consoles,
gaming hardware or gaming-industry news.
The fields domain and type are hard gates: use domain=sports for physical sporting activity,
domain=physical_games for board/card/tabletop/party/mind/traditional/recreational games,
and domain=other when the evidence is outside scope. Use type=other when no allowed
content type fits. Choose the smallest defensible factual claim/event supported by the
candidate evidence. Do not invent a second source or URL. source_urls MUST contain only
URLs present in the supplied candidate records. Return one item for each supplied ID when
possible. Return only JSON.
""".strip()
    from .schemas import CANDIDATE_SCHEMA
    return providers.ai(
        system=system,
        user=_candidate_blocks(batch),
        schema_name="candidate_classifier_v4",
        schema=CANDIDATE_SCHEMA,
        max_tokens=5000,
        lane="discovery",
    )


def _merge_classification_rows(batch: list[dict], data: dict, report=None, stage: str = "classification", state: dict | None = None) -> tuple[list[dict], set[str]]:
    by_id = {i + 1: c for i, c in enumerate(batch)}
    result: list[dict] = []
    seen_ids: set[int] = set()
    for row in data.get("items", []) if isinstance(data, dict) else []:
        try:
            item_id = int(row.get("id", 0))
            original = by_id[item_id]
        except Exception:
            if report:
                report.reject(stage, "invalid_candidate_id")
            continue
        if item_id in seen_ids:
            if report:
                report.reject(stage, "duplicate_returned_id", text(original.get("title")), candidate_id=text(original.get("candidate_id")))
            continue
        seen_ids.add(item_id)
        candidate_id = text(original.get("candidate_id"))
        merged = {**original, **{k: row.get(k) for k in row if k != "id"}}
        domain = text(merged.get("domain"))
        kind = text(merged.get("kind"))
        content_type = text(merged.get("type"))
        if domain == "other" or content_type == "other" or kind == "other":
            if report:
                report.reject(stage, "out_of_scope", text(original.get("title")), candidate_id=candidate_id)
                report.candidate(stage, candidate_id, "rejected", "out_of_scope")
                report.candidate_terminal(candidate_id, "rejected", "out_of_scope")
            if state is not None:
                record_candidate(state, original, "rejected", "out_of_scope")
            continue
        if merged.get("category") not in CATEGORIES:
            if report:
                report.reject(stage, "invalid_category", text(original.get("title")), candidate_id=candidate_id)
                report.candidate(stage, candidate_id, "rejected", "invalid_category")
                report.candidate_terminal(candidate_id, "rejected", "invalid_category")
            if state is not None:
                record_candidate(state, original, "rejected", "invalid_category")
            continue
        if merged.get("angle") not in ANGLES:
            if report:
                report.reject(stage, "invalid_angle", text(original.get("title")), candidate_id=candidate_id)
                report.candidate(stage, candidate_id, "rejected", "invalid_angle")
                report.candidate_terminal(candidate_id, "rejected", "invalid_angle")
            if state is not None:
                record_candidate(state, original, "rejected", "invalid_angle")
            continue
        if domain not in {"sports", "physical_games"}:
            if report:
                report.reject(stage, "invalid_domain", text(original.get("title")), candidate_id=candidate_id)
                report.candidate(stage, candidate_id, "rejected", "invalid_domain")
                report.candidate_terminal(candidate_id, "rejected", "invalid_domain")
            if state is not None:
                record_candidate(state, original, "rejected", "invalid_domain")
            continue
        if is_video_game_contaminated(" ".join(str(merged.get(k, "")) for k in ("title", "subject", "claim_or_event", "game_or_sport"))):
            if report:
                report.reject(stage, "video_game", text(original.get("title")), candidate_id=candidate_id)
                report.candidate(stage, candidate_id, "rejected", "video_game")
                report.candidate_terminal(candidate_id, "rejected", "video_game")
            if state is not None:
                record_candidate(state, original, "rejected", "video_game")
            continue

        # Never trust the model to introduce a source URL that was not retrieved by us.
        original_url = text(original.get("url"))
        supplied_urls = {original_url, text(original.get("canonical"))}
        returned_urls = [u for u in (merged.get("source_urls") or []) if text(u) in supplied_urls]
        merged["source_urls"] = [original_url] if original_url and original_url not in returned_urls else returned_urls[:1]
        if not merged["source_urls"]:
            merged["source_urls"] = [original_url] if original_url else []
        result.append(merged)
        if report:
            report.candidate(stage, candidate_id, "classified")
        if state is not None:
            record_candidate(state, merged, "classified", "classification")
    return result, {str(by_id[i].get("candidate_id")) for i in seen_ids if i in by_id}


def classify_candidates(providers: Providers, candidates: list[dict], mode: str, state: dict | None = None, report=None) -> list[dict]:
    if not candidates:
        if report:
            report.count("classification.input", 0)
        return []

    # First account for the full discovery list. The previous production failure silently
    # changed 100 candidates into 36 classification inputs. Here the 64 intentionally
    # excluded by the AI-input capacity receive an explicit classification_capacity reason.
    ordered_all = sorted(candidates, key=lambda x: float(x.get("prelim_score", 0)), reverse=True)
    candidate_ids = [text(item.get("candidate_id")) for item in ordered_all if text(item.get("candidate_id"))]
    if report:
        report.count("candidate.classification_pool_input", len(candidate_ids))
        for item in ordered_all:
            cid = text(item.get("candidate_id"))
            if cid:
                report.candidate("classification_pool", cid, "available")

    ordered = ordered_all[:MAX_CLASSIFICATION_CANDIDATES]
    overflow = ordered_all[MAX_CLASSIFICATION_CANDIDATES:]
    if report:
        report.count("classification.input", len(ordered))
        report.count("classification.capacity_rejected", len(overflow))
        for item in overflow:
            cid = text(item.get("candidate_id"))
            report.candidate("classification", cid, "rejected", "classification_capacity")
            report.candidate_terminal(cid, "rejected", "classification_capacity")
            report.reject("classification", "classification_capacity", text(item.get("title")), candidate_id=cid)
            if state is not None:
                record_candidate(state, item, "rejected", "classification_capacity")

    output: list[dict] = []
    for start in range(0, len(ordered), MAX_CLASSIFICATION_BATCH_SIZE):
        batch = ordered[start:start + MAX_CLASSIFICATION_BATCH_SIZE]
        try:
            data = _classify_batch(providers, batch, mode)
        except Exception as exc:
            logger.warning("Candidate classification batch failed size=%d: %s", len(batch), exc)
            for item in batch:
                cid = text(item.get("candidate_id"))
                if report:
                    report.reject("classification", "ai_call_failed", text(item.get("title")), str(exc), candidate_id=cid)
                    report.candidate("classification", cid, "rejected", "ai_call_failed")
                    report.candidate_terminal(cid, "rejected", "ai_call_failed")
                    if state is not None:
                        record_candidate(state, item, "rejected", "ai_call_failed")
            continue

        batch_output, returned_ids = _merge_classification_rows(batch, data, report=report, state=state)
        output.extend(batch_output)
        missing = [item for item in batch if text(item.get("candidate_id")) not in returned_ids]
        if missing:
            if report:
                report.count("classification.missing", len(missing))
            # Retry ONLY missing candidates, with a fresh batch-local 1..N ID space.
            try:
                retry_data = _classify_batch(providers, missing, mode)
            except Exception as exc:
                logger.warning("Candidate missing-only retry failed size=%d: %s", len(missing), exc)
                retry_data = {"items": []}
                for item in missing:
                    cid = text(item.get("candidate_id"))
                    if report:
                        report.reject("classification", "missing_retry_failed", text(item.get("title")), str(exc), candidate_id=cid)
                        report.candidate("classification", cid, "rejected", "missing_retry_failed")
                        report.candidate_terminal(cid, "rejected", "missing_retry_failed")
                        if state is not None:
                            record_candidate(state, item, "rejected", "missing_retry_failed")
            retry_output, retry_ids = _merge_classification_rows(missing, retry_data, report=report, state=state)
            output.extend(retry_output)
            returned_ids |= retry_ids
            for item in missing:
                cid = text(item.get("candidate_id"))
                if cid not in retry_ids:
                    if report:
                        report.reject("classification", "unreturned_after_retry", text(item.get("title")), candidate_id=cid)
                        report.candidate("classification", cid, "rejected", "unreturned_after_retry")
                        report.candidate_terminal(cid, "rejected", "unreturned_after_retry")
                    if state is not None:
                        record_candidate(state, item, "rejected", "unreturned_after_retry")

    # Final accounting gate for ALL candidates that were eligible for classification, not only
    # the first MAX_CLASSIFICATION_CANDIDATES. This is the direct regression protection for the
    # former 100 -> 36 silent loss.
    if report:
        missing = report.candidate_gate("classification", candidate_ids)
        report.count("classification.unreturned", len([cid for cid in missing if cid]))
        report.count("classification.output", len(output))
        report.count("classification.accounted", len(candidate_ids) - len(missing))
    return output


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


def select_candidates(providers: Providers, candidates: list[dict], state: dict, max_items: int | None = None, report=None) -> list[dict]:
    remaining = max(0, MAX_DISCOVERY_POSTS_PER_DAY - discovery_posts_today(state))
    limit = min(MAX_DISCOVERY_POSTS_PER_RUN, remaining) if max_items is None else min(max_items, remaining)
    if limit <= 0:
        return []

    prepared = []
    for candidate in candidates:
        verification = candidate.get("verification", {})
        cid = text(candidate.get("candidate_id"))
        if verification.get("status") not in {"verified", "disputed"}:
            record_candidate(state, candidate, "rejected", "not_verified")
            if report:
                report.candidate("editorial", cid, "rejected", "not_verified")
                report.candidate_terminal(cid, "rejected", "not_verified")
            continue
        if is_claim_duplicate(state, candidate):
            record_candidate(state, candidate, "rejected", "duplicate_claim")
            if report:
                report.candidate("editorial", cid, "rejected", "duplicate_claim")
                report.candidate_terminal(cid, "rejected", "duplicate_claim")
            continue
        score, breakdown = score_candidate(candidate, verification, state)
        if score < 20:
            record_candidate(state, candidate, "rejected", "below_quality_gate", score)
            if report:
                report.candidate("editorial", cid, "rejected", "below_quality_gate")
                report.candidate_terminal(cid, "rejected", "below_quality_gate")
            continue
        prepared.append({**candidate, "editor_score": score, "score_breakdown": breakdown})

    prepared.sort(key=lambda x: (x["editor_score"], int(x.get("verification", {}).get("confidence", 0))), reverse=True)
    shortlist = prepared[:max(1, limit * 3)]
    shortlist_ids = {text(c.get("candidate_id")) for c in shortlist}
    if report:
        for candidate in prepared:
            cid = text(candidate.get("candidate_id"))
            if cid and cid not in shortlist_ids:
                report.candidate("editorial", cid, "rejected", "editorial_pool_capacity")
                report.candidate_terminal(cid, "rejected", "editorial_pool_capacity")
                record_candidate(state, candidate, "rejected", "editorial_pool_capacity", candidate.get("editor_score"))
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
        if report:
            report.candidate("editorial", text(best.get("candidate_id")), "selected", "editorial_selection")
    selected_ids = {text(c.get("candidate_id")) for c in output}
    if report:
        for candidate in prepared:
            cid = text(candidate.get("candidate_id"))
            if cid not in selected_ids:
                report.candidate("editorial", cid, "rejected", "editorial_capacity")
                report.candidate_terminal(cid, "rejected", "editorial_capacity")
                record_candidate(state, candidate, "rejected", "editorial_capacity", candidate.get("editor_score"))
        report.candidate_gate("editorial", [text(c.get("candidate_id")) for c in candidates])
    return output


def verify_shortlist(providers: Providers, candidates: list[dict], state: dict, max_items: int = MAX_VERIFICATION_CANDIDATES, report=None) -> list[dict]:
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

    pool_ids = {text(c.get("candidate_id")) for c in pool}
    for candidate in candidates:
        cid = text(candidate.get("candidate_id"))
        if cid not in pool_ids and report:
            report.candidate("verification", cid, "rejected", "not_verification_pool")
            report.candidate_terminal(cid, "rejected", "not_verification_pool")
        if cid not in pool_ids:
            record_candidate(state, candidate, "rejected", "not_verification_pool")

    verified = []
    for candidate in pool:
        ok, verification = verify_candidate(providers, candidate)
        candidate["verification"] = verification
        cid = text(candidate.get("candidate_id"))
        if ok:
            verified.append(candidate)
            if report:
                report.candidate("verification", cid, "verified", text(verification.get("status")))
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
                report.reject("verification", reason[:80], text(candidate.get("title")), candidate_id=cid)
                report.candidate("verification", cid, "rejected", reason)
                report.candidate_terminal(cid, "rejected", reason)
            record_candidate(
                state,
                candidate,
                "rejected",
                reason,
                float(verification.get("confidence", 0)),
            )

    if report:
        report.candidate_gate("verification", [text(c.get("candidate_id")) for c in candidates])
    return verified

