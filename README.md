# The Sports Newsroom Discovery Engine v2.2.1

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

## v2.2.1 architecture

```text
Internet / Exa / RSS
        ↓
Raw candidates
        ↓
Cheap deterministic filters
        ↓
Query-family balancing
        ↓
One batch AI classifier
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

**AI is budgeted.** The default is 18 logical AI operations per run, with a dynamic reserve of 4 AI operations per mandatory daily post (up to 8 when both daily anchors are due). Discovery has room for one classifier call, three verification calls, two writers, two final fact checks and up to two repair calls.

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

`knowledge_state.json` stores claims, entities, posts, source health, candidate decisions and recent run summaries.

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
├── tests/test_core.py
├── main.py
├── requirements.txt
└── README.md
```
