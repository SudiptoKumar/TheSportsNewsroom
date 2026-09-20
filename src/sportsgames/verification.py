from __future__ import annotations

import logging
import re
from typing import Iterable

from .discovery import is_video_game_contaminated
from .providers import Providers, fetch_article, source_tier
from .schemas import VERIFY_SCHEMA, STORY_FACTCHECK_SCHEMA
from .state import claim_key
from .utils import domain_of, normalize_text, similarity, text

logger = logging.getLogger("sports-games-hub.verification")


def independent_domains(urls: Iterable[str]) -> set[str]:
    return {domain_of(u) for u in urls if domain_of(u)}


def verify_candidate(providers: Providers, candidate: dict) -> tuple[bool, dict]:
    urls = list(dict.fromkeys(text(u) for u in candidate.get("source_urls", []) if text(u)))
    domains = independent_domains(urls)

    # Conservative corroboration: discover another domain when we do not already
    # have a known primary source plus enough evidence.
    if not any(source_tier(u) == 1 for u in urls) or len(domains) < 2:
        query = f"{candidate.get('subject','')} {candidate.get('claim_or_event','')} official history rules".strip()
        for extra in providers.exa_search(query, num=6):
            u = text(extra.get("url"))
            if not u or domain_of(u) in domains or source_tier(u) == 4:
                continue
            if is_video_game_contaminated(f"{extra.get('title','')} {extra.get('excerpt','')}"):
                continue
            urls.append(u)
            domains.add(domain_of(u))
            if len(urls) >= 5:
                break
    candidate["source_urls"] = urls[:6]

    evidence_blocks = []
    for url in candidate["source_urls"][:5]:
        article = fetch_article(url, fallback_excerpt=candidate.get("excerpt", ""))
        if article.get("text"):
            evidence_blocks.append(
                f"SOURCE: {url}\nTIER: {source_tier(url)}\nTEXT:\n{article['text'][:8500]}"
            )

    if not evidence_blocks:
        return False, {"status": "unverified", "confidence": 0, "reason": "No source evidence extracted."}

    system = """
You are the strict verification editor for The Sports Newsroom.
Verify the candidate from the supplied source evidence only.
A known primary/official source can establish a fact by itself when the source directly supports it.
Otherwise, require at least two genuinely independent credible domains.
Discovery sources can support context but do not automatically make a claim true.
Lead-only sources (Reddit, Quora, social media, forums) cannot verify claims by themselves.
Check every date, number, name, origin, rule, record and causal/historical statement.
If sources conflict, mark disputed. If evidence is absent, mark unverified.
Never use model memory to fill missing details.
Return only the schema.
""".strip()
    user = (
        f"CANDIDATE\nSubject: {candidate.get('subject')}\n"
        f"Claim/Event: {candidate.get('claim_or_event')}\nAngle: {candidate.get('angle')}\n\n"
        + "\n\n".join(evidence_blocks)
    )
    result = providers.ai(system=system, user=user, schema_name="candidate_verification_v2", schema=VERIFY_SCHEMA, max_tokens=2000)
    status = text(result.get("status"))
    confidence = int(result.get("confidence", 0) or 0)

    has_primary = any(source_tier(u) == 1 for u in candidate["source_urls"])
    domain_count = len(independent_domains(candidate["source_urls"]))
    hard_ok = (
        status == "verified"
        and confidence >= 80
        and (has_primary or domain_count >= 2)
        and not is_video_game_contaminated(" ".join(map(text, [candidate.get("subject"), candidate.get("claim_or_event")])) )
    )
    if status == "disputed" and candidate.get("kind") in {"fact", "history", "rule"} and confidence >= 85 and domain_count >= 2:
        # Disputed facts can be publishable only when the dispute itself is the point.
        hard_ok = True
    return hard_ok, result


def verify_story_against_evidence(providers: Providers, story: dict, candidate: dict) -> bool:
    source_urls = list(dict.fromkeys(text(u) for u in story.get("sources", []) if text(u)))
    if not source_urls:
        source_urls = list(dict.fromkeys(text(u) for u in candidate.get("source_urls", []) if text(u)))

    evidence = []
    for url in source_urls[:5]:
        article = fetch_article(url, fallback_excerpt=candidate.get("excerpt", ""))
        if article.get("text"):
            evidence.append(f"SOURCE: {url}\nTEXT:\n{article['text'][:7000]}")
    if not evidence:
        return False

    system = """
You are the final factual grounding checker.
Compare the generated post against the supplied source evidence.
PASS only if every substantive factual statement in the generated post is supported by the evidence or is a harmless structural transition.
Do not allow invented dates, numbers, names, rules, origins, records, causal explanations or superlatives.
Do not reward plausibility. Evidence must be present.
Return only JSON.
""".strip()
    user = (
        f"CANDIDATE: {candidate.get('claim_or_event')}\n"
        f"GENERATED:\nHeadline: {story.get('headline')}\nDek: {story.get('dek')}\n"
        f"Body: {story.get('body')}\nWhy: {story.get('why_interesting')}\n"
        f"Key points: {story.get('key_points')}\n\n" + "\n\n".join(evidence)
    )
    result = providers.ai(system=system, user=user, schema_name="story_factcheck_v2", schema=STORY_FACTCHECK_SCHEMA, max_tokens=1400)
    return result.get("status") == "pass"


def deterministic_story_checks(story: dict, source_text: str) -> tuple[bool, str]:
    fields = " ".join(
        [text(story.get("headline")), text(story.get("dek")), text(story.get("body")),
         text(story.get("why_interesting")), " ".join(map(text, story.get("key_points", [])))]
    )
    if is_video_game_contaminated(fields):
        return False, "video_game_contamination"
    source_digits = set(re.findall(r"\d{3,}", source_text))
    output_digits = set(re.findall(r"\d{3,}", fields))
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
