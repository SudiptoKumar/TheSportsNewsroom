# The Sports Newsroom V1 1.4.0

Automated Telegram newsroom for evergreen Sports & Games knowledge, relevant internet-sourced photos, and a rotating live schedule/results pair for `@TheSportsNewsroom`.

## 1. Production engine

There is one production engine. Scheduled runs, `--dry-run`, diagnostics, and self-tests share the same operational entrypoint.

```text
Evergreen

Normal web search
   ↓
Sports/scope gate + source normalization
   ↓
20-sector reservoir + duplicate protection
   ↓
Cerebras ranking
   ↓
Normal source-page evidence retrieval
   ↓
Exa Contents fallback when normal evidence is insufficient
   ↓
Optional Exa Agent hard-case verification
   ↓
Cerebras editorial generation
   ↓
40–60 word deterministic validation
   ↓
Relevant image discovery + local verification
   ↓
Telegram publication
```

```text
Live pair

ESPN / TheSportsDB / CricketData
   ↓
Date + state validation
   ↓
Cross-source enrichment + sport diversity
   ↓
Major-event threshold
   ↓
SPORT + GAME + TIME/RESULT
   ↓
Rich H1 + table
   ↓
Publish replacement
   ↓
Persist new IDs
   ↓
Retire known older live IDs
   ↓
Batch delete + retry queue
```

## 2. Evergreen discovery

Normal web search is the default discovery layer. Exa Search is a recovery fallback only when ordinary search results are sparse. Exa is not required for every successful story.

The 20 sectors are:

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

A sector can remain uncovered without aborting the run. Only candidates that pass sports-scope and evidence gates are eligible for Cerebras editorial generation.

## 3. Evergreen quality gates

The discovery layer rejects dictionary/definition pages, obvious non-sports sources, gaming/esports, betting/rumours, invalid source URLs, current-news framing, and duplicate knowledge already present in the coverage index.

Verification requires readable evidence from a real source page. If normal page retrieval is insufficient, Exa Contents may be used. The bot never publishes an unverified search result merely because an AI verification path is unavailable.

## 4. Cerebras handling

Cerebras is used for ranking and editorial generation only.

- Primary model: configured `gpt-oss-120b` by default
- Ranking: medium reasoning when supported
- Editorial: low reasoning when supported
- Ranking budget: 768 completion tokens by default
- Editorial budget: 512 completion tokens by default
- Proactive spacing: 12.5 seconds between attempts
- Per-run provider-attempt cap: 16
- HTTP 429: honor provider reset information, with a 60-second minimum fallback and adaptive backoff
- HTTP 402: stop AI work for the run, do not retry credits/payment failures
- Three consecutive failed logical AI operations open the circuit

The run continues with partial evergreen output instead of allowing AI throttling to abort the live pair.

## 5. Evergreen post format

One renderer assembles the final Telegram HTML. The public structure is fixed:

```text
[Photo]

<b>Headline</b>

One short context paragraph of 40–60 words.

<b><a href="https://t.me/TheSportsNewsroom">Sports News</a></b> #TagOne #TagTwo

Source: <a href="URL">Source Name</a>
```

The channel name is bold and clickable on the same line as exactly two topic-relevant hashtags. The handle is not printed separately. Only one source is shown publicly, even when multiple sources were used internally. The source name is clickable and the raw URL is never visible. Every anchor-bearing Telegram text/caption is required to use `parse_mode=HTML`. The editorial body is 40–60 words, written in plain human language with concrete facts and natural sentence flow; em dashes and en dashes are rejected by the validator.

## 6. Image system

The bot tries source-page lead images first, then ordinary Bing Images search, then Exa image fallback. Every external image is downloaded and verified before Telegram receives it.

Checks include HTTP 200, supported image MIME type, maximum file size, decodability, minimum dimensions, blank/near-blank detection, and a reasonable aspect ratio. Verified JPEGs keep their original bytes; PNG/WebP candidates are normalized to JPEG before upload. No cropping is applied. If Telegram rejects an otherwise verified external image, the post is retried once with the branded card. Obvious low-quality/repost hosts are penalized or rejected. If no relevant verified image survives, the bot uses the branded 1200×675 fallback card.

## 7. Live pair

Every run computes:

- previous-day results = `run date - 1 day`
- next-day games = `run date + 1 day`

Results are only final events with a real score/result. Schedule rows are only future scheduled events. A major-event threshold prevents low-importance fixture dumps.

Both messages use a Rich H1 and three table columns (SPORT, GAME, TIME/RESULT):

```text
Previous-day results: SPORT | GAME | RESULT
Next-day games:       SPORT | GAME | TIME
```

The runner keeps one active schedule message and one active results message. Known older live message IDs from active state and historical V1 run records are queued for deletion after the replacement succeeds. Failed deletions remain queued for retry.

## 8. State management

`coverage_index.json` is the durable evergreen duplicate/coverage index and is bounded to 20,000 records.

`news_state.json` stores operational state. Mutable run history, daily records, publication state, and old post records are pruned using `STATE_RETENTION_DAYS`. Live retirement IDs are preserved until deletion succeeds.

## 9. GitHub Actions

`.github/workflows/newbot.yml`:

- pins the production application version to `1.4.0`
- runs repository structure and compile checks
- runs the offline self-test suite before production
- uses a 25-minute application deadline inside the 40-minute job
- uses normal web search as the primary discovery path
- keeps Exa credentials optional for successful normal-search runs
- commits state changes back to `main`

Workflow-dispatch modes are `run`, `diagnose`, `dry-run`, and `self-test`.

## 10. Local setup

```bash
python -m pip install -r requirements.txt
python main.py --self-test
python main.py --version
python main.py --validate-config
python main.py --dry-run
```

Required production secrets are `TELEGRAM_BOT_TOKEN` and `CEREBRAS_API_KEY`. `EXA_API_KEY` is optional and only needed for fallback research/image paths. `CRICKETDATA_API_KEY` is optional.

## 11. Repository tree

```text
.
├── .github/
│   └── workflows/
│       ├── import-zip.yml
│       └── newbot.yml
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
├── coverage_index.json
├── main.py
├── news_state.json
├── posted_urls.txt
├── README.md
└── requirements.txt
```

## 12. Version

**The Sports Newsroom V1 1.4.0**

## V1.4.0 hotfix

The GitHub Actions regression-test step now runs with external provider credentials cleared. Unit tests also isolate provider keys and explicitly disable the Exa image fallback for deterministic card-fallback tests. Production runs keep the Exa image recovery path enabled by default.

