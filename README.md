# The Sports Newsroom V4

> A production-oriented Telegram sports-and-games newsroom built around evergreen research, permanent editorial memory, AI-assisted ranking and editing, verified web imagery, and a separate temporary live-sports pair.

**Version:** `4.1.1`  
**Target channel:** `@TheSportsNewsroom`  
**Runtime:** Python `3.12`  
**Deployment:** GitHub Actions  
**Timezone:** `Asia/Dhaka`  
**Runs:** `2 per day`

---

## 1. What V4 Does

Every production run is split into two independent layers.

```text
EVERGREEN ENGINE
Exa research → Coverage Memory → Cerebras ranking → Cerebras editorial → Image verification → 10 posts

LIVE SPORTS ENGINE
Structured sports adapters → Major-event filter → Native Telegram Rich Message tables → 2 temporary posts
```

A successful run publishes:

```text
10 evergreen posts
+
1 Next-Day Major Games table
+
1 Previous-Day Major Results table
```

The two live posts are always the final two messages of the run.

With two successful runs per day:

```text
20 evergreen posts / day
2 live temporary posts
22 active channel posts
```

The evergreen layer is permanent channel content. The live pair is rotated on every successful run.

---

## 2. The V4 Editorial Principle

```text
Exa discovers
      ↓
Coverage remembers
      ↓
Cerebras ranks
      ↓
Cerebras edits
      ↓
Image engine verifies
      ↓
Code validates
      ↓
Telegram publishes
```

Exa is the research engine. Cerebras is the editorial AI. Deterministic Python remains the final authority for validation, deduplication, state, publication safety, and live-message rotation.

---

## 3. Evergreen Content Model

Each Exa research package contains exactly 20 evergreen sectors, one per sector:

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

The Exa research prompt is kept in `prompts/exa_daily_evergreen_v4.txt` and is used verbatim with V4 coverage context injected into its history placeholders.

Evergreen research excludes temporary current sports information such as live schedules, recent results, transfers, rumours, injuries, betting, current standings, breaking news, and current previews/reports.

---

## 4. Two-Run Daily Diversity

The bot keeps a daily sector ledger.

### Run 1

The full 20-sector Exa package is researched. Coverage memory removes already-covered knowledge and the top 10 are selected.

### Run 2

Run 1's published sectors are already in state. The second run therefore prioritizes the unused sectors and the final daily set spans the full 20-sector matrix.

This prevents the second run from simply publishing ten more posts from the same editorial areas.

---

## 5. Coverage Memory

`coverage_index.json` is the permanent editorial memory.

It survives Telegram message deletion and is not coupled to the temporary live-message rotation.

Each record stores concepts such as:

```json
{
  "content_id": "...",
  "content_type": "evergreen",
  "sector": "Sport Origin",
  "normalized_subject": "...",
  "central_knowledge_unit": "...",
  "central_claim": "...",
  "editorial_angle": "...",
  "sport_or_game": "...",
  "country": "...",
  "region": "...",
  "people": [],
  "published_at": "...",
  "run_id": "...",
  "telegram_message_id": 123,
  "source_urls": [],
  "fingerprints": []
}
```

### Duplicate protection layers

```text
1. Canonical URL
2. Content fingerprint
3. Normalized subject
4. Central knowledge unit
5. Central claim
6. Semantic similarity
7. Permanent historical coverage
8. Live event identity for sports events
```

This means a story cannot become "new" just because the headline or source URL changed.

At the same time, related subjects remain possible when their knowledge unit is materially different. For example, publishing a football origin story does not block later coverage of football pitch dimensions or referee history.

---

## 6. Exa Research

V4 uses Exa in two separate research passes.

### Pass 1: discovery

```text
POST https://api.exa.ai/search
Type: deep
Prompt: prompts/exa_daily_evergreen_v4.txt
Schema: schemas/exa_daily_sports_games_output_schema_v1.json
```

The structured discovery schema is intentionally compact. It returns only the fields required to identify and compare 20 candidates: sector, normalized subject, central knowledge unit, central claim, and research focus. The application validates the provider schema contract before the request.

Before discovery, V4 builds a compact exclusion context from `coverage_index.json` and injects it into the prompt.

### Pass 2: selected evidence

After Cerebras selects the top 10, Exa performs a second deep-research pass using `prompts/exa_selected_evidence_v4.txt` and `schemas/exa_selected_evidence_v4.json`. This pass supplies evidence text, source URLs, and real image candidates for those ten subjects.

The local validator remains authoritative. Discovery candidates are not treated as publication-ready until coverage checks, selected evidence, Cerebras editorial transformation, image validation, and the final quality gate all pass.

The code never invents a missing research item simply to reach 20.

## 7. Cerebras Editorial AI

Cerebras is used in two main editorial stages.

### Stage A: ranking

All valid Exa candidates are ranked on factors such as:

- novelty
- evidence strength
- audience interest
- visual potential
- evergreen longevity
- historical or educational value
- global variety
- repetition risk

The ranking prompt is `prompts/cerebras_rank_v4.txt`.

### Stage B: editorial transformation

The selected 10 candidates are turned into publication-ready Telegram stories.

The editorial prompt is `prompts/cerebras_editorial_v4.txt`.

The editor receives only the researched candidate, supplied evidence, and verified image candidates. It is explicitly forbidden from adding unsupported facts.

The result includes:

```text
headline
short deck
hook
body
key points
why it is interesting
caption
hashtags
editorial angle
image choice
```

Different sectors can use different editorial angles instead of forcing every post into one identical template.

---

## 8. Real Image Discovery

V4 prefers a real relevant image rather than a generated generic card.

The image path is:

```text
Exa image candidates
      ↓
URL/source validation
      ↓
Cerebras image selection
      ↓
Final deterministic checks
      ↓
Telegram Rich Message photo block
```

Stored image metadata can include:

```text
image URL
source page URL
source name
alt text
caption
source type
verification status
```

The system never fabricates an image URL or silently claims an unsupported license.

When no acceptable image survives validation, the post is explicitly marked as image-unavailable rather than pretending a missing image exists.

---

## 9. Live Sports Layer

The live layer does not use Exa.

It reuses the structured sports/event adapters already proven in V3 and filters them into two consolidated publications:

```text
NEXT-DAY MAJOR GAMES

SPORT | GAME | TIME | COMPETITION
```

and:

```text
PREVIOUS-DAY MAJOR RESULTS

SPORT | GAME | RESULT | COMPETITION
```

Only major events are retained. Event importance considers competition tier, tournament stage, prominence, and other source-provided metadata.

Multiple source reports for the same event are collapsed with a stable event identity derived from sport, competition, date, and participants.

---

## 10. Telegram Rich Messages

V4 uses Telegram's native `sendRichMessage` method for the structured live tables and the evergreen Rich Message renderer.

Live tables are generated with:

```text
is_bordered = true
is_striped = true
is_compact = true
```

The current Telegram Bot API documents `sendRichMessage`, `InputRichBlockTable`, table captions, and the `is_compact` field. These Rich Message capabilities were introduced across Bot API 10.1–10.3.

The publisher retains a compatibility fallback for evergreen posts when a rich-message request is explicitly rejected.

---

## 11. Safe Live Rotation

The previous live pair is never deleted first.

V4 performs:

```text
Generate new schedule
Generate new results
        ↓
Validate both
        ↓
Publish new schedule
Publish new results
        ↓
Verify both message IDs
        ↓
Delete old live pair
        ↓
Save new live state
```

If either new live post fails, the old pair remains intact.

This is intentionally different from the permanent evergreen layer, which is never removed by live rotation.

---

## 12. Idempotent Publication

Every post receives a stable publication ID.

Example:

```text
2026-09-22-AM:evergreen:<fingerprint>
2026-09-22-AM:live_schedule:<fingerprint>
2026-09-22-AM:live_results:<fingerprint>
```

A retry after a successful Telegram publication checks this ID first and does not publish the same logical post again.

Uncertain Telegram responses are recorded separately so a timeout does not silently become a duplicate publication.

---

## 13. Repository Structure

```text
TheSportsNewsroom/
│
├── main.py
├── news_state.json
├── posted_urls.txt
├── coverage_index.json
├── requirements.txt
├── README.md
│
├── prompts/
│   ├── exa_daily_evergreen_v4.txt
│   ├── cerebras_rank_v4.txt
│   ├── cerebras_editorial_v4.txt
│   └── cerebras_duplicate_review_v4.txt
│
├── schemas/
│   ├── exa_daily_sports_games_output_schema_v1.json
│   └── exa_selected_evidence_v4.json
│
├── tests/
│   ├── __init__.py
│   └── test_v4.py
│
└── .github/
    └── workflows/
        ├── newbot.yml
        └── import-zip.yml
```

`main.py` remains intentionally compact and reuses the mature V3 adapters and reliability helpers instead of creating unnecessary framework or database dependencies.

---

## 14. Dependencies

```text
Python 3.12+
requests
Pillow
```

No database server is required.

No Node.js runtime is required.

No web framework is required.

API integrations are performed through REST calls.

---

## 15. GitHub Secrets

Configure these under:

```text
Repository
→ Settings
→ Secrets and variables
→ Actions
```

### Required

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

### Optional

```text
CEREBRAS_MODEL
TELEGRAM_ADMIN_CHAT_ID
CRICKETDATA_API_KEY
```

Repository variable:

```text
TELEGRAM_CHANNEL
```

Defaults to:

```text
@TheSportsNewsroom
```

Never commit API keys to the repository.

---

## 16. Telegram Setup

1. Create the bot with BotFather.
2. Add it to `@TheSportsNewsroom`.
3. Give it administrator rights required for publishing and deleting its own live messages.
4. Add `TELEGRAM_BOT_TOKEN` to GitHub Secrets.
5. Set `TELEGRAM_CHANNEL` as a repository variable when the default channel is not used.

Use the manual `diagnose` workflow input before production operation.

---

## 17. Local Installation

```bash
git clone <your-repository-url>
cd TheSportsNewsroom

python -m venv .venv
```

### Windows

```powershell
.venv\Scripts\Activate.ps1
```

### Linux / macOS

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Set environment variables.

### PowerShell

```powershell
$env:EXA_API_KEY="..."
$env:CEREBRAS_API_KEY="..."
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHANNEL="@TheSportsNewsroom"
```

### Linux / macOS

```bash
export EXA_API_KEY="..."
export CEREBRAS_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHANNEL="@TheSportsNewsroom"
```

---

## 18. Testing

### Compile

```bash
python -m py_compile main.py
python -m py_compile tests/test_v4.py
```

### V4 self-test

```bash
python main.py --self-test
```

This runs the preserved V3 regression suite plus the V4 architecture checks.

### Standalone unit tests

```bash
python -m unittest discover -v
```

The test suite covers:

```text
sector matrix
coverage duplicate detection
related-topic handling
live event identity
source-event collapse
Rich Message table structure
image blocks
editorial validation
current-news rejection
Exa package validation
publication idempotency
image URL validation
stable publication IDs
```

All tests are offline and do not require production credentials.

---

## 19. Workflow Schedule

### Importing a new ZIP

The repository importer does not guess which ZIP to use. In **Import ZIP into Repository**, choose **Import** and enter the exact ZIP filename, for example:

```text
TheSportsNewsroom-v4.1.1.zip
```

The workflow verifies the extracted project, checks the Exa schemas, runs the V4 self-test, runs the standalone unit tests, and only then commits the import. This prevents an older V3/V4 package left in the repository root from being imported accidentally.


The production GitHub Actions workflow runs twice per day:

```text
02:15 UTC → 08:15 Asia/Dhaka
14:15 UTC → 20:15 Asia/Dhaka
```

Manual modes:

```text
run       → production V4 pipeline
diagnose  → credential/channel/state checks
dry-run   → no Telegram publication and no state writes
self-test → full offline regression suite
```

The workflow validates the repository before running and commits:

```text
news_state.json
posted_urls.txt
coverage_index.json
```

State pushes use retrying rebase/push logic so a concurrent repository update is less likely to lose state.

---

## 20. Operational Rules

### Never do this

```text
Use Exa as the live schedule source
Treat a changed headline as a new topic
Delete coverage memory with live messages
Publish an incomplete Exa package as a full package
Invent facts to reach ten posts
Delete old live posts before new live posts are verified
```

### Always do this

```text
Remember published evergreen knowledge
Check coverage before publishing
Rank the complete valid research package
Validate generated copy after Cerebras
Verify external image URLs
Keep live schedule/results separate from evergreen history
Publish the live pair last
Persist state only after confirmed operations
```

---

## 21. Production Success Invariant

After every successful run:

```text
10 new evergreen posts
+
1 new next-day major-games table
+
1 new previous-day major-results table
```

The live pair is the last two messages.

After two successful runs:

```text
20 evergreen posts
+
1 active schedule post
+
1 active results post
```

The coverage index continues growing even when the live pair is replaced.

---

## 22. Version

```text
The Sports Newsroom V4.1.0
```

Core principle:

```text
EXA FINDS
COVERAGE REMEMBERS
CEREBRAS JUDGES
CEREBRAS EDITS
IMAGE ENGINE FINDS
CODE VERIFIES
TELEGRAM PRESENTS
STATE CONTROLS WHAT SURVIVES
```


# V4.1 Research Boundary

V4.1 separates Exa research from editorial generation. Exa first returns a compact 20-sector discovery package that stays inside Exa's current structured-output contract. A second Exa pass researches evidence, source URLs, and image candidates for the ten subjects chosen by Cerebras. Cerebras remains the ranking and editorial layer.

The Exa discovery schema intentionally contains only the minimum fields needed for coverage and ranking: sector, normalized subject, central knowledge unit, central claim, and research focus. The application verifies the schema property/depth limits locally before making the network request.

Run 2 still researches the complete 20-sector package. It ranks the full package first, then the local selector removes sectors already published during Run 1.

Self-test telemetry is isolated from the production GitHub Step Summary so simulated records cannot be mistaken for real Telegram publication telemetry.
