# The Sports Newsroom V2.2.0

Automated Telegram newsroom for evergreen Sports & Games knowledge, relevant internet-sourced photos, plus a rotating live schedule/results pair that is kept to the newest two messages.

This repository contains **one production engine only**. The scheduled run, dry-run, and diagnostic entrypoints all dispatch through the same `run_once()` implementation.

## 1. Architecture

```text
Evergreen

Normal web search (primary, no paid search API)
   ↓
Exa Search fallback only when normal results are sparse
   ↓
20-sector candidate reservoir
   ↓
Local URL / subject / knowledge-unit dedupe
   ↓
Cerebras ranking
   ↓
Exa Contents evidence retrieval
   ↓
Optional Exa Agent hard-case verification
   ↓
Cerebras editorial generation
   ↓
Normalization + deterministic validation
   ↓
Visual Telegram publication with idempotency
```

```text
Live pair

Sports-data adapters
   ↓
Event identity + importance filtering
   ↓
Rich Telegram H1 heading + exactly two-column table
   ↓
Publish replacement schedule/results messages
   ↓
Persist new IDs + queue replaced IDs
   ↓
Delete queued IDs with one JSON-serialized batch request
   ↓
Retry any failed cleanup on the next run
```

The live pair is data-driven and does not use Exa as a fallback publishing engine.

## 2. Production volume

The production schedule runs twice per day:

- 10 evergreen posts is the target per run
- 2 scheduled runs per day
- 20 evergreen posts/day is the nominal target
- 2 temporary live messages are refreshed on every successful run: next-day schedule and previous-day results

The evergreen phase is deliberately **partial-credit**, so source scarcity, deadline pressure or AI throttling can produce fewer than 10 posts without aborting the run. The live pair is still attempted.

The live pair is rotated transactionally. A replacement is accepted only after Telegram returns its new message ID. The replaced message IDs are persisted in `retiring_message_ids` and sent to Telegram's `deleteMessages` endpoint as a JSON-serialized array. Failed cleanup remains queued for the next run, so a temporary Telegram error cannot strand the previous pair permanently.

## 3. The 20 evergreen sectors

| # | Sector |
|---:|---|
| 1 | Sport Discovery |
| 2 | Game Discovery |
| 3 | Interesting Sports Fact |
| 4 | Interesting Game Fact |
| 5 | Rule Check |
| 6 | How to Play |
| 7 | Sport Origin |
| 8 | Game Origin |
| 9 | On This Date |
| 10 | First / Last / Only |
| 11 | Records & Milestones |
| 12 | Forgotten Sport |
| 13 | Forgotten Game |
| 14 | Equipment / Measurement |
| 15 | Why Does This Happen? |
| 16 | Then vs Now |
| 17 | New Sport Discovery |
| 18 | New Tabletop Game Discovery |
| 19 | Traditional / Regional Game |
| 20 | Sports & Games People |

Every production run attempts all 20 sectors, but sector discovery is **partial-credit**: a sector that has no usable candidate after recovery is logged and skipped rather than aborting the entire run. Ranking, verification, local coverage state and editorial validation determine how many publishable evergreen posts the run can produce.

## 4. Evergreen content rules

Evergreen discovery intentionally excludes current-news material such as:

- live scores and match reports
- current fixtures and previews
- standings and current table positions
- transfers, signings and squad updates
- injuries and current-season form
- betting and odds
- breaking/latest news framing
- promotional pages

Historical origins, rules, records, milestones, equipment, traditional games, people, documented discoveries and historical comparisons remain in scope.

## 5. Provider responsibilities

### Normal web search

Used as the primary no-API-key discovery layer through ordinary web search engines. Exa Search is used only when the normal provider returns too few usable results. The application does not ask Search to manufacture the final Telegram publication object.

### Image discovery

Image discovery is normal-search-first and separate from the editorial AI. The bot first checks lead-image metadata from the selected evidence pages, then searches ordinary Bing Images for the exact story subject. Exa image results are a last-resort fallback only when normal image candidates fail verification. The selected image is downloaded to the runner before publication and the original bytes are sent without cropping or resizing.

### Exa Contents

Used after ranking to retrieve text and highlights from selected source URLs. Requests are batched and normalized before evidence reaches the editorial stage.

### Exa Agent

Used only for hard cases such as disputed origins, exact historical dates, first/last/only claims, conflicting records and difficult attribution questions.

### Cerebras

Used for two structured-output jobs:

1. Rank the candidate reservoir.
2. Turn verified evidence into the final evergreen Telegram post.

The client enforces a per-run provider-attempt budget and adaptive cross-call throttling. Repeated 429 responses increase the pause before the next attempt; once the run budget is exhausted, further AI work is skipped so the run can still publish any candidates that already have enough evidence and continue to the live pair.

The editorial layer is instructed to use only supplied evidence and not invent claims, dates, numbers, people, locations, rules or URLs. Each evergreen publication uses one 40-60 word paragraph, a 6-14 word headline, and at most 3 hashtags. Lists and duplicate explanatory blocks are excluded. Image selection is handled by the deterministic image subsystem after editorial validation.

### Local Python

The application is authoritative for URL normalization, sector coverage, semantic dedupe, evergreen safety filters, validation, publication IDs, Telegram idempotency, live-pair rotation and state persistence.

## 6. Evergreen publication shape

### Visual design

The preferred evergreen visual is a real, story-relevant internet photo selected from normal image search or the chosen source pages. The bot sends the verified original file directly to Telegram without server-side crop, resize, recompression or aspect-ratio normalization.

The generated 1200x675 branded card remains the deterministic fallback when no external candidate passes verification. It contains one clear category label, one readable headline, and no more than three compact information columns. The columns select only high-value metadata such as **KEY FACT**, **WHEN**, and **WHERE**.

Every evergreen Telegram post follows one compact editorial contract:

```text
<b>Headline</b>

One 40-60 word paragraph

Source: clickable source names
#Tag #Tag #Tag
```

The final message is assembled by exactly one function: `render_evergreen_post(story)`. Source names are rendered as clickable HTML links. Raw URLs are never shown as visible text. Hashtags are always on the final line and are capped at 3.

## 7. Reliability and fallback design

Prompt files are external override files, but the four production prompts also have embedded fallbacks in `main.py`. Missing or renamed prompt files therefore do not stop the engine from running.

The hard-case Agent schema is also embedded in `main.py` and is checked against the repository schema by the test suite.

The evergreen publisher uses a verified-media decision:

```text
render_evergreen_post()
        ↓
find relevant photo from normal web sources
        ↓
HEAD + GET verification
        ├── valid → upload original file with sendPhoto
        └── invalid/unavailable → 1200x675 branded card
```

A remote image URL is never passed directly to Telegram. The bot requires HTTP 200, an image content type, a file size within Telegram's upload limit, a decodable JPEG/PNG/WebP payload, and valid Telegram photo geometry before sending it. No downloaded image is cropped or resized.

State writes are skipped in `--dry-run` mode. Telegram publishing is skipped in `--dry-run` mode.

## 8. Single operational entrypoint

`run_once()` is the canonical operational entrypoint.

```text
python main.py
    ↓
v1_main()
    ↓
run_once()
    ↓
v1_run_once()
```

The command modes are only modes of the same engine:

```bash
python main.py --diagnose
python main.py --dry-run
python main.py --self-test --fast
python main.py --validate-config
python main.py --version
python main.py
```

`--diagnose` performs provider/configuration health checks through the canonical run dispatcher.

`--dry-run` executes the V2 research, ranking, verification, editorial and selection path without Telegram publication or persistent state writes.

`--self-test` checks the V2 architecture and provider contracts offline.

## 9. Configuration

Required secrets:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

`EXA_API_KEY` is used only by the paid Exa fallback/verification paths. It is no longer the default evergreen discovery search provider.

Optional environment variables:

```text
CEREBRAS_MODEL=gpt-oss-120b
CEREBRAS_MAX_CALLS_PER_RUN=16
CEREBRAS_RATE_LIMIT_PAUSE_SECONDS=5
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_CHANNEL=@TheSportsNewsroom
CHANNEL_OVERRIDE=
CRICKETDATA_API_KEY=
THESPORTSDB_KEY=3
```

V1 tuning variables:

```text
V1_SEARCH_RESULTS_PER_SECTOR=6
V1_MAX_CANDIDATES_PER_SECTOR=3
V1_CONTENTS_URLS_PER_STORY=3
V1_AGENT_MAX_CASES=2
V1_AGENT_TIMEOUT_SECONDS=120
V1_SEARCH_DELAY_SECONDS=0.12
NORMAL_SEARCH_RESULTS=6
NORMAL_SEARCH_FALLBACK_MIN=3
NORMAL_SEARCH_TIMEOUT=15
IMAGE_SEARCH_RESULTS=6
IMAGE_SEARCH_TIMEOUT=12
IMAGE_PAGE_MAX_BYTES=512000
IMAGE_MIN_RELEVANCE=0.10
MAX_IMAGE_BYTES=10000000
V1_CONTENTS_CHARS=9000
V1_RANK_MAX_CANDIDATES=20
V1_RANK_MAX_TOKENS=700
V1_EDITORIAL_MAX_TOKENS=900
```

Global runtime controls include `RUN_DEADLINE_SECONDS` (default 1500 seconds), `HTTP_TIMEOUT`, `STATE_RETENTION_DAYS`, `POST_DELAY_SECONDS` and `LOG_LEVEL`.

## 10. Local setup

Requirements:

- Python 3.12+
- `requests`
- `Pillow`

Install:

```bash
python -m pip install -r requirements.txt
```

Set the required environment variables, then verify the repository:

```bash
python main.py --version
python main.py --validate-config
python main.py --self-test --fast
python -m unittest discover -s tests -v
```

Run a non-publishing rehearsal:

```bash
python main.py --dry-run
```

Run production:

```bash
python main.py
```

## 11. GitHub Actions

Production workflow: `.github/workflows/newbot.yml`

Schedule:

```text
02:15 UTC → 08:15 Asia/Dhaka
14:15 UTC → 20:15 Asia/Dhaka
```

Manual modes:

```text
run
diagnose
dry-run
self-test
```

The workflow verifies the repository structure, compiles the Python files, validates the JSON schema, prints the version, runs the V2 architecture tests and the full unit-test suite, then runs the selected mode.

## 12. Repository structure

```text
.
├── main.py
├── README.md
├── requirements.txt
├── news_state.json
├── posted_urls.txt
├── coverage_index.json
├── prompts/
│   ├── cerebras_editorial_v1.txt
│   ├── cerebras_rank_v1.txt
│   ├── exa_hard_case_agent_v1.txt
│   └── exa_sector_discovery_v1.txt
├── schemas/
│   └── exa_agent_hard_case_v1.json
├── tests/
│   ├── __init__.py
│   └── test_v1.py
└── .github/
    └── workflows/
        ├── import-zip.yml
        └── newbot.yml
```

There are no legacy V3/V4 production pipelines, V4 prompt packs or V4 schemas in the repository.

## 13. State and compatibility

The active state namespace remains the stable internal `v1` namespace for backwards-compatible state migration. The live sub-state also stores `retiring_message_ids` so interrupted rotations can finish deleting stale live messages on the next run.

The engine can migrate the previous live-pair/day memory stored under the old `v4` state key. That key is treated strictly as historical data migration. No V4 engine or V4 execution path remains.

Core state files:

```text
news_state.json      runtime state, publication records and live-pair IDs
coverage_index.json  semantic/editorial coverage memory
posted_urls.txt      URL-level publication memory
```

## 14. Testing

Current verification:

```text
V2 architecture tests: see `--self-test` output
Python unittest suite: see CI/full-suite output
```

The tests explicitly guard against the original failure mode by checking that the canonical run entrypoints resolve to the same V2 implementation and that legacy V3/V4 orchestration symbols are absent.

## 15. Versioning

The application version has one source of truth: `2.2.0` for this release.

```python
APP_VERSION = "2.2.0"
```

The CLI reports:

```text
The Sports Newsroom V2.2 2.2.0
```

Update `APP_VERSION` in `main.py` for future releases and keep repository documentation aligned with that value.
