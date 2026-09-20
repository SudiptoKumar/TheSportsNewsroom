from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "state"
STATE_FILE = STATE_DIR / "knowledge_state.json"
PUBLISHED_FILE = STATE_DIR / "published_urls.txt"

BD_TZ = ZoneInfo("Asia/Dhaka")

APP_NAME = "The Sports Newsroom Discovery Engine"
APP_VERSION = "2.1.0"
CHANNEL_DEFAULT = "@TheSportsNewsroom"

CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", CHANNEL_DEFAULT).strip()
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()

# Website retrieval is deliberately fast-fail. A blocked/slow site must not stall the whole run.
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "6"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "12"))
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)

# AI pacing/budget. Expensive calls are reserved for shortlisted candidates.
AI_MIN_INTERVAL_SECONDS = float(os.getenv("AI_MIN_INTERVAL_SECONDS", "1.75"))
AI_MAX_CALLS_PER_RUN = int(os.getenv("AI_MAX_CALLS_PER_RUN", "18"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "2"))

POST_DELAY_SECONDS = float(os.getenv("POST_DELAY_SECONDS", "3"))
MAX_EXA_RESULTS = int(os.getenv("MAX_EXA_RESULTS", "8"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "90"))
MAX_CLASSIFICATION_CANDIDATES = int(os.getenv("MAX_CLASSIFICATION_CANDIDATES", "50"))
MAX_VERIFICATION_CANDIDATES = int(os.getenv("MAX_VERIFICATION_CANDIDATES", "10"))
MAX_EDITORIAL_SHORTLIST = int(os.getenv("MAX_EDITORIAL_SHORTLIST", "18"))
MAX_DISCOVERY_POSTS_PER_DAY = int(os.getenv("MAX_DISCOVERY_POSTS_PER_DAY", "4"))
MAX_DISCOVERY_POSTS_PER_RUN = int(os.getenv("MAX_DISCOVERY_POSTS_PER_RUN", "2"))
STATE_RETENTION_DAYS = int(os.getenv("STATE_RETENTION_DAYS", "365"))
MAX_SOURCE_FETCHES_PER_STORY = int(os.getenv("MAX_SOURCE_FETCHES_PER_STORY", "4"))
MAX_REPAIR_ATTEMPTS = int(os.getenv("MAX_REPAIR_ATTEMPTS", "2"))

# Daily anchors are deliberately independent of the evergreen quota.
NEXT_UP_AFTER_HOUR = int(os.getenv("NEXT_UP_AFTER_HOUR", "6"))
DAY_IN_SPORTS_AFTER_HOUR = int(os.getenv("DAY_IN_SPORTS_AFTER_HOUR", "19"))

SEARCHES_PER_RUN = int(os.getenv("SEARCHES_PER_RUN", "14"))
NEXT_SCHEDULE_LOOKBACK_DAYS = int(os.getenv("NEXT_SCHEDULE_LOOKBACK_DAYS", "90"))
PAST_RESULTS_PUBLISH_DAYS_BEFORE = int(os.getenv("PAST_RESULTS_PUBLISH_DAYS_BEFORE", "1"))
PAST_RESULTS_PUBLISH_DAYS_AFTER = int(os.getenv("PAST_RESULTS_PUBLISH_DAYS_AFTER", "4"))
ENTITY_30D_SOFT_CAP = int(os.getenv("ENTITY_30D_SOFT_CAP", "5"))

# Telegram Rich Message limits. A photo caption remains the hard fallback.
MAX_RICH_CHARACTERS = 32768
MAX_CAPTION_CHARACTERS = 1024

HEADERS = {
    "User-Agent": "TheSportsNewsroomDiscoveryEngine/2.1 (+https://t.me/TheSportsNewsroom)",
    "Accept-Language": "en-US,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src",
}

@dataclass(frozen=True)
class RuntimeConfig:
    app_name: str = APP_NAME
    version: str = APP_VERSION
    channel: str = TELEGRAM_CHANNEL
    tz: ZoneInfo = BD_TZ
    state_file: Path = STATE_FILE
    published_file: Path = PUBLISHED_FILE


CONFIG = RuntimeConfig()
