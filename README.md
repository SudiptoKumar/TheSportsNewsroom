# The Sports Newsroom Discovery Engine v0.1.0

Production-oriented Telegram bot for [@TheSportsNewsroom](https://t.me/TheSportsNewsroom).

## Product

This is a **sports + real-world games discovery engine**, not a conventional sports-news scraper. It covers professional and obscure sports, board/card/tabletop/party/traditional/mind games, rules, how-to-play guides, history, facts, new physical games and historical date-based content.

It explicitly excludes video games, consoles, PC/mobile gaming, esports, gaming hardware, DLC, patches and video-game industry news.

## Editorial backbone

Two daily sports anchor posts are kept in a separate mandatory lane:

- `NEXT UP · [EXACT DATE]`
- `THE DAY IN SPORTS · [EXACT DATE]`

Discovery posts are a separate quality-gated lane. Discovery can never consume the reserved AI budget for the mandatory lane.

## Discovery families

`GAME DISCOVERY` · `NEW BOARD GAME` · `NEW CARD GAME` · `NEW TABLETOP GAME` · `NEW SPORT` · `DID YOU KNOW?` · `RULE CHECK` · `HOW TO PLAY` · `GAME HISTORY` · `SPORT HISTORY` · `ON THIS DATE` · `100 YEARS AGO` · `WHY?` · `FIRST / LAST / ONLY` · `THEN → NOW` · `FORGOTTEN` · `SPORT DISCOVERY` · `MYTH VS FACT` · `GAME ANATOMY` · `THE STORY BEHIND THE NUMBER`

## v0.1.0 architecture

```text
Internet / Exa / RSS
        ↓
Raw candidates
        ↓
Cheap deterministic filters
        ↓
Query-family balancing
        ↓
Stable candidate IDs
        ↓
AI classifier in batches of 8
        ↓
Missing-ID-only retries
        ↓
Candidate accounting gate
        ↓
Small verification pool
        ↓
Evidence packets
        ↓
Claim verification
        ↓
Deterministic editorial selection
        ↓
AI writer
        ↓
Structural + numeric checks
        ↓
One repair pass when needed
        ↓
Final evidence grounding
        ↓
Original visual
        ↓
Telegram Rich Message / sendPhoto fallback
```

### Important design changes

**Event date is not source publication date.** Current and historical searches do not use Exa publication-date filters to represent an event date. Historical content is searched by the exact historical date/year.

**Direct scraping is optional.** If a source returns 403/404/timeout, the run can fall back to Exa search-highlight evidence rather than dropping the candidate immediately. Fetching uses short timeouts and limited retries.

**AI is budgeted.** The default is 18 Cerebras API attempts per run. The reserve is computed from the mandatory jobs actually due for their target dates, not from the current clock hour. Unused mandatory reserve is released back to discovery after the mandatory lane gets its turn.

**Classification is bounded.** Candidates are classified in batches of 8 with batch-local IDs. A missing candidate is retried by itself rather than sending the entire 36-candidate batch again. Every classification input receives an explicit terminal accounting decision.

**Mandatory lanes are target-date based.** `NEXT UP` catches up from midnight through the 06:00 cutoff for the current date, while the previous day's `THE DAY IN SPORTS` catches up through the same early-morning window. Missed windows are persisted as `expired` rather than silently disappearing. Repeated in-window `failed`/`no_data` attempts are bounded by `MANDATORY_MAX_ATTEMPTS` and then recorded as `gave_up`; an uncertain Telegram delivery is never auto-retried.

**Publishing is guarded.** A deterministic publish intent is persisted before Telegram is touched. Confirmed success becomes `published`; network/5xx ambiguity becomes `unknown` and is never automatically resent.

**No second editorial AI call.** Candidate classification is AI-assisted; final selection is deterministic and logged, which makes the selection auditable and reduces API usage.

**Claim-level memory.** Published claims are stored with both angle-specific and core-claim fingerprints so paraphrased duplicates are rejected.

**Run telemetry.** Every run writes `data/state/last_run_report.json` and prints a funnel report to the GitHub Actions log and Step Summary. It records search counts, candidate counts, rejection reasons, source failures, timings, AI budget use and published posts.

## State

```text
data/state/
├── knowledge_state.json
├── published_urls.txt
└── last_run_report.json
```

`knowledge_state.json` stores claims, entities, posts, source health, candidate decisions, target-date lane state, publish intents and recent run summaries. Existing `daily_flags` are migrated into the richer lane state automatically.

## Environment variables

Required GitHub Actions secrets:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional configuration:

```text
CEREBRAS_MODEL=gpt-oss-120b
AI_MAX_CALLS_PER_RUN=18
AI_MANDATORY_CALLS_PER_DAILY=4
MAX_CLASSIFICATION_CANDIDATES=36
MAX_CLASSIFICATION_BATCH_SIZE=8
MAX_VERIFICATION_CANDIDATES=3
MAX_DISCOVERY_POSTS_PER_RUN=2
MAX_DISCOVERY_POSTS_PER_DAY=4
SEARCHES_PER_RUN=18
HTTP_CONNECT_TIMEOUT=6
HTTP_READ_TIMEOUT=12
HTTP_RETRY_COUNT=1
```

## Local validation

```bash
python -m py_compile main.py src/sportsgames/*.py
python -m unittest discover -s tests -v
python main.py --self-test
```

Live API execution requires the Exa, Cerebras and Telegram credentials.

## Repository tree

```text
SportsGamesDiscoveryBot/
├── .github/workflows/
│   ├── newbot.yml
│   └── import-zip.yml
├── data/
│   ├── taxonomy.json
│   ├── sources.json
│   └── state/
│       ├── knowledge_state.json
│       ├── published_urls.txt
│       └── last_run_report.json
├── src/sportsgames/
│   ├── config.py
│   ├── content.py
│   ├── discovery.py
│   ├── editorial.py
│   ├── lanes.py
│   ├── media.py
│   ├── observability.py
│   ├── pipeline.py
│   ├── providers.py
│   ├── schemas.py
│   ├── state.py
│   ├── taxonomy.py
│   ├── telegram.py
│   ├── utils.py
│   └── verification.py
├── tests/
│   ├── __init__.py
│   └── test_core.py
├── main.py
├── requirements.txt
└── README.md
```

MANDATORY_MAX_ATTEMPTS=3
