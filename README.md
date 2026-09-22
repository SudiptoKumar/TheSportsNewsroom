# The Sports Newsroom V1.4

Production-oriented Telegram newsroom for evergreen Sports & Games knowledge.

V1.4 changes the system from **“find a few stories and fight to fill ten slots”** into a **daily research batch**:

```text
20 sectors
   ↓
10 Exa discoveries per sector
   ↓
≈200 raw candidates
   ↓
Exa native relevance ranking + deterministic tie-breaks
   ↓
1 winner + up to 2 reserves per sector
   ↓
20 selected knowledge units
   ↓
Exa Contents verification
   ↓
20 research packages
   ↓
ONE Cerebras editorial batch
   ↓
20 polished stories
   ↓
AM publishes 10
   ↓
PM publishes the other 10
```

The same 20-story daily batch is persisted between GitHub Actions runs. The evening run never performs a second discovery pass for the same day unless the stored batch is missing or incomplete.

## Editorial goal

The newsroom publishes durable Sports & Games knowledge, not daily sports-news reporting.

Each day contains all 20 evergreen sectors exactly once:

- AM: 10 sectors
- PM: the remaining 10 sectors
- zero sector overlap
- exactly one selected knowledge unit per sector
- permanent historical coverage memory prevents repeating the same underlying topic

The two temporary live posts remain separate from the evergreen system. Every successful run ends with:

1. Next-Day Major Games Schedule
2. Previous-Day Major Game Results

The bot publishes the new live pair first and deletes the previous live pair only after both new messages have been successfully created.

## The 20 evergreen sectors

1. Sport Discovery
2. Game Discovery
3. Interesting Sports Fact
4. Interesting Game Fact
5. Rule Check
6. How to Play
7. Sport Origin
8. Game Origin
9. On This Date
10. First / Last / Only
11. Records & Milestones
12. Forgotten Sport
13. Forgotten Game
14. Equipment / Measurement
15. Why Does This Happen?
16. Then vs Now
17. New Sport Discovery
18. New Tabletop Game Discovery
19. Traditional / Regional Game
20. Sports & Games People

## Quality-first discovery

V1.4 does not ask AI to “come up with ten posts”. Exa searches each sector independently and supplies up to ten ranked results. The target is 200 candidates across the full 20-sector matrix. Hard sectors have multiple targeted query variants and continue through those variants until their ten-result reservoir is filled or the available query family is exhausted.

Exa Search already returns ranked, relevant results with relevance scores, so the V1.4 selector uses Exa’s native ranking as its primary retrieval signal rather than inventing a second AI ranking stage. Exa also supports page contents and highlights for downstream evidence extraction. citeturn918216search1turn918216search0

The local selector only protects the newsroom contract:

- one winner per sector
- historical coverage dedupe
- cross-sector knowledge-unit dedupe
- obvious current-news exclusion
- URL integrity
- source-quality tie-breaks

There is no generic quality score that can force a topic into publication just to reach a quota.

## Why 20 winners are selected before Cerebras

The research batch must contain one strong candidate from every sector before editorial writing begins. This prevents Cerebras from deciding which sectors exist and prevents a weak sector from being replaced by another sector.

The selected item can also have up to two ranked reserves from the same sector. A reserve is used only when the selected source cannot be retrieved. A reserve never changes the sector.

## Exa Contents verification

After selection, the bot sends the known URLs to Exa Contents in batches. Contents is designed for known URLs and can return text, highlights, summaries and metadata, with per-URL status handling for mixed success. citeturn736336search1turn736336search8

The bot requests source text plus relevant highlights and keeps the source URL attached to the evidence. The evidence package is what reaches Cerebras.

The bot does not use Exa Agent as a normal step in V1.4. Search is sufficient for broad discovery and Contents is sufficient for selected-page verification; deeper agentic research would only be added after a measured need is demonstrated.

## Cerebras editorial

Cerebras is the writing desk, not the research desk.

The selected 20 research packages are sent in **one structured editorial request** using strict JSON Schema. This eliminates the earlier pattern of one Cerebras request per story that could run into request-per-minute throttling.

The default model remains `gpt-oss-120b` and the structured response is validated locally before anything can reach Telegram.

Editorial style is **micro-storytelling**:

```text
Useful hook
   ↓
Context
   ↓
Distinctive detail
   ↓
Memorable factual takeaway
```

This is deliberately not:

```text
“Imagine you are standing…”
“Have you ever wondered…”
long blog narrative
vlog narration
fictional scene
```

The normal target is roughly 120-150 words of useful editorial content including the lead, story, key details and takeaway. Telegram length fitting removes optional material only when necessary to stay inside the platform caption limit.

## Source display

The Telegram post displays:

```text
Source
FIFA
```

`FIFA` is the clickable Telegram link. The raw URL is hidden from the reader but preserved internally in state and coverage records.

## Image strategy

Images are selected separately from the editorial model.

Priority:

1. real image supplied by the selected source page
2. real image from the independent selected source
3. targeted Exa image discovery for the exact subject
4. branded 1200×675 fallback card

Image relevance is scored using subject/title similarity, source relationship and source quality. Obvious logos, favicons, placeholders, sprites, avatars and invalid assets are rejected.

There is no plain-text visual fallback for evergreen posts.

## AI failure behavior

V1.4 is designed so that one bad AI object does not destroy the researched batch.

- A malformed editorial item gets deterministic fallback text based only on stored evidence.
- A complete Cerebras batch failure does **not** publish low-quality copy. The verified research batch remains in `news_state.json` and the next run retries the editorial stage without paying for another 200-result discovery batch.
- Cerebras rate-limit, authentication and transient errors are handled by the existing bounded AI client. There are no random request-shape mutations.

The research batch is the durable checkpoint between Exa and Cerebras.

## State and idempotency

`news_state.json` stores:

- daily 20-sector plan
- daily discovery counts
- the 20 selected candidates
- up to two reserves per sector
- the 20 final editorial stories
- published sectors and Telegram message IDs
- live schedule/result message IDs
- run history and provider telemetry

`coverage_index.json` stores long-term evergreen knowledge coverage and is used to prevent repeating the underlying knowledge unit with a new headline.

`posted_urls.txt` remains for legacy URL compatibility.

State is saved after successful evergreen publication steps. A corrupted or unreadable state file causes the bot to stop rather than create a blank state and risk duplicates. State serialization converts datetimes and dates to safe ISO strings, and daily research checkpoints older than seven days are pruned to keep Git state bounded.

## Failure-safe live rotation

The old live pair is never deleted before the new pair is complete.

```text
Create new schedule
        ↓
Create new results
        ↓
Both verified?
   ├─ no → delete new partial post when possible; keep old pair
   └─ yes
        ↓
Persist new pair
        ↓
Delete old pair
```

If the result post fails after the new schedule is created, the bot attempts to remove that new schedule and keeps the previous pair.

## Production pipeline

```text
Daily state check
      ↓
20-sector plan
      ↓
Exa discovery: up to 10 × 20 ≈ 200 candidates
      ↓
URL + coverage + same-day knowledge dedupe
      ↓
Exa-native ranked selection
      ↓
20 sector winners + reserves
      ↓
Exa Contents verification
      ↓
20 persisted research packages
      ↓
Cerebras one-batch editorial generation
      ↓
Per-story deterministic validation
      ↓
Image selection
      ↓
20 persisted editorial stories
      ↓
Publish current run's 10 sectors
      ↓
Live schedule
      ↓
Live results
      ↓
Persist state
      ↓
PM reuses the same daily batch for the other 10 sectors
```

## Required secrets

```text
EXA_API_KEY=...
CEREBRAS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
```

## Optional variables

```text
TELEGRAM_CHANNEL=@TheSportsNewsroom
TELEGRAM_ADMIN_CHAT_ID=...
CRICKETDATA_API_KEY=...
CEREBRAS_MODEL=gpt-oss-120b
V1_DISCOVERY_RESULTS_PER_SECTOR=10
V1_DISCOVERY_MIN_RESULTS_PER_SECTOR=10
V1_MAX_RESERVES_PER_SECTOR=2
V1_CONTENTS_CHARS=7000
V1_IMAGE_SEARCH_RESULTS=5
V1_SEARCH_DELAY_SECONDS=0.15
V1_EDITORIAL_MAX_TOKENS=6500
V1_CEREBRAS_BATCH_ENABLED=1
V1_ALLOW_EDITORIAL_FALLBACK=0
POST_DELAY_SECONDS=3
RUN_DEADLINE_SECONDS=2100
```

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --version
python main.py --self-test --fast
python -m unittest discover -s tests -v
python main.py --validate-config
```

Production:

```bash
python main.py
```

Dry run:

```bash
python main.py --dry-run
```

## GitHub Actions

The production workflow runs twice per day in UTC:

- 02:15 UTC ≈ 08:15 Asia/Dhaka
- 14:15 UTC ≈ 20:15 Asia/Dhaka

The AM run normally creates the daily 200→20 research batch and publishes its 10 sectors. The PM run restores the already-prepared 20-story batch and publishes the other 10 sectors. If AM research completed but Cerebras editorial failed, PM retries editorial from the stored research checkpoint rather than repeating Exa discovery.

## Repository structure

```text
TheSportsNewsroom/
├── main.py
├── news_state.json
├── coverage_index.json
├── posted_urls.txt
├── requirements.txt
├── README.md
├── prompts/
│   ├── exa_sector_discovery_v1.txt
│   ├── exa_contents_verification_v1.txt
│   └── cerebras_editorial_v1.txt
├── schemas/
│   └── exa_agent_hard_case_v1.json
├── tests/
│   └── test_v1.py
└── .github/workflows/
    ├── newbot.yml
    └── import-zip.yml
```

## Production test philosophy

The repository tests both pure logic and provider-boundary contracts. The V1.4 suite checks:

- exactly 20 sectors
- exact 10+10 daily split with no overlap
- 10-target discovery per sector
- no Exa Search outputSchema dependency
- one winner per sector from the 200-candidate matrix
- same-knowledge protection across sectors
- persistent 20-story batch behavior
- one Cerebras request for 20 stories
- strict nested JSON schema
- unsupported numeric-claim rejection
- nullable AI output normalization
- clickable source-name rendering
- photo-first publication
- transactional live-pair rotation
- legacy state safety

## Version

**V1.4.0 — Daily 200→20 Quality Hub**
