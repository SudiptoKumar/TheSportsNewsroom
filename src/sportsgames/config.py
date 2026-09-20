from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "state"
STATE_FILE = STATE_DIR / "knowledge_state.json"
PUBLISHED_FILE = STATE_DIR / "published_urls.txt"
RUN_REPORT_FILE = STATE_DIR / "last_run_report.json"

BD_TZ = ZoneInfo("Asia/Dhaka")

APP_NAME = "The Sports Newsroom Discovery Engine"
APP_VERSION = "0.1.0"
CHANNEL_DEFAULT = "@TheSportsNewsroom"

CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", CHANNEL_DEFAULT).strip()
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()

# Fast-fail web retrieval. Do not let one blocked domain consume the run.
HTTP_CONNECT_TIMEOUT = int(os.getenv("HTTP_CONNECT_TIMEOUT", "6"))
HTTP_READ_TIMEOUT = int(os.getenv("HTTP_READ_TIMEOUT", "12"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))  # compatibility alias
HTTP_RETRY_COUNT = int(os.getenv("HTTP_RETRY_COUNT", "1"))

POST_DELAY_SECONDS = float(os.getenv("POST_DELAY_SECONDS", "3"))
MAX_EXA_RESULTS = int(os.getenv("MAX_EXA_RESULTS", "6"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "100"))
MAX_CLASSIFICATION_CANDIDATES = int(os.getenv("MAX_CLASSIFICATION_CANDIDATES", "36"))
MAX_CLASSIFICATION_BATCH_SIZE = int(os.getenv("MAX_CLASSIFICATION_BATCH_SIZE", "8"))
MAX_EDITORIAL_SHORTLIST = int(os.getenv("MAX_EDITORIAL_SHORTLIST", "12"))
MAX_VERIFICATION_CANDIDATES = int(os.getenv("MAX_VERIFICATION_CANDIDATES", "3"))
MAX_DISCOVERY_POSTS_PER_DAY = int(os.getenv("MAX_DISCOVERY_POSTS_PER_DAY", "4"))
MAX_DISCOVERY_POSTS_PER_RUN = int(os.getenv("MAX_DISCOVERY_POSTS_PER_RUN", "2"))
STATE_RETENTION_DAYS = int(os.getenv("STATE_RETENTION_DAYS", "365"))

# AI budget: mandatory daily lane has priority, discovery uses the remainder.
AI_MAX_CALLS_PER_RUN = int(os.getenv("AI_MAX_CALLS_PER_RUN", "18"))
AI_MAX_REPAIR_PER_STORY = int(os.getenv("AI_MAX_REPAIR_PER_STORY", "1"))
AI_MANDATORY_CALLS_PER_DAILY = int(os.getenv("AI_MANDATORY_CALLS_PER_DAILY", "4"))
MANDATORY_MAX_ATTEMPTS = int(os.getenv("MANDATORY_MAX_ATTEMPTS", "3"))

# Daily anchors are deliberately independent of the evergreen quota.
NEXT_UP_AFTER_HOUR = int(os.getenv("NEXT_UP_AFTER_HOUR", "6"))
DAY_IN_SPORTS_AFTER_HOUR = int(os.getenv("DAY_IN_SPORTS_AFTER_HOUR", "19"))

# Evergreen query breadth. Daily sports searches are separate.
SEARCHES_PER_RUN = int(os.getenv("SEARCHES_PER_RUN", "18"))

MAX_RICH_CHARACTERS = 32768
MAX_CAPTION_CHARACTERS = 1000

HEADERS = {
    "User-Agent": "TheSportsNewsroomDiscoveryEngine/0.1 (+https://t.me/TheSportsNewsroom)",
    "Accept-Language": "en-US,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    run_report_file: Path = RUN_REPORT_FILE

CONFIG = RuntimeConfig()
