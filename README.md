# The Sports Newsroom Discovery Engine

Production-oriented Telegram bot for **[@TheSportsNewsroom](https://t.me/TheSportsNewsroom)**.

This project reuses the previous bot's proven engineering foundation, especially Python 3.12, Exa, Cerebras, requests/BeautifulSoup/trafilatura/Pillow/feedparser, GitHub Actions, persistent state and Telegram Bot API publishing. The content architecture is redesigned for a much broader product:

> **Sports + real-world games discovery, knowledge and history.**

It is not a conventional sports-news scraper and it does not cover video games or esports.

## Product vision

The channel is built as a **living sports-and-games archive**.

Every published item should answer at least one of these questions:

- What important sporting event is happening on a specific date?
- What happened on a specific sports date?
- What game is this?
- How do you play it?
- What is the official rule?
- Why does this strange rule exist?
- Where did the game or sport come from?
- What surprising fact is actually true?
- What happened 25, 50, 75 or 100 years ago?
- What new physical board/card/tabletop game is worth discovering?
- What unusual or forgotten game/sport exists?

The system deliberately favors **interesting + verified + evergreen** over high post volume.

## Channel

```text
https://t.me/TheSportsNewsroom
@TheSportsNewsroom
```

## Explicit exclusion

This project does not cover:

```text
PlayStation
Xbox
Nintendo video games
PC gaming
Mobile gaming
Steam
Video games
Esports
Gaming hardware
DLC
Patch notes
Video-game trailers
Video-game studios/publishers
```

The exclusion is enforced at multiple stages, including query/candidate screening and final-story validation.

## Daily editorial backbone

The system has two permanent sports anchor posts.

### NEXT UP · exact date

A reference-oriented preview for the next calendar day.

It combines:

- major matches/events
- finals and championship events
- notable races
- important tournament stages
- significant events across many sports
- compact event listings
- why the selected event is worth following
- exact date and UTC time when supported

### THE DAY IN SPORTS · exact date

A historical-reference style recap for the previous calendar day.

It combines:

- major results
- titles/championships decided
- records
- milestones
- notable upsets/events
- a compact cross-sport index

Posts use exact dates instead of disposable language such as `today`, `yesterday`, `tomorrow`, `tonight` or `latest`.

## Evergreen discovery system

Discovery candidates are classified into content families such as:

```text
GAME DISCOVERY
NEW BOARD GAME
NEW CARD GAME
NEW TABLETOP GAME
NEW SPORT
DID YOU KNOW?
RULE CHECK
OFFICIAL RULE vs HOUSE RULE
HOW TO PLAY
GAME HISTORY
SPORT HISTORY
ON THIS DATE
100 YEARS AGO
THEN → NOW
WHY?
FIRST / LAST / ONLY
FORGOTTEN GAME
FORGOTTEN SPORT
SPORT DISCOVERY
MYTH VS FACT
GAME ANATOMY
THE STORY BEHIND THE NUMBER
GAME ORIGIN
SPORT ORIGIN
```

### Surprise-driven searching

The query engine intentionally searches for surprise patterns instead of only searching by sport/game name:

```text
why is it called
origin of
oldest known
first ever
only time ever
never been broken
invented by accident
originally called
originally meant
rule most people get wrong
official rule
house rule
myth about
banned in
strange rule
unusual tradition
forgotten history
why does it use
where did it come from
what changed
then vs now
first to
only to
what does the number mean
```

This allows the system to discover facts and stories that do not look like normal sports news.

## History engine

Historical discovery is date-driven.

For the current calendar date, the system searches historical events around:

```text
25 years ago
50 years ago
75 years ago
100 years ago
125 years ago
```

It can also discover:

- first/last/only events
- old rules
- forgotten sports
- game origins
- rule evolution
- historic milestones
- equipment evolution
- famous and obscure historical events

The important design principle is **date-first permanence**. A post should still make sense months or years later.

## Game universe

The bot covers real-world games beyond professional sport:

```text
Sports
Board games
Card games
Tabletop games
Party games
Traditional games
Mind games
Recreational games
Regional games
Historical games
New physical games
```

Examples include Ludo, UNO, Chess, Carrom, Monopoly, Catan, Go, Shogi, Mahjong, Codenames, Kabaddi, Kho Kho, Sepak Takraw and many other games/sports. The taxonomy is intentionally expandable.

## Knowledge-object architecture

The project does not treat an article as the permanent unit of knowledge.

```text
SOURCE
  ↓
CLAIM / EVENT / GAME
  ↓
KNOWLEDGE OBJECT
  ↓
CONTENT ANGLE
  ↓
POST
```

Example:

```text
UNO
├── history
├── origin
├── official rules
├── house rules
├── misconceptions
├── interesting facts
├── editions
├── terminology
└── strategy
```

This allows multiple valid posts about a game without repeatedly publishing the same underlying fact.

## Claim-level verification

The AI does not get to decide that a fact is true simply because it sounds plausible.

Verification pipeline:

```text
candidate
  ↓
claim extraction
  ↓
source evidence retrieval
  ↓
primary-source check
  ↓
independent corroboration
  ↓
conflict detection
  ↓
verification status
  ↓
editorial selection
  ↓
story grounding
```

A known primary/official source can establish a claim if the source directly supports it. Without primary evidence, the default requirement is at least two independent credible domains.

Lead-only sources such as Reddit, Quora, social posts, forums and random blogs cannot verify a fact by themselves.

## Source hierarchy

```text
Tier 1
Official federations
Official competition/rulebook pages
Museums / archives
Original records/documents

Tier 2
Reuters
AP
BBC Sport
ESPN
Sky Sports
The Guardian
Specialist reputable publications

Tier 3
Britannica
Guinness World Records
Wikipedia
Atlas Obscura
BoardGameGeek and specialist discovery sources

Tier 4
Reddit
Quora
Forums
Social media
Random blogs
```

Unknown domains are treated conservatively as reference-level rather than trusted primary evidence.

## Editorial scoring

Candidates are scored across:

```text
Surprise        0–5
Verification    0–5
Evergreen value 0–5
Novelty         0–5
Clarity         0–5
Curiosity       0–5
Shareability    0–5
```

Maximum = 35.

Hard gates apply before publishing:

```text
insufficient verification → reject
weak evergreen value      → reject
duplicate underlying fact → reject
video-game contamination  → reject
unsupported factual text  → reject
```

The system does not manufacture posts to satisfy a quota.

## Novelty model

The archive remembers **claims**, not only URLs.

For example:

```text
Why does tennis use “love”?

Where did the tennis term “love” come from?
```

can represent the same underlying factual claim and will normally be treated as a repeat.

Novelty checks consider:

- normalized claim
- subject
- angle
- semantic similarity
- recent subject history
- recent category history
- recent angle history

This prevents a large source ecosystem from producing repeated versions of the same fact.

## Editorial diversity

The scheduler deliberately rotates:

- content category
- content angle
- sport/game entity
- geography
- current vs historical content

It is possible for one game to have many posts over time, but the same subject is not allowed to dominate the feed simply because it generates many search results.

## Multi-stage AI responsibilities

### Discovery classifier
Identifies what a candidate can actually support.

### Verification editor
Checks factual claims against source evidence.

### Editorial selector
Chooses candidates that are worth publishing and diverse.

### Story editor
Writes the post from supplied evidence only.

### Final grounding checker
Checks the generated story again against source evidence.

Python code enforces deterministic constraints around the AI.

## Deterministic safeguards

```text
video-game blacklist
URL canonicalization
claim fingerprinting
source-domain counting
date validation
numeric grounding
post-length validation
HTML validation
daily publish flags
state persistence
retry/backoff
```

The principle is:

> **AI proposes. Python enforces.**

## Repository

```text
SportsGamesDiscoveryBot/
│
├── .github/
│   └── workflows/
│       ├── newbot.yml
│       └── import-zip.yml
│
├── data/
│   ├── taxonomy.json
│   ├── sources.json
│   └── state/
│       ├── knowledge_state.json
│       └── published_urls.txt
│
├── src/
│   └── sportsgames/
│       ├── __init__.py
│       ├── config.py
│       ├── taxonomy.py
│       ├── schemas.py
│       ├── utils.py
│       ├── state.py
│       ├── providers.py
│       ├── discovery.py
│       ├── verification.py
│       ├── editorial.py
│       ├── content.py
│       ├── media.py
│       ├── telegram.py
│       └── pipeline.py
│
├── tests/
│   └── test_core.py
│
├── main.py
├── requirements.txt
└── README.md
```

## Why this structure

The previous bot was intentionally consolidated in one file for easy deployment. That approach becomes difficult to maintain once the content universe expands.

This project keeps the same lightweight GitHub Actions deployment model, but separates the important domains:

```text
config.py        environment + limits
 taxonomy.py      sports/games/source taxonomy
 schemas.py       Cerebras JSON contracts
 utils.py         deterministic helpers
 state.py         knowledge/archive persistence
 providers.py     Exa, Cerebras, HTTP, RSS, Telegram
 discovery.py     web search/query strategies
 verification.py  claim verification + final grounding
 editorial.py     classification + scoring + diversity
 content.py       story and daily sports generation
 media.py         original Pillow visual cards
 telegram.py      Rich Message + sendPhoto fallback
 pipeline.py      end-to-end orchestration
```

This keeps each part replaceable without turning the repository into a large framework.

## Technical stack

```text
Python 3.12
Exa
Cerebras
requests
urllib3
beautifulsoup4
Pillow
feedparser
trafilatura
GitHub Actions
Telegram Bot API
```

Telegram Rich Messages are preferred where available, with a `sendPhoto` fallback. Telegram's current Bot API supports Rich Messages, structured HTML/Markdown and media blocks. See the official Bot API documentation: https://core.telegram.org/bots/api

## GitHub Secrets

Required:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

The workflow sets:

```text
TELEGRAM_CHANNEL=@TheSportsNewsroom
CEREBRAS_MODEL=gpt-oss-120b
```

## Local validation

Install:

```bash
pip install -r requirements.txt
```

Compile:

```bash
python -m py_compile main.py src/sportsgames/*.py
```

Run tests:

```bash
python -m unittest discover -s tests -v
```

Run offline self-test:

```bash
python main.py --self-test
```

Show version:

```bash
python main.py --version
```

Run production pipeline:

```bash
python main.py
```

## GitHub Actions schedule

The production workflow runs six times per day using the `Asia/Dhaka` timezone. The run timing provides several opportunities to recover from temporary source/API failures while daily flags prevent duplicate anchor posts.

The bot can publish the next-day preview after the configured morning cutoff and the previous-day recap after the configured evening cutoff.

## Important operational note

The system intentionally uses **broad discovery**, not a claim that the internet can be exhaustively crawled in a single run. `NEXT UP` and `THE DAY IN SPORTS` are generated from a broad tracked-sport search set and verified source candidates. The sport taxonomy is designed to grow over time.

## Production philosophy

The quality hierarchy is:

```text
Truth
↓
Evidence
↓
Novelty
↓
Interest
↓
Editorial presentation
↓
Volume
```

The project should prefer publishing fewer excellent discoveries over filling the channel with repetitive or weakly supported material.
