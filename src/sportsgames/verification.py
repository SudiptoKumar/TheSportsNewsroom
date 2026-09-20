from __future__ import annotations

import logging
import re
from typing import Iterable

from .discovery import candidate_source_records, is_video_game_contaminated
from .providers import Providers, fetch_article, source_tier
from .schemas import STORY_FACTCHECK_SCHEMA, VERIFY_SCHEMA
from .state import claim_key
from .utils import domain_of, normalize_text, similarity, text

logger = logging.getLogger("sports-games-hub.verification")


def independent_domains(urls: Iterable[str]) -> set[str]:
    return {domain_of(u) for u in urls if domain_of(u)}


def build_evidence_packet(candidate: dict, *, max_sources: int = 4, max_chars_each: int = 7000) -> tuple[str, list[dict]]:
    """Collect reusable evidence once. Direct retrieval is optional when Exa supplied a useful excerpt."""
    records = []
    seen = set()
    for raw in candidate_source_records(candidate):
        url = text(raw.get("url"))
        if not url:
            continue
        canonical = text(raw.get("canonical")) or text(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        article = fetch_article(url, fallback_excerpt=text(raw.get("excerpt")))
        evidence_text = text(article.get("text"))
        if not evidence_text:
            continue
        records.append({
            "url": url,
            "title": text(raw.get("title")),
            "excerpt": text(raw.get("excerpt")),
            "evidence": evidence_text[:max_chars_each],
            "source_tier": int(raw.get("source_tier", source_tier(url)) or source_tier(url)),
            "direct_ok": bool(article.get("ok")),
            "blocked": bool(article.get("blocked")),
        })
        if len(records) >= max_sources:
            break
    blocks = [
        f"SOURCE: {r['url']}\nTIER: {r['source_tier']}\nDIRECT_FETCH: {r['direct_ok']}\nEVIDENCE:\n{r['evidence']}"
        for r in records
    ]
    return "\n\n".join(blocks), records


def verify_candidate(providers: Providers, candidate: dict) -> tuple[bool, dict]:
    records = candidate_source_records(candidate)
    urls = [text(r.get("url")) for r in records if text(r.get("url"))]
    domains = independent_domains(urls)

    # Only add corroborating searches when the candidate does not already have enough independent evidence.
    if not any(source_tier(u) == 1 for u in urls) or len(domains) < 2:
        query = f"{candidate.get('subject','')} {candidate.get('claim_or_event','')} official history rules".strip()
        for extra in providers.exa_search(query, num=5):
            u = text(extra.get("url"))
            if not u or domain_of(u) in domains or source_tier(u) == 4 or is_video_game_contaminated(f"{extra.get('title','')} {extra.get('excerpt','')} {u}"):
                continue
            records.append(extra)
            domains.add(domain_of(u))
            if len(records) >= 5:
                break
    candidate["source_records"] = records[:6]
    candidate["source_urls"] = list(dict.fromkeys(text(r.get("url")) for r in records if text(r.get("url"))))[:6]

    evidence_text, evidence_records = build_evidence_packet(candidate, max_sources=5)
    candidate["evidence_records"] = evidence_records
    if not evidence_records:
        return False, {"status": "unverified", "confidence": 0, "reason": "No usable source evidence."}

    system = """
You are the strict verification editor for The Sports Newsroom.
Verify the candidate from supplied source evidence only.
A primary/official source can verify a claim alone only when it directly supports the claim.
Otherwise require at least two genuinely independent credible domains.
Tier 4 lead-only sources and Tier 5 unknown sources cannot verify a claim by themselves.
Check every date, number, name, rule, origin, record and causal/historical statement.
If evidence conflicts, mark disputed. If evidence is absent, mark unverified.
Never use model memory to fill gaps.
Return only JSON.
""".strip()
    user = (
        f"CANDIDATE\nSubject: {candidate.get('subject')}\n"
        f"Claim/Event: {candidate.get('claim_or_event')}\nAngle: {candidate.get('angle')}\n\n"
        f"EVIDENCE\n{evidence_text}"
    )
    try:
        result = providers.ai(system=system, user=user, schema_name="candidate_verification_v3", schema=VERIFY_SCHEMA, max_tokens=1800)
    except Exception as exc:
        logger.warning("Candidate verification failed: %s", exc)
        return False, {"status": "unverified", "confidence": 0, "reason": str(exc)}
    status = text(result.get("status"))
    confidence = int(result.get("confidence", 0) or 0)
    source_tier_values = [int(r.get("source_tier", 5) or 5) for r in evidence_records]
    has_primary = any(t == 1 for t in source_tier_values)
    credible_domains = {
        domain_of(r.get("url", "")) for r in evidence_records if domain_of(r.get("url", "")) and int(r.get("source_tier", 5) or 5) <= 3
    }
    hard_ok = (
        status == "verified"
        and confidence >= 80
        and (has_primary or len(credible_domains) >= 2)
        and not is_video_game_contaminated(" ".join(map(text, [candidate.get("subject"), candidate.get("claim_or_event")])) )
    )
    if status == "disputed" and candidate.get("kind") in {"fact", "history", "rule"} and confidence >= 85 and len(credible_domains) >= 2:
        hard_ok = True
    return hard_ok, result


def verify_story_against_evidence(providers: Providers, story: dict, candidate: dict) -> tuple[bool, dict]:
    evidence_records = candidate.get("evidence_records") or []
    if not evidence_records:
        _, evidence_records = build_evidence_packet(candidate, max_sources=4)
    evidence = "\n\n".join(
        f"SOURCE: {r.get('url')}\nTIER: {r.get('source_tier')}\nEVIDENCE:\n{text(r.get('evidence'))[:7000]}"
        for r in evidence_records if text(r.get("evidence"))
    )
    if not evidence:
        return False, {"status": "fail", "unsupported_statements": ["No evidence"], "reason": "No evidence available."}
    system = """
You are the final factual grounding checker.
Compare the generated post against the supplied evidence packet.
PASS only if every substantive factual statement is supported by the evidence.
Do not allow invented dates, numbers, names, rules, origins, records, causal explanations or superlatives.
Do not infer from plausibility. Evidence must be present.
A stylistic transition is harmless, but new factual content is not.
Return only JSON.
""".strip()
    user = (
        f"CANDIDATE: {candidate.get('claim_or_event')}\n"
        f"GENERATED:\nHeadline: {story.get('headline')}\nDek: {story.get('dek')}\n"
        f"Body: {story.get('body')}\nWhy: {story.get('why_interesting')}\n"
        f"Key points: {story.get('key_points')}\n\nEVIDENCE:\n{evidence}"
    )
    try:
        result = providers.ai(system=system, user=user, schema_name="story_factcheck_v3", schema=STORY_FACTCHECK_SCHEMA, max_tokens=1200)
    except Exception as exc:
        return False, {"status": "fail", "unsupported_statements": [str(exc)], "reason": "Factcheck AI failed."}
    return result.get("status") == "pass", result


def deterministic_story_checks(story: dict, source_text: str) -> tuple[bool, str]:
    fields = " ".join([
        text(story.get("headline")), text(story.get("dek")), text(story.get("body")),
        text(story.get("why_interesting")), " ".join(map(text, story.get("key_points", []))),
    ])
    if is_video_game_contaminated(fields):
        return False, "video_game_contamination"
    # Ground 3+ digit numbers and years conservatively. Small numbers often arise from formatting (e.g. list counts).
    source_digits = set(re.findall(r"\b\d{3,4}\b", source_text))
    output_digits = set(re.findall(r"\b\d{3,4}\b", fields))
    missing = sorted(x for x in output_digits if x not in source_digits)
    if missing:
        return False, f"unsupported_numeric_tokens:{','.join(missing[:5])}"
    return True, "ok"


def is_claim_duplicate(state: dict, candidate: dict) -> bool:
    subject = text(candidate.get("subject")) or text(candidate.get("game_or_sport"))
    claim = text(candidate.get("claim_or_event"))
    angle = text(candidate.get("angle"))
    if not subject or not claim:
        return False
    key = claim_key(subject, claim, angle)
    if key in state.get("claims", {}):
        return True
    normalized_subject = normalize_text(subject)
    for old in state.get("claims", {}).values():
        if normalize_text(old.get("subject")) != normalized_subject:
            continue
        old_claim = text(old.get("claim"))
        if similarity(old_claim, claim) >= 0.86:
            return True
        if similarity(old_claim, claim) >= 0.75 and normalize_text(old.get("angle")) == normalize_text(angle):
            return True
    return False
