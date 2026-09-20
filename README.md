# Sports & Games Discovery Bot

A Telegram bot for discovering and publishing **evergreen, source-verified sports and physical-game content**.

This is intentionally **not** a video-game or esports bot. It covers real-world sports, board games, card games, party games, traditional games, mind games, tabletop games, new physical games, rules, history, origins, unusual facts and dated sports events.

## Product idea

The channel is designed as a **living sports-and-games archive**, not a disposable news feed.

The two daily sports anchors are:

```text
NEXT UP · [EXACT DATE]
What notable sporting events are scheduled for the next calendar day.

THE DAY IN SPORTS · [EXACT DATE]
What notable sporting events/results defined the previous calendar day.
```

Other posts are selected dynamically from the knowledge engine:

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
ON THIS DATE
100 YEARS AGO
THEN → NOW
WHY?
FIRST / LAST / ONLY
FORGOTTEN GAME
FORGOTTEN SPORT
GAME ORIGIN
SPORT ORIGIN
STRANGE RULE
GAME MECHANIC
```

A post is designed to remain understandable years later. Exact calendar dates are preferred over disposable wording such as `today`, `yesterday`, or `tomorrow`.

## Explicit exclusions

```text
PlayStation
Xbox
PC gaming
Mobile gaming
Steam
Video games
Esports
DLC
Patch notes
Game trailers
Gaming hardware
Video-game studios/publishers
```

The exclusion is enforced both during discovery and after AI classification/generation.

## Architecture

```text
Internet
  ↓
RSS + Exa discovery
  ↓
Candidate normalization
  ↓
Video-game contamination filter
  ↓
AI classification
  ↓
Article/source extraction
  ↓
Claim-level verification
  ↓
Conflict + source-quality check
  ↓
Underlying-claim novelty check
  ↓
Interest + evergreen + diversity scoring
  ↓
AI editorial selection
  ↓
Format-specific generation
  ↓
Deterministic validation / grounding
  ↓
Original visual card
  ↓
Telegram Rich Message
  ↓
Persistent knowledge/archive state
```

The AI proposes. Python enforces the hard rules.

## Knowledge model

The persistent state stores more than URLs. It keeps:

```text
claims
entities
posts
historical records
category history
angle history
source health
queue
```

That allows the bot to understand that:

```text
"Why does tennis use love?"
```

and

```text
"Where did the tennis term love come from?"
```

can be the same underlying claim even though the headlines differ.

This is the main anti-repeat mechanism for evergreen content.

## Verification policy

Source quality is layered:

1. **Primary / official**: federations, tournament organizers, official rulebooks, museums, archives, original records.
2. **High-quality reference**: Britannica, Guinness World Records, established specialist/reference publications.
3. **Discovery sources**: Wikipedia, BoardGameGeek, Atlas Obscura and specialist publications.
4. **Lead only**: Reddit, Quora, forums and random blogs.

Rules:

- One strong primary source can verify a claim.
- Without a primary source, the bot requires at least two independent credible sources.
- Conflicting evidence becomes `disputed`.
- Missing evidence becomes `unverified` and is not published.
- Dates, numbers, names, origins, rules and records must be supported by source evidence.
- The writer is forbidden from filling missing details from memory.

## Editorial scoring

Evergreen candidates are assessed across:

```text
Surprise
Curiosity
Usefulness
Evergreen value
Novelty
Verifiability
Shareability
```

Hard gates override the score:

```text
Weak verification  → reject
Weak evergreen value → reject
Duplicate underlying claim → reject
Repeated subject/angle → penalize or reject
Video-game contamination → reject
```

There is a daily ceiling for safety, but no requirement to manufacture posts when the candidate pool is weak.

## Content diversity

The scheduler rotates:

- categories
- angles
- games/sports
- geographic coverage
- current vs historical content

A single game can have many legitimate knowledge angles, but the same angle is not repeatedly posted just because a new article exists.

## Current technical stack

The project intentionally reuses the previous project's core engineering foundation:

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

Telegram Rich Messages are used when available, with a standard `sendPhoto` fallback. The implementation follows the current Bot API Rich Message `html` + `media` model. See: https://core.telegram.org/bots/api

## Repository

```text
SportsGamesDiscoveryBot/
├── .github/
│   └── workflows/
│       ├── newbot.yml
│       └── import-zip.yml
├── README.md
├── main.py
├── knowledge_state.json
├── published_urls.txt
└── requirements.txt
```

The production components remain consolidated in `main.py` so the repository stays easy to deploy with GitHub Actions.

## GitHub secrets

Required:

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional:

```text
TELEGRAM_CHANNEL=@SportsGamesHub
TELEGRAM_ADMIN_CHAT_ID=...
CEREBRAS_MODEL=gpt-oss-120b
MAX_DISCOVERY_POSTS_PER_DAY=5
MAX_DISCOVERY_CANDIDATES=60
POST_DELAY_SECONDS=3
DISCOVERY_COOLDOWN_HOURS=3.5
```

## Workflow schedule

The included workflow runs five times per day in `Asia/Dhaka` and can also be started manually.

The runtime is idempotent. A dated sports anchor is only published once for its target date, and evergreen posts use persistent claim/angle memory to avoid repeating old content.

## Self-test

The workflow runs:

```bash
python -m py_compile main.py
python main.py --self-test
python main.py
```

The self-test does not call external APIs or Telegram. It checks canonicalization, video-game rejection, claim deduplication, verification gates, message generation, date anchoring, rendering and state retention.

## Deployment notes

1. Create a GitHub repository.
2. Upload the repository files.
3. Add the three required GitHub Actions secrets.
4. Change `TELEGRAM_CHANNEL` in the workflow to the real channel if necessary.
5. Run the workflow manually once.
6. Review the first few posts and source quality before leaving the scheduled workflow active.

## Editorial goal

The target feeling is:

> Every time the user opens the channel, there should be a realistic chance of learning something they did not know about sports or physical games.

That can be a current sporting event, a new card game, an obscure traditional game, a surprising rule, a verified fact, a historical event from 100 years ago, an origin story, a game explanation or a piece of sports history.
