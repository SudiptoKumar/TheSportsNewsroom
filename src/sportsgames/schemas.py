from __future__ import annotations

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "maxItems": 30, "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "enum": ["fact", "game", "rule", "history", "sport_event", "new_game", "howto"]},
                "category": {"type": "string"},
                "angle": {"type": "string"},
                "game_or_sport": {"type": "string"},
                "subject": {"type": "string"},
                "claim_or_event": {"type": "string"},
                "source_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "why_interesting": {"type": "string"},
                "date_anchor": {"type": "string"},
            },
            "required": ["id", "kind", "category", "angle", "game_or_sport", "subject", "claim_or_event", "source_urls", "why_interesting", "date_anchor"],
            "additionalProperties": False,
        }},
    },
    "required": ["items"], "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["verified", "disputed", "unverified", "reject"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "supported_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 15},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 15},
        "source_assessments": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "conflicts": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "reason": {"type": "string"},
    },
    "required": ["status", "confidence", "supported_claims", "unsupported_claims", "source_assessments", "conflicts", "reason"],
    "additionalProperties": False,
}

EDITORIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 12},
        "editorial_note": {"type": "string"},
    },
    "required": ["selected_ids", "editorial_note"], "additionalProperties": False,
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": [
            "fact", "game_discovery", "rule_check", "how_to_play", "history", "on_this_date",
            "century_ago", "why", "first_last_only", "then_vs_now", "forgotten", "number",
            "daily_next", "daily_past",
        ]},
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "body": {"type": "string"},
        "why_interesting": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "date_anchor": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["format", "headline", "dek", "body", "why_interesting", "key_points", "date_anchor", "sources", "tags"],
    "additionalProperties": False,
}

STORY_FACTCHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "fail"]},
        "unsupported_statements": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "reason": {"type": "string"},
    },
    "required": ["status", "unsupported_statements", "reason"],
    "additionalProperties": False,
}

DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "maxItems": 60, "items": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"}, "event": {"type": "string"}, "date": {"type": "string"},
                "time_utc": {"type": "string"}, "competition": {"type": "string"}, "stage": {"type": "string"},
                "location": {"type": "string"}, "status": {"type": "string"},
                "importance": {"type": "integer", "minimum": 0, "maximum": 100},
                "reason": {"type": "string"}, "source_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            },
            "required": ["sport", "event", "date", "time_utc", "competition", "stage", "location", "status", "importance", "reason", "source_urls"],
            "additionalProperties": False,
        }},
    },
    "required": ["events"], "additionalProperties": False,
}
