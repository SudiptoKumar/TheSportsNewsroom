# The Sports Newsroom V1.3

Production-oriented Telegram newsroom for evergreen Sports & Games knowledge. V1.3 changes the operating principle from **fill 10 posts** to **earn 10 publish slots**. The bot can spend additional research time and will refuse weak filler.

## Editorial Goal

Every production day contains all 20 evergreen sectors exactly once across two runs:

- AM run: 10 unique sectors
- PM run: the other 10 unique sectors
- No sector overlap between the two runs
- No cross-sector knowledge-unit duplication inside the day
- Each run ends with exactly two temporary live posts: next-day major games schedule, then previous-day major results

The 10 evergreen posts in a run are treated as a set. The bot will not substitute a different sector just to reach ten.

## What makes V1.3 different

### 1. Quality before quantity
A story must pass a deterministic evidence-quality floor before it can reach the publication stage. Weak source pages, generic listicles, current-news leakage, duplicate knowledge, and unsupported claims are rejected. Hard historical sectors require independent source coverage.

### 2. Deterministic 10+10 sector plan
A stable date-based permutation creates an AM set of 10 sectors and a PM set of the remaining 10. The plan is persisted in `news_state.json`, so reruns cannot silently replace sectors or overlap the two runs.

### 3. Exa is the research desk
Exa Search discovers source pages. Exa Contents verifies selected source pages in batches. A deep Exa search is used only when a sector fails the first quality pass. The default pipeline does not use Exa Agent.

### 4. Cerebras is optional and bounded
Cerebras no longer ranks twenty candidates and no longer generates ten separate requests. It receives one compact batch request for the ten verified stories. If that request fails or is unavailable, V1.3 uses deterministic editorial fallbacks and continues without treating the AI provider as a production dependency.

### 5. Hub-length writing
Posts are deliberately compact: a 6-14 word headline, a 32-60 word summary, and three short key points. The goal is to give the reader the complete useful idea quickly, not write a long narrative.

### 6. Source attribution
The Telegram post shows a `Source` section with the source **name as the clickable text**. The raw URL is hidden behind the source name. The stored state still preserves source URLs for auditability.

### 7. Relevant imagery
The bot first uses a real Exa-discovered image connected to the selected source and subject. Images are scored for subject similarity, source relation, source quality, and obvious asset-quality failures such as logos, favicons and placeholders. If no usable real image survives, a branded 1200x675 visual card is generated. Plain text is never used as the visual fallback.

## The 20 Evergreen Sectors

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

## Evergreen exclusions

Current scores, results, fixtures, schedules, standings, transfers, injuries, betting, rumors, breaking news, current previews, current recaps, and other short-lived sports information do not belong in the evergreen desk.

## Production pipeline

```text
Persisted daily sector plan
        ↓
10 assigned sectors for this run
        ↓
Exa Search discovery
        ↓
URL + knowledge-unit + cross-sector dedupe
        ↓
Deterministic quality gate
        ↓
Exa Contents batch verification
        ↓
Secondary source check for hard/weak cases
        ↓
Deep Exa recovery only for failed sectors
        ↓
Relevant-image selection
        ↓
ONE optional Cerebras batch copy pass
        ↓
Deterministic editorial validation
        ↓
Pre-publication 10-story contract check
        ↓
Telegram photo-first publication
        ↓
Live schedule + live results
        ↓
Coverage/state persistence
```

## Failure policy

The bot does not silently fill a missing sector with another sector. If a sector cannot meet the evidence-quality floor after normal and deep recovery, the evergreen phase exits without publishing a fake substitute. This is intentional: a missing post is preferable to a low-quality post pretending to be useful.

Cerebras failures are non-fatal. A 429, malformed response, quota issue, or unavailable key causes V1.3 to use deterministic editorial output for the affected run.

Telegram publication is photo-first. If a real image send fails, a generated branded card is attempted. The bot never intentionally downgrades an evergreen story to `sendMessage` plain text.

## State files

- `news_state.json`: daily sector plan, publication ledger, live pair, run history and post metadata
- `coverage_index.json`: permanent evergreen knowledge coverage and source history
- `posted_urls.txt`: legacy compatibility / historical URL record

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
│   ├── exa_hard_case_agent_v1.txt
│   └── cerebras_editorial_v1.txt
├── schemas/
│   └── exa_agent_hard_case_v1.json
├── tests/
│   └── test_v1.py
└── .github/workflows/
    ├── newbot.yml
    └── import-zip.yml
```

## Required environment

```text
EXA_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL=@TheSportsNewsroom
```

Optional:

```text
CEREBRAS_API_KEY=...
CEREBRAS_MODEL=gpt-oss-120b
TELEGRAM_ADMIN_CHAT_ID=...
CRICKETDATA_API_KEY=...
V1_CEREBRAS_BATCH_ENABLED=1
V1_QUALITY_FLOOR=78
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

A production run is:

```bash
python main.py
```

## GitHub Actions

The workflow runs twice per day using GitHub Actions cron in UTC. The live pair is generated only after the evergreen set is successfully prepared and is published as the final two Telegram messages. Previous live messages are deleted only after the new pair has been successfully published and persisted.

## Provider design

Exa Search is used for discovery because the endpoint supports source filtering, highlights/text extraction, categories, and deeper search modes. V1.3 avoids giant Search `outputSchema` contracts and uses ordinary result retrieval for discovery. Exa Contents is used for already-known URLs.

Cerebras structured output is constrained to one small batch schema. The current `gpt-oss-120b` path uses strict JSON schema mode when available, but the bot does not depend on it for correctness.

## Quality contract

A production evergreen story must have:

- exactly one assigned sector
- a new underlying knowledge unit
- no same-day cross-sector duplication
- evergreen subject matter
- evidence extracted from the discovered source pages
- source quality strong enough for the sector
- a higher evidence bar for hard historical claims
- hub-length publication copy
- a valid source hyperlink
- a usable real image or generated branded visual

## Version

**V1.3.0 — Quality Hub redesign**
