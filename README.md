# The Sports Newsroom

> **Sports, games, rules, history, discoveries, and dated event intelligence in one Telegram newsroom.**

**The Sports Newsroom** is a single-file Python newsroom engine built for `@TheSportsNewsroom`. It is designed to go beyond conventional sports-news bots that simply scrape headlines and repost scores.

The project combines:

**Discovery → Source collection → Filtering → AI understanding → Verification → Deduplication → Editorial selection → Rich Telegram publishing → Persistent state**

It covers both **real-world sports** and **physical/tabletop games**, while deliberately excluding video games, esports, gaming hardware, patches, DLC, Steam, and similar gaming-industry content.

---

## 1. What This Project Does

The Sports Newsroom automatically searches for and publishes several kinds of content.

### Daily sports intelligence

The bot builds dated posts for:

- **NEXT UP · [EXACT DATE]**
  - Upcoming notable sports events
  - Fixtures, matches, races, tournaments, finals, and other scheduled events
  - Uses an explicit calendar date rather than disposable words such as “tomorrow”

- **THE DAY IN SPORTS · [EXACT DATE]**
  - Previous day's notable sports results
  - Major events, records, milestones, and historical significance
  - Uses an explicit calendar date rather than “yesterday”

### Evergreen discovery

The bot also searches for content that remains useful after the day it was published:

- Interesting sports facts
- Unknown or unusual games
- Board games
- Card games
- Traditional and regional games
- Rules and rule myths
- Official rule vs house rule
- How to play
- Game and sport origins
- Etymology and terminology
- Historical events
- “On this date”
- “100 years ago”
- First / last / only events
- Forgotten sports and games
- Rule changes
- Unusual traditions
- Records and milestones
- Equipment and measurement explanations
- New sports and new tabletop games
- “Why does this happen?” explanations
- Then vs now comparisons

The goal is not simply to find the most popular story.

The goal is to find **useful, interesting, verifiable sports-and-games knowledge** that a reader may want to save or share.

---

# 2. Why It Is Different

Most automated Telegram news bots follow a simple pattern:

```text
RSS / Website
      ↓
Find article
      ↓
Rewrite title
      ↓
Post
```

The Sports Newsroom uses a broader editorial pipeline:

```text
              ┌─────────────────────┐
              │  Exa / RSS Search   │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Candidate Collection│
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Hard Exclusion      │
              │ Video games/esports │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Source Extraction   │
              │ & Normalization     │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Cerebras AI         │
              │ Classification /    │
              │ Editorial Analysis  │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Verification        │
              │ & Grounding         │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Event / Claim       │
              │ Deduplication       │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Editorial Selection │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Rich HTML + Image   │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Telegram Publishing │
              └──────────┬──────────┘
                         ↓
              ┌─────────────────────┐
              │ Persistent State    │
              └─────────────────────┘
```

This makes the project closer to a **small automated editorial desk** than a basic scraper.

---

# 3. What Problem It Solves

Traditional sports automation has several recurring problems:

### Problem 1: Everything becomes generic news

A normal scraper tends to produce:

> Team A beat Team B.

The Sports Newsroom can instead search for:

> Why is this rule written this way?

> Where did this game come from?

> What happened on this date in sports history?

> What is the official rule, and what is only a house rule?

> What unusual sport or game is worth discovering?

---

### Problem 2: Repeated stories

Different websites can report the same underlying event.

The bot therefore uses URL normalization, similarity checks, claim/event-oriented keys, and persistent state to reduce repeated publishing.

---

### Problem 3: Video-game contamination

The project is explicitly about **sports and physical/tabletop games**, not video games.

It contains hard exclusion terms for content involving areas such as:

- PlayStation
- Xbox
- Steam
- DLC
- Patch notes
- Consoles
- Esports
- Video games
- PC gaming
- Mobile gaming
- Gaming hardware

This filtering happens before normal editorial selection.

---

### Problem 4: Disposable date language

Daily posts should remain understandable when someone reads them later.

Instead of:

> Tomorrow's matches

the newsroom uses:

> **NEXT UP · 21 SEPTEMBER 2026**

Instead of:

> Yesterday's results

it uses:

> **THE DAY IN SPORTS · 20 SEPTEMBER 2026**

This makes archived Telegram content easier to understand and search.

---

### Problem 5: AI-generated claims without grounding

AI is not treated as the source of truth.

The system gathers source material first and uses Cerebras for structured classification and editorial generation.

The generated story is then checked against available source evidence before publishing.

---

# 4. What It Does NOT Do

This project intentionally does **not** aim to be:

- A video-game news bot
- An esports news bot
- A gaming hardware news bot
- A transfer-rumor bot
- A betting bot
- A gossip bot
- A generic sports-score feed
- A simple RSS forwarder
- A scraper that blindly reposts every article
- A replacement for official sporting bodies or rulebooks
- A guarantee that every discovered source is authoritative

For rules, historical claims, records, and other factual content, source quality still matters. Discovery sources can identify a lead, but important claims should be supported by stronger evidence where possible.

---

# 5. Content Scope

## Sports

The project includes many sports categories, including examples such as:

- Football
- Cricket
- Basketball
- Tennis
- Badminton
- Table tennis
- Volleyball
- Baseball
- Rugby
- Golf
- Boxing
- MMA
- Formula 1
- Athletics
- Swimming
- Cycling
- Gymnastics
- Wrestling
- Fencing
- Archery
- Rowing
- Sailing
- Surfing
- Hockey
- Snooker
- Billiards
- Darts
- Squash
- Bowling
- Handball
- Kabaddi
- Kho kho
- Sepak takraw
- Judo
- Karate
- Taekwondo
- Weightlifting
- Triathlon
- And other physical sports

## Games

The project also covers physical and tabletop games such as:

- Chess
- Go
- Shogi
- Backgammon
- Monopoly
- Catan
- Carrom
- Mahjong
- Scrabble
- Risk
- Ticket to Ride
- Snakes and Ladders
- Pachisi
- Mancala
- UNO
- Rummy
- Hearts
- Spades
- Bridge
- Cribbage
- Mafia
- Werewolf
- Codenames
- Pictionary
- Charades
- Traditional games
- Mind games

The exact coverage is controlled by the category and search configuration inside `main.py`.

---

# 6. Source Strategy

The project does not depend on one website.

It uses several source groups.

### Official / primary sources

Examples include:

- FIFA
- UEFA
- ICC
- World Athletics
- FIDE
- ITF
- World Badminton
- Formula 1 / FIA
- World Rugby
- Olympics
- Paralympics
- World Boxing
- IJF
- World Archery
- World Rowing
- World Aquatics
- UCI
- FIBA

These are especially useful for official schedules, rules, competitions, and governing-body information.

### Secondary sources

Examples include:

- BBC Sport
- ESPN
- Sky Sports
- The Guardian
- Reuters
- AP
- NBC Sports
- CBS Sports
- Sports Illustrated

### Reference / discovery sources

Examples include:

- Wikipedia
- Britannica
- Guinness World Records
- Atlas Obscura
- BoardGameGeek
- Dicebreaker
- Tabletop Gaming

### Lead-only sources

The project also recognizes:

- Reddit
- Quora

These are treated as discovery leads rather than automatic proof.

---

# 7. APIs and External Services

The project uses three main external services.

## Exa

**Purpose:** web discovery and source retrieval.

Exa is used to search for:

- Current sports events
- Upcoming schedules
- Previous-day results
- Historical sports events
- Evergreen sports facts
- Game history
- Rules
- New games
- Interesting discoveries

Required environment variable:

```text
EXA_API_KEY
```

---

## Cerebras

**Purpose:** AI classification, structured analysis, editorial selection, and story generation.

Default model:

```text
gpt-oss-120b
```

Required environment variable:

```text
CEREBRAS_API_KEY
```

Optional model override:

```text
CEREBRAS_MODEL
```

The code uses structured JSON-schema responses for important AI stages rather than relying on unrestricted prose.

---

## Telegram Bot API

**Purpose:** final publication to the Telegram channel.

Required environment variable:

```text
TELEGRAM_BOT_TOKEN
```

Default channel:

```text
@TheSportsNewsroom
```

Optional override:

```text
TELEGRAM_CHANNEL
```

The bot publishes the generated story together with a generated visual.

---

# 8. Python Libraries

`requirements.txt` contains:

| Library | Purpose |
|---|---|
| `exa-py` | Exa search and discovery |
| `cerebras_cloud_sdk` | Cerebras AI API |
| `requests` | HTTP requests and Telegram API communication |
| `urllib3` | HTTP retry support |
| `beautifulsoup4` | HTML parsing |
| `Pillow` | Image generation and visual cards |
| `feedparser` | RSS feed parsing |
| `trafilatura` | Article/source text extraction |

Python version:

```text
Python 3.12
```

No database server is required.

No Node.js runtime is required.

No framework is required.

---

# 9. Repository Structure

The project intentionally follows a very small CareerNewsroom-style repository structure.

```text
TheSportsNewsroom/
│
├── main.py
├── news_state.json
├── posted_urls.txt
├── requirements.txt
│
└── .github/
    └── workflows/
        ├── newbot.yml
        └── import-zip.yml
```

There is deliberately no:

```text
src/
tests/
package.json
node_modules/
```

The bot is designed as a compact single-file Python deployment.

---

## `main.py`

The complete newsroom engine.

It contains:

- Configuration
- Source registry
- Search queries
- RSS discovery
- URL normalization
- Candidate collection
- Video-game filtering
- Source extraction
- AI client handling
- Classification
- Editorial generation
- Verification
- Deduplication
- Daily scheduling logic
- History discovery
- Rich HTML formatting
- Image generation
- Telegram publishing
- State management
- Self-test

---

## `news_state.json`

Persistent newsroom state.

It allows the bot to remember information between GitHub Actions runs, including previously processed content and knowledge records.

This prevents every scheduled run from behaving like a completely new bot.

---

## `posted_urls.txt`

A lightweight record of URLs that have already been published.

It provides an additional protection layer against repeated publication.

---

## `requirements.txt`

Python dependencies required by `main.py`.

---

## `.github/workflows/newbot.yml`

The main production workflow.

It:

1. Checks out the repository
2. Installs Python 3.12
3. Installs dependencies
4. Verifies the expected repository structure
5. Runs `py_compile`
6. Runs the built-in self-test
7. Runs the newsroom
8. Saves updated state
9. Commits and pushes state changes

The workflow is scheduled every 3 hours and can also be started manually.

---

## `.github/workflows/import-zip.yml`

A repository management workflow used to import a project ZIP into the repository.

It supports:

- `import`
- `clean`

The workflow can detect a ZIP in the repository root, extract it, import its contents, remove the ZIP, and commit the resulting project.

---

# 10. Rich Telegram Publishing

The project is not designed to publish plain text only.

It generates a visual and a structured rich message.

The rich message can contain:

- Section label
- Headline
- Main story
- Key points
- Why it is interesting
- Source links
- Hashtags / tags
- HTML formatting
- Attached image

The renderer uses Telegram-compatible rich HTML structures such as:

```html
<b>Headline</b>
```

and source links such as:

```html
<a href="https://example.com">Source</a>
```

The final rich content is also length-checked.

If the richer Telegram publishing path fails, the bot has a Bot API photo-caption fallback.

---

# 11. Content Formats

The newsroom supports format labels such as:

```text
DID YOU KNOW?
GAME DISCOVERY
RULE CHECK
HOW TO PLAY
GAME / SPORTS HISTORY
ON THIS DATE
100 YEARS AGO
WHY?
FIRST · LAST · ONLY
THEN VS NOW
```

This helps prevent every post from looking like the same generic news template.

---

# 12. AI Workflow

Cerebras is used after candidate collection.

A simplified flow is:

```text
Search candidates
       ↓
Normalize
       ↓
Remove invalid / unwanted candidates
       ↓
Collect source evidence
       ↓
Send structured candidate data to Cerebras
       ↓
Classify and analyze
       ↓
Select editorial candidates
       ↓
Generate structured story
       ↓
Run grounding checks
       ↓
Render Telegram HTML
       ↓
Publish
```

The AI does not receive an unlimited blank canvas.

The application controls the candidate data, schema, categories, and editorial constraints.

---

# 13. Verification and Grounding

The project includes deterministic checks around generated stories.

Examples include:

- Required fields must exist
- Unsupported numerical claims can be rejected
- Source URLs must be retained
- Daily date anchors are checked
- Video-game contamination is rejected
- Duplicate candidates can be rejected
- Generated content must remain within Telegram length limits

The purpose is to prevent a fluent AI response from automatically becoming a published fact.

---

# 14. Deduplication

There are multiple layers of duplicate protection.

### URL normalization

Tracking parameters such as:

```text
utm_source
utm_medium
utm_campaign
gclid
fbclid
```

are removed when constructing canonical URLs.

### Text similarity

The application compares normalized text using sequence similarity and token overlap.

### Persistent URL history

Published URLs are stored in:

```text
posted_urls.txt
```

### Persistent knowledge state

Broader story/knowledge records are stored in:

```text
news_state.json
```

This allows the system to avoid treating the same underlying story as new merely because another website published a different version.

---

# 15. Time and Date Handling

The project uses:

```text
Asia/Dhaka
```

for its newsroom clock.

Daily event searches use exact calendar dates.

For example:

```text
21 September 2026
```

rather than:

```text
tomorrow
```

This matters because Telegram posts remain visible after their original publication time.

A dated archive should still make sense later.

---

# 16. GitHub Actions Automation

The main workflow runs:

```text
Every 3 hours
```

It can also be started manually through:

```text
GitHub → Actions → TheSportsNewsroom → Run workflow
```

The workflow uses:

```yaml
python-version: "3.12"
```

and installs dependencies with:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Before the production run it checks:

```bash
python -m py_compile main.py
```

and:

```bash
python main.py --self-test
```

---

# 17. GitHub Secrets Setup

Go to:

```text
GitHub Repository
→ Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

Add these secrets.

## Required

### `EXA_API_KEY`

Your Exa API key.

```text
EXA_API_KEY=your_exa_key
```

### `CEREBRAS_API_KEY`

Your Cerebras API key.

```text
CEREBRAS_API_KEY=your_cerebras_key
```

### `TELEGRAM_BOT_TOKEN`

Token created through BotFather.

```text
TELEGRAM_BOT_TOKEN=123456:ABC...
```

---

## Optional

### `CEREBRAS_MODEL`

If you want to override the default:

```text
CEREBRAS_MODEL=gpt-oss-120b
```

If this secret is not provided, the workflow uses:

```text
gpt-oss-120b
```

---

# 18. Telegram Setup

Create a Telegram bot using **BotFather**.

Then:

1. Create the bot.
2. Copy the bot token.
3. Add the bot to `@TheSportsNewsroom`.
4. Give it the permissions required to publish.
5. Add the token to GitHub Secrets as:

```text
TELEGRAM_BOT_TOKEN
```

The workflow already defaults to:

```text
@TheSportsNewsroom
```

You can override it with:

```text
TELEGRAM_CHANNEL
```

if required.

---

# 19. Local Setup

Clone the repository:

```bash
git clone <your-repository-url>
cd TheSportsNewsroom
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

### Windows

```bash
.venv\Scripts\activate
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

---

# 20. Configure Environment Variables Locally

### Linux / macOS

```bash
export EXA_API_KEY="your_exa_key"
export CEREBRAS_API_KEY="your_cerebras_key"
export TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
export TELEGRAM_CHANNEL="@TheSportsNewsroom"
```

### PowerShell

```powershell
$env:EXA_API_KEY="your_exa_key"
$env:CEREBRAS_API_KEY="your_cerebras_key"
$env:TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
$env:TELEGRAM_CHANNEL="@TheSportsNewsroom"
```

Optional:

```bash
CEREBRAS_MODEL=gpt-oss-120b
```

---

# 21. Run the Self-Test

Before a real run:

```bash
python main.py --self-test
```

A successful self-test should end with a message similar to:

```text
Self-test passed for The Sports Newsroom v2.0.0-career-structure
```

This is an offline test and does not prove that Exa, Cerebras, or Telegram credentials are working.

---

# 22. Check the Version

```bash
python main.py --version
```

---

# 23. Run the Bot

After configuring valid API credentials:

```bash
python main.py
```

The normal execution will:

```text
Discover
→ Filter
→ Extract
→ Analyze
→ Verify
→ Deduplicate
→ Select
→ Generate image
→ Publish
→ Save state
```

---

# 24. Configuration

The current production workflow defines these operational values:

| Variable | Current workflow value | Purpose |
|---|---:|---|
| `TELEGRAM_CHANNEL` | `@TheSportsNewsroom` | Destination |
| `CEREBRAS_MODEL` | `gpt-oss-120b` | AI model |
| `MAX_DISCOVERY_POSTS_PER_DAY` | `5` | Evergreen discovery post limit |
| `MAX_DISCOVERY_CANDIDATES` | `60` | Discovery candidate capacity |
| `MAX_HISTORY_CANDIDATES` | `30` | Historical candidate capacity |
| `MAX_SPORT_EVENT_CANDIDATES` | `70` | Daily event candidate capacity |
| `DISCOVERY_COOLDOWN_HOURS` | `3` | Discovery cooldown |
| `STATE_RETENTION_DAYS` | `120` | State retention window |
| `HTTP_TIMEOUT` | `20` | HTTP timeout |
| `POST_DELAY_SECONDS` | `1.0` | Delay between publications |

These can be changed in the workflow or supplied through environment variables.

---

# 25. State Persistence

The bot is designed to run repeatedly through GitHub Actions.

After execution, the workflow stages:

```text
news_state.json
posted_urls.txt
```

If there are changes, GitHub Actions commits them back to the repository.

This creates a lightweight persistent memory system without requiring PostgreSQL, Redis, or another external database.

---

# 26. Failure Philosophy

The project prefers explicit failure over silently publishing bad information.

Examples:

```text
Missing required API configuration
        ↓
Stop / report failure
```

```text
Invalid generated story
        ↓
Reject candidate
```

```text
Video-game contamination
        ↓
Reject candidate
```

```text
Duplicate story
        ↓
Reject candidate
```

```text
Rich publishing fails
        ↓
Use Telegram Bot API fallback
```

This is important for a newsroom because a missing post is generally easier to investigate than a confidently incorrect post.

---

# 27. Security

Never commit API keys directly into:

```text
main.py
```

or:

```text
news_state.json
```

Use GitHub Actions Secrets.

Do not publish:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

inside the repository.

If a token is accidentally exposed, revoke it and create a new one.

---

# 28. Operational Model

The project is intended for a GitHub Actions environment.

```text
GitHub Actions
      │
      ├── Python 3.12
      │
      ├── Exa
      │    └── Discover sources
      │
      ├── Cerebras
      │    └── Analyze / classify / generate
      │
      ├── Local state
      │    ├── news_state.json
      │    └── posted_urls.txt
      │
      └── Telegram
           └── @TheSportsNewsroom
```

No continuously running server is required for the scheduled workflow.

---

# 29. Design Philosophy

The project follows five major principles.

### 1. Discovery over repetition

Do not publish merely because something exists.

Find content worth discovering.

### 2. Evidence before confidence

AI-generated wording should be based on collected source material.

### 3. Exact dates over disposable language

A future archive reader should understand the post without knowing when it was originally published.

### 4. Variety over one-dimensional sports coverage

The newsroom can move between:

```text
Current event
→ History
→ Rules
→ Game discovery
→ Fact
→ Traditional game
→ New game
→ Historical event
→ Explanation
```

### 5. Compact deployment

The project intentionally keeps the production repository small:

```text
1 Python engine
2 state/history files
1 dependency file
2 GitHub workflows
```

---

# 30. Production Checklist

Before enabling the scheduled workflow:

- [ ] `main.py` exists
- [ ] `news_state.json` exists
- [ ] `posted_urls.txt` exists
- [ ] `requirements.txt` exists
- [ ] `newbot.yml` exists
- [ ] `import-zip.yml` exists
- [ ] `EXA_API_KEY` is configured
- [ ] `CEREBRAS_API_KEY` is configured
- [ ] `TELEGRAM_BOT_TOKEN` is configured
- [ ] Bot has permission to publish in the channel
- [ ] `python main.py --self-test` passes
- [ ] GitHub Actions workflow can start
- [ ] First production run is inspected before relying on scheduled runs

---

# 31. Important Limitations

This is an automated editorial system, not a human newsroom.

It can still encounter:

- Incorrect or incomplete source pages
- Conflicting historical records
- Missing schedules
- Changing websites
- API outages
- Search-result quality variation
- Telegram API failures
- AI interpretation errors
- Source pages that cannot be extracted cleanly

The verification layer reduces these risks but does not make them impossible.

For high-stakes factual claims, the original governing body, official rulebook, event organizer, archive, or other primary source should remain the authority.

---

# 32. Project Identity

**Name:** The Sports Newsroom

**Telegram:** `@TheSportsNewsroom`

**Runtime:** Python 3.12

**Automation:** GitHub Actions

**AI:** Cerebras

**Web discovery:** Exa

**Publishing:** Telegram Bot API / rich Telegram message path

**Image generation:** Pillow

**Timezone:** Asia/Dhaka

**Architecture:** Single-file Python newsroom engine

---

## The Core Idea

The Sports Newsroom is built around a simple principle:

> **Sports news should not only tell people what happened. It should also help them discover what is interesting about sports and games.**

That means the newsroom can move beyond:

```text
Who won?
What was the score?
Who transferred?
```

and explore:

```text
Why is this rule like that?
Where did this game come from?
What happened on this date?
What is the official rule?
What game has almost disappeared?
Why does this sport use this measurement?
What was the first or only time something happened?
What changed over time?
What new game is worth discovering?
```

That combination of **current events + evergreen knowledge + physical games + history + rules + discovery** is what defines The Sports Newsroom.
