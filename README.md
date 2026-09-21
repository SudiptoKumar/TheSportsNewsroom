# The Sports Newsroom V1.1

Automated Telegram newsroom for evergreen Sports & Games knowledge, with a separate live schedule/results pair.

The V1.1 research engine is deliberately **Search-first** and is aligned with the current Exa Search / Contents / Agent API model and Cerebras API Version 2 behavior:

```text
Exa Search
   ↓
Local candidate reservoir
   ↓
Deterministic URL / subject / knowledge-unit dedupe
   ↓
Cerebras ranking
   ↓
Top 10 research targets
   ↓
Exa Contents verification
   ↓
Optional secondary Search
   ↓
Optional Exa Agent hard-case verification
   ↓
Cerebras editorial writing
   ↓
Canonical normalization
   ↓
Deterministic validation
   ↓
Telegram idempotent publish
```

The live engine is separate and is not researched with Exa:

```text
Schedule / results data
   ↓
Event identity + importance filtering
   ↓
Rich Telegram table
   ↓
Publish new pair
   ↓
Verify IDs
   ↓
Delete old pair
   ↓
Persist state
```

## 1. What V1 publishes

At steady state the channel maintains:

- 20 evergreen Sports & Games knowledge sectors across the editorial calendar.
- 10 evergreen posts per production run.
- 2 production runs per day.
- 1 temporary next-day major games schedule post.
- 1 temporary previous-day major results post.

The live pair is always published **after** the evergreen posts, so the final two Telegram messages from a successful run are the live schedule and live results.

The 20 evergreen sectors are:

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

## 2. What is excluded from evergreen content

Evergreen discovery does not intentionally target current-news material such as:

- live scores and live match coverage
- current fixtures and upcoming match previews
- standings and current table positions
- transfers and signings
- injuries and squad updates
- betting and odds
- breaking news
- current-season form
- current match reports or current recaps
- promotional pages

Historical origins, rule changes, records, milestones, traditional games, old equipment, historical people, documented discoveries and other durable knowledge are allowed.

## 3. V1 responsibility boundaries

### Exa Search

Exa Search is the **discovery and targeted retrieval layer**. V1 does not ask Search to generate the complete publication object.

Each editorial sector gets its own targeted search query. A normal run searches all 20 sectors and builds a candidate reservoir from the native Exa results.

The code uses the native Search result fields such as:

- title
- URL
- published date
- author
- result ID
- image
- highlights
- text when supplied
- favicon

V1 does not put a large publication schema into Search.

### Exa Contents

Contents is used only after the ranking desk selects candidates. V1 sends the already-known source URLs in batches and asks Exa for highlights and text.

A positive `maxAgeHours` value is used so Exa can use recent cached extraction while still allowing fallback fetching when necessary.

### Exa Agent

Agent is an escalation tool, not the default research endpoint. It is used only for hard historical claims, conflicts, and multi-hop verification. Agent runs are created asynchronously and polled to a terminal state.

V1 uses Agent only for difficult cases such as:

- first / last / only claims
- exact historical dates
- disputed origins
- traditional or regional attribution conflicts
- complex historical versus modern comparisons
- difficult record claims

Agent research is asynchronous. V1 creates a run, stores the run ID, polls until a terminal state, and uses the structured result plus terminal grounding.

### Cerebras

Cerebras is treated as a Cerebras API Version 2 structured-output service. The default model is `gpt-oss-120b`, which currently supports structured outputs and reasoning. Strict JSON Schema is the primary output mode; JSON mode is retained only as a controlled compatibility fallback. Cerebras HTTP errors are classified and logged with safe request/rate-limit diagnostics, without exposing API keys.

Cerebras has two editorial jobs:

1. Rank the local candidate reservoir.
2. Transform verified evidence into the final Telegram post.

Cerebras is explicitly instructed not to create new facts, dates, numbers, people, locations, URLs or evidence.

### Local Python code

The local application remains authoritative for:

- sector coverage
- URL normalization
- subject and knowledge-unit deduplication
- evergreen safety rules
- source quality signals
- evidence sufficiency
- output normalization
- image validation
- publication IDs
- Telegram idempotency
- live-pair rotation
- state persistence

## 4. Discovery algorithm

A V1 run starts by loading persistent coverage state and determining whether it is the first or second scheduled run for the current Bangladesh day.

The complete 20-sector matrix is still researched on **every run**. Run 2 does not reduce discovery to only ten sectors.

Previously published sectors are instead passed into the ranking context so the selection stage can prefer unused sectors.

### Discovery fan-out

Default configuration:

```text
20 sectors
× 6 Exa Search results
≈ 120 raw retrieval candidates
```

The system then applies local filtering:

```text
Raw Search results
    ↓
URL normalization and duplicate removal
    ↓
Current-news exclusion
    ↓
Source quality filtering
    ↓
Permanent coverage matching
    ↓
Knowledge-unit / semantic similarity filtering
    ↓
Maximum 3 candidates per sector
    ↓
Cerebras ranking reservoir
```

The default reservoir can therefore contain roughly 60 candidates before ranking, depending on source quality and deduplication.

## 5. Sector-specific search

V1 does not use one giant generic query.

Each sector has its own search intent. Examples:

### Sport Origin

Search for documented origins, predecessors, early evidence, development, spread and the modern form.

### Rule Check

Search for official or historical rules, unusual rules, misunderstood rules and rule changes.

### First / Last / Only

Search specifically for documented superlatives and require stronger evidence and qualification.

### Traditional / Regional Game

Search for historical and cultural documentation, rules, equipment, geography and terminology.

### On This Date

Search for a historical Sports & Games event that occurred on the calendar date being processed. The date must be verified from evidence.

## 6. Candidate deduplication

V1 does not treat a webpage headline as the unit of coverage.

The system distinguishes:

```text
URL identity
Subject identity
Knowledge-unit identity
Claim identity
```

For example, three pages describing the same historical origin can become one knowledge candidate with multiple supporting sources instead of three separate future posts.

This protects the channel from semantic repetition even when later sources use different wording.

## 7. Ranking

Cerebras ranks the local reservoir using the following dimensions:

- evergreen value
- source quality
- evidence potential
- distinct knowledge
- global variety
- audience usefulness
- curiosity / interest
- visual potential
- low repetition risk
- preference for sectors not already published earlier today

The model's ranking is **not** the final publication authority. Deterministic code still enforces sector, coverage and validation rules.

V1 selects:

```text
ranked reservoir
    ↓
unused sector constraint
    ↓
knowledge-unit uniqueness
    ↓
exactly 10 evergreen targets
```

A reserve pool remains available so a selected candidate that later fails evidence or editorial validation can be replaced without restarting the entire run.

## 8. Evidence verification

For the selected ten:

```text
Selected candidate
    ↓
Exa Contents on known source URLs
    ↓
Evidence extraction
    ↓
Evidence sufficiency check
```

If evidence is weak:

```text
Targeted secondary Exa Search
    ↓
Additional source URLs
    ↓
Exa Contents again
```

A candidate must have at least one successfully extracted source and useful evidence before it proceeds to editorial generation.

Difficult sectors can then escalate to Exa Agent.

## 9. Hard-case verification

Agent output is deliberately small and structured:

```json
{
  "verdict": "supported | qualified | unsupported | insufficient | conflict",
  "qualified_claim": "...",
  "evidence_summary": "...",
  "source_urls": [],
  "conflicts": []
}
```

The schema has five top-level properties.

Agent evidence is not treated as a substitute for source retrieval. When Agent identifies additional source URLs, V1 attempts to retrieve them through Exa Contents before final editorial use.

## 10. Editorial generation

Cerebras receives:

- selected candidate
- sector
- knowledge unit
- central claim
- verified evidence
- source information
- image candidates

The editorial model produces the publication fields required by the existing Telegram renderer.

Default editorial constraints:

- headline: 6-14 words
- body: 50-120 words
- 3-5 key points
- no clickbait
- no current-news framing
- no unsupported facts
- no unsupported numbers
- image selection only from supplied candidates

## 11. Output normalization

This layer is mandatory.

Raw model output never goes directly to Telegram.

For example:

```text
AI output:
image = null

Normalizer:
image = {}
```

Likewise:

```text
sources = null → []
key_points = null → []
tags = null → []
people = null → []
```

This specifically prevents the production failure where the renderer evaluated:

```python
story.get("image", {}).get("url")
```

while the actual runtime value was `image = None`.

The renderer is still kept defensive even after normalization.

## 12. Image system

Images are optional enrichment.

Primary discovery path:

```text
Exa Search result
    ↓
native image field
    ↓
source-page relationship retained
    ↓
image validation
```

Secondary Search results can contribute additional source-linked image candidates.

An image is never allowed to be a hard structural requirement for a story. If no valid image exists, the publication can safely fall back to a text-only rich message.

## 13. Evidence and claim boundary

The central editorial rule is:

> Evidence can constrain what Cerebras writes, but Cerebras cannot upgrade evidence.

Examples:

```text
"earliest documented evidence"
```

must not become:

```text
"first ever"
```

unless the evidence actually establishes the absolute claim.

Likewise, approximate historical language must remain qualified when the evidence is qualified.

## 14. Exa request policy

V1 deliberately avoids the old pattern of trying multiple guessed payload variants after a provider validation error.

Provider errors are handled by class:

```text
400 → configuration / payload problem; surface it
401 → authentication problem
403 → permission problem
402 → billing / credits problem
429 → backoff and retry
5xx → retry with bounded backoff
```

The provider adapter records the status and error without hiding a 400 behind random payload mutations.

## 15. Rate-limit strategy

The default Exa Search limit is 10 QPS. V1 therefore performs the 20 discovery jobs sequentially with a small configurable delay between them.

The Contents phase is batched and uses a much higher documented default limit, so source verification is handled separately from discovery.

The main production setting is:

```text
V1_SEARCH_DELAY_SECONDS=0.15
```

This is intentionally conservative for GitHub Actions.

## 16. Recovery hierarchy

A weak sector does not cause the entire 20-sector process to restart.

Recovery follows this order:

```text
normal Search
    ↓
alternate targeted Search
    ↓
source-focused Search
    ↓
deep Search for difficult recovery cases
```

A difficult selected subject can then escalate to Agent.

One failed candidate also does not invalidate the whole run. The reserve pool can replace it.

## 17. Live engine

The live schedule and live results are intentionally outside the evergreen Exa research engine.

They use the existing live sports data adapters and event identity logic.

The system:

1. generates the new schedule/results pair
2. publishes the new schedule
3. publishes the new results
4. verifies both message IDs
5. persists the new IDs
6. deletes the old pair only after successful replacement

If new live publication fails, the old pair is preserved.

## 18. State files

### `news_state.json`

Stores runtime memory, including V1 state, AI client state, publication information and live message IDs.

### `coverage_index.json`

Stores long-lived coverage information used for duplicate and knowledge-unit protection.

### `posted_urls.txt`

Legacy and operational URL memory retained for compatibility and idempotency.

## 19. V1 state layout

V1 stores its main state under:

```json
{
  "v1": {
    "schema": 1,
    "last_run_at": "...",
    "daily": {},
    "runs": [],
    "live": {
      "schedule": {},
      "results": {}
    },
    "publication": {}
  }
}
```

The first V1 run can migrate the existing V4 live pair and same-day memory when those fields are available.

## 20. Required environment variables

### Required for production

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

### Optional

```text
CEREBRAS_MODEL
a default of gpt-oss-120b is used when not set

TELEGRAM_ADMIN_CHAT_ID
CRICKETDATA_API_KEY
TELEGRAM_CHANNEL
CHANNEL_OVERRIDE
```

### V1 tuning variables

```text
V1_SEARCH_RESULTS_PER_SECTOR=6
V1_MAX_CANDIDATES_PER_SECTOR=3
V1_CONTENTS_URLS_PER_STORY=3
V1_AGENT_MAX_CASES=2
V1_AGENT_TIMEOUT_SECONDS=120
V1_SEARCH_DELAY_SECONDS=0.15
V1_CONTENTS_CHARS=9000
```

The default values are designed for the intended two-run GitHub Actions schedule. They can be changed through workflow environment variables without changing the source.

## 21. Dependencies

`requirements.txt` currently uses:

```text
requests>=2.31
Pillow>=10.1
```

The application calls Exa, Cerebras and Telegram directly over HTTPS. No Exa or Cerebras SDK is required by the repository.

## 22. Local setup

```bash
git clone <repository>
cd TheSportsNewsroom
python -m venv .venv
```

Activate the environment, then:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Set the required environment variables and run:

```bash
python main.py --version
python main.py --validate-config
python main.py --self-test --fast
python -m unittest discover -s tests -v
```

Production run:

```bash
python main.py
```

Dry run:

```bash
python main.py --dry-run
```

Diagnosis:

```bash
python main.py --diagnose
```

## 23. GitHub Actions

### Production workflow

File:

```text
.github/workflows/newbot.yml
```

Scheduled runs:

```text
02:15 UTC
14:15 UTC
```

These correspond to approximately:

```text
08:15 Asia/Dhaka
20:15 Asia/Dhaka
```

Manual modes:

```text
run
diagnose
dry-run
self-test
```

Before production execution the workflow performs:

```text
repository structure validation
compile check
schema check
version check
production configuration validation
offline regression tests
V1 tests
```

Only then does it start the real bot run.

### Import workflow

File:

```text
.github/workflows/import-zip.yml
```

This workflow requires an explicit V1 ZIP filename.

Default:

```text
TheSportsNewsroom-v1.2.0.zip
```

It does **not** search for the first arbitrary `*.zip` in the repository.

Before importing it verifies:

- required V1 files
- V1 prompt files
- Agent schema
- repository workflow files
- package `APP_VERSION`

It imports only after those checks pass.

## 24. Test suite

### Integrated self-test

```bash
python main.py --self-test --fast
```

This keeps the stable legacy regression/compatibility tests and adds V1-specific provider/data-contract checks.

### Standalone V1 tests

```bash
python -m unittest discover -s tests -v
```

V1 tests cover:

- exact 20-sector matrix
- Search payload contract
- absence of discovery `outputSchema`
- native auto-search behavior
- Contents batching
- Contents freshness policy
- Agent schema size
- null-output normalization
- renderer safety after normalization
- secondary Search URL handling
- current-news filtering

### What offline tests do not prove

Offline tests do not prove that a real Exa account will return usable evidence for every live query. They validate request construction, local rules, response parsing, normalization and deterministic safety.

A production run with real credentials is required to validate actual provider availability, source quality and Telegram delivery.

## 25. Exa API model used by V1

V1 is based on Exa's current API separation:

```text
/search
    discovery and ranked retrieval

/contents
    retrieval/extraction from known URLs or IDs

/agent/runs
    asynchronous multi-step research escalation
```

Current Exa documentation:

- Search: https://exa.ai/docs/reference/search
- Contents: https://exa.ai/docs/reference/get-contents
- Agent: https://exa.ai/docs/reference/agent-api-guide
- Error codes: https://exa.ai/docs/reference/error-codes
- Rate limits: https://exa.ai/docs/reference/rate-limits

The implementation intentionally follows the native API response shapes instead of treating Search as a single giant synthetic content-generation endpoint.

## 26. File structure

```text
TheSportsNewsroom/
├── .github/
│   └── workflows/
│       ├── newbot.yml
│       └── import-zip.yml
├── prompts/
│   ├── cerebras_editorial_v1.txt
│   ├── cerebras_rank_v1.txt
│   ├── exa_contents_verification_v1.txt
│   ├── exa_hard_case_agent_v1.txt
│   ├── exa_sector_discovery_v1.txt
│   ├── ... legacy V4 prompts retained for compatibility
├── schemas/
│   ├── exa_agent_hard_case_v1.json
│   └── ... legacy V4 schemas retained for compatibility
├── tests/
│   ├── __init__.py
│   ├── test_v1.py
│   └── test_v4.py
├── coverage_index.json
├── main.py
├── news_state.json
├── posted_urls.txt
├── requirements.txt
└── README.md
```

## 27. Operational philosophy

V1 uses a strict boundary between **retrieval**, **evidence**, **editorial generation**, and **publication**.

```text
Exa Search  → find
Exa Contents → verify / extract
Exa Agent → resolve difficult cases
Cerebras → rank / write
Python → enforce rules
Telegram → publish
```

The purpose of this separation is reliability. A provider can return an incomplete, nullable, redirected, or otherwise unexpected field without being allowed to crash the Telegram renderer or silently bypass coverage rules.



## 11. Production safety invariants

V1.1 treats every AI response as untrusted input. Before Telegram rendering, all optional values are normalized: `null` images become `{}`, missing lists become `[]`, and malformed optional fields are discarded safely.

The production orchestrator is tested separately from unit functions. The offline suite includes a mocked run through `v1_run_once()` so a production-only symbol/reference or control-flow error is caught before import.

Cerebras diagnostics preserve the actual HTTP status and safe provider message. In particular, HTTP 429 rate-limit failures are no longer reduced to a generic `ranking failed` message.

The live schedule/results pair does not use Exa fallback in V1.1. Live events come from the sports-data adapters; Exa is reserved for evergreen research.

## 12. Provider contracts

### Exa

- Search: `POST https://api.exa.ai/search`
- Contents: `POST https://api.exa.ai/contents`
- Agent: `POST https://api.exa.ai/agent/runs`, then `GET /agent/runs/{id}`
- Search discovery omits `outputSchema` and uses native result fields plus highlights.
- Contents batches up to 100 known URLs and uses a supported `maxAgeHours` policy.
- Agent output is small and structured; complex claims can return nullable fields, so the local parser normalizes them.

### Cerebras

- Chat endpoint: `POST https://api.cerebras.ai/v1/chat/completions`
- API Version 2 is explicitly requested via `X-Cerebras-Version-Patch: 2`.
- Default model: `gpt-oss-120b`.
- Primary response mode: strict `json_schema`.
- Ranking uses a bounded reservoir and a small response budget.
- Retryable provider classes: connection errors, 408/429/5xx.
- Authentication, permission, invalid-request, and unsupported-model errors are surfaced instead of being retried blindly.

## 13. Final test commands

```bash
python -m py_compile main.py
python main.py --self-test --fast
python -m unittest discover -s tests -v
python main.py --validate-config
```

A real production provider run still requires the GitHub secrets. The test suite does not pretend that mocked HTTP calls are proof of live provider success.


## V1.2.0 Production Hardening

V1.2.0 adds the final production guards identified during real GitHub Actions runs. Cerebras ranking is capped to the intended 20-candidate set with a compact payload and a small completion budget. Token-quota 429 responses are surfaced without blind retry loops. Internal datetime/date values are converted to ISO-8601 strings at JSON API boundaries. The V1 Telegram publisher never falls back to a plain text post: it uses native Rich Messages when possible, then a photo + structured caption, and finally a generated 1200×675 branded card + structured caption. A successful V1 story therefore always retains a visual presentation.
