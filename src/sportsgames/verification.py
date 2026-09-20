from __future__ import annotations

import logging
import re
from typing import Iterable

from .discovery import is_video_game_contaminated
from .providers import Providers, fetch_article, source_tier
from .schemas import VERIFY_SCHEMA, STORY_FACTCHECK_SCHEMA
from .state import claim_key, core_claim_key
from .utils import domain_of, normalize_text, similarity, text

logger = logging.getLogger("sports-games-hub.verification")


def independent_domains(urls: Iterable[str]) -> set[str]:
    return {domain_of(url) for url in urls if domain_of(url)}


def _extra_source_query(candidate: dict) -> str:
    return f"{candidate.get('subject', '')} {candidate.get('claim_or_event', '')} official history rules".strip()


def verify_candidate(providers: Providers, candidate: dict, lane: str = "discovery") -> tuple[bool, dict]:
    urls = list(dict.fromkeys(text(url) for url in candidate.get("source_urls", []) if text(url)))
    excerpts = {text(candidate.get("url")): text(candidate.get("excerpt"))}
    domains = independent_domains(urls)

    if not any(source_tier(url) == 1 for url in urls) or len(domains) < 2:
        extras = providers.exa_search(_extra_source_query(candidate), num=6, family="verification")
        for extra in extras:
            url = text(extra.get("url"))
            if not url or domain_of(url) in domains or source_tier(url) == 4:
                continue
            if is_video_game_contaminated(f"{extra.get('title', '')} {extra.get('excerpt', '')}"):
                continue
            urls.append(url)
            excerpts[url] = text(extra.get("excerpt"))
            domains.add(domain_of(url))
            if len(urls) >= 5:
                break
    candidate["source_urls"] = urls[:5]

    evidence_packets = []
    for url in candidate["source_urls"]:
        article = fetch_article(url, fallback_excerpt=excerpts.get(url, ""), report=providers.report)
        if article.get("text"):
            evidence_packets.append({
                "url": url,
                "tier": source_tier(url),
                "source": candidate.get("source") or domain_of(url),
                "text": article["text"][:8500],
                "fallback": bool(article.get("fallback")),
            })
    candidate["evidence_packets"] = evidence_packets

    if not evidence_packets:
        return False, {"status": "unverified", "confidence": 0, "reason": "No source evidence available."}

    system = """
You are the strict verification editor for The Sports Newsroom.
Verify the candidate from the supplied evidence only.
A known primary/official source can establish a fact by itself when its evidence directly supports it.
Otherwise require at least two genuinely independent domains and do not count lead-only sources.
Check every date, number, name, origin, rule, record, and causal/historical statement.
A source being plausible is not evidence.
If sources conflict, mark disputed and explain the conflict.
Never use model memory to fill missing details.
Return only JSON.
""".strip()
    user = (
        f"CANDIDATE\nSubject: {candidate.get('subject')}\n"
        f"Claim/Event: {candidate.get('claim_or_event')}\n"
        f"Angle: {candidate.get('angle')}\n\n"
        + "\n\n".join(
            f"SOURCE: {packet['url']}\nTIER: {packet['tier']}\nEVIDENCE:\n{packet['text']}"
            for packet in evidence_packets
        )
    )
    try:
        result = providers.ai(
            system=system,
            user=user,
            schema_name="candidate_verification_v3",
            schema=VERIFY_SCHEMA,
            max_tokens=1800,
            lane=lane,
        )
    except Exception as exc:
        logger.warning("Candidate verification failed: %s", exc)
        return False, {"status": "unverified", "confidence": 0, "reason": str(exc)}

    status = text(result.get("status"))
    confidence = int(result.get("confidence", 0) or 0)
    has_primary = any(source_tier(url) == 1 for url in candidate["source_urls"])
    domain_count = len(independent_domains(candidate["source_urls"]))
    has_lead_only = any(source_tier(url) == 4 for url in candidate["source_urls"])
    hard_ok = (
        status == "verified"
        and confidence >= 80
        and (has_primary or (domain_count >= 2 and not has_lead_only))
        and not is_video_game_contaminated(" ".join(map(text, [candidate.get("subject"), candidate.get("claim_or_event")])) )
    )
    if (
        status == "disputed"
        and text(candidate.get("angle")) in {"myth", "rule", "house_rule", "origin", "banned"}
        and confidence >= 85
        and domain_count >= 2
    ):
        hard_ok = True
    return hard_ok, result


def verify_story_against_evidence(providers: Providers, story: dict, candidate: dict, lane: str = "discovery") -> bool:
    packets = candidate.get("evidence_packets") or []
    if not packets:
        return False
    evidence = "\n\n".join(
        f"SOURCE: {packet.get('url')}\nTIER: {packet.get('tier')}\nTEXT:\n{packet.get('text', '')[:8000]}"
        for packet in packets
    )
    system = """
You are the final factual grounding checker for The Sports Newsroom.
PASS only when every substantive factual statement in the generated post is supported by the supplied evidence.
Reject invented dates, numbers, names, rules, origins, records, causal explanations, superlatives and unsupported specifics.
A concise structural transition is allowed.
Return only JSON.
""".strip()
    user = (
        f"CANDIDATE: {candidate.get('claim_or_event')}\n"
        f"GENERATED:\nHeadline: {story.get('headline')}\n"
        f"Dek: {story.get('dek')}\n"
        f"Body: {story.get('body')}\n"
        f"Why: {story.get('why_interesting')}\n"
        f"Key points: {story.get('key_points')}\n\n{evidence}"
    )
    try:
        result = providers.ai(
            system=system,
            user=user,
            schema_name="story_factcheck_v3",
            schema=STORY_FACTCHECK_SCHEMA,
            max_tokens=1200,
            lane=lane,
        )
    except Exception as exc:
        logger.warning("Story factcheck failed: %s", exc)
        return False
    return result.get("status") == "pass"


def deterministic_story_checks(story: dict, source_text: str) -> tuple[bool, str]:
    fields = " ".join([
        text(story.get("headline")),
        text(story.get("dek")),
        text(story.get("body")),
        text(story.get("why_interesting")),
        " ".join(map(text, story.get("key_points", []))),
    ])
    if is_video_game_contaminated(fields):
        return False, "video_game_contamination"
    source_digits = set(re.findall(r"\d{3,}", source_text))
    output_digits = set(re.findall(r"\d{3,}", fields))
    missing = sorted(value for value in output_digits if value not in source_digits)
    if missing:
        return False, f"unsupported_numeric_tokens:{','.join(missing[:5])}"
    if re.search(r"\b(today|yesterday|tomorrow|tonight|latest)\b", fields.lower()) and text(story.get("format")) not in {"daily_next", "daily_past"}:
        return False, "disposable_time_language"
    return True, "ok"


def is_claim_duplicate(state: dict, candidate: dict) -> bool:
    subject = text(candidate.get("subject")) or text(candidate.get("game_or_sport"))
    claim = text(candidate.get("claim_or_event"))
    angle = text(candidate.get("angle"))
    if not subject or not claim:
        return False
    claims = state.get("claims", {})
    if core_claim_key(subject, claim) in claims or claim_key(subject, claim, angle) in claims:
        return True
    normalized_subject = normalize_text(subject)
    for old in claims.values():
        if normalize_text(old.get("subject")) != normalized_subject:
            continue
        old_claim = text(old.get("claim"))
        similarity_score = similarity(old_claim, claim)
        if similarity_score >= 0.84:
            return True
        if similarity_score >= 0.72 and normalize_text(old.get("angle")) == normalize_text(angle):
            return True
    return False
