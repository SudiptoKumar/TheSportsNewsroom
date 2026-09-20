# The Sports Newsroom Discovery Engine

Production-oriented Telegram bot for **[@TheSportsNewsroom](https://t.me/TheSportsNewsroom)**.

Version **2.1.0** keeps the previous project's Python/Exa/Cerebras/Telegram/GitHub Actions foundation while replacing the content system with a broader **Sports + Real-World Games Discovery Engine**.

## Product rule

> Find something people would want to know, play, understand, remember, or share. Verify it before publishing.

The system covers:

- sports and sporting events
- board games
- card games
- tabletop games
- party games
- traditional/regional games
- mind/puzzle games
- newly discovered physical games
- rules and rule clarifications
- how-to-play guides
- game/sport history and origins
- unusual and forgotten sports/games
- facts, first/last/only records and date-anchored history

It explicitly excludes video games, esports, consoles, Steam, DLC, patches, gaming hardware and video-game industry news.

## Daily backbone

Two independent anchor jobs create a permanent date-based archive:

1. **NEXT UP · YYYY-MM-DD**
2. **THE DAY IN SPORTS · YYYY-MM-DD**

The posts are reference-oriented and use exact dates. The scheduler never depends on a source being published on the same day as the sporting event. Future schedules may have been published weeks earlier; past results may be reported shortly after the event date.

## Discovery engines

- Game discovery
- New board/card/tabletop game discovery
- Evergreen facts
- Rules and official-vs-house-rule checks
- How to play
- Game/sport history
- On this date
- 25/50/75/100/125-year historical discovery
- First / Last / Only
- Why?
- Then → Now
- Forgotten games/sports
- New physical sports
- Interesting numbers and terminology

Search is driven by surprise patterns, not only topic names.

## Verification architecture

The bot separates discovery from truth.

```text
Search
  ↓
Candidate
  ↓
Cheap Python filtering
  ↓
Cerebras classification
  ↓
Preliminary ranking
  ↓
Top-candidate verification
  ↓
Evidence packet
  ↓
Cerebras factual verification
  ↓
Novelty / archive check
  ↓
Cerebras writing
  ↓
Deterministic grounding
  ↓
Repair loop if needed
  ↓
Final factual grounding
  ↓
Visual
  ↓
Telegram
```

### Source handling

Direct HTTP extraction is best-effort, not mandatory. A 403 or timeout no longer destroys the candidate. Exa highlights are retained as fallback evidence, and corroborating sources can be discovered when needed.

Known source hierarchy:

- Tier 1: official/primary
- Tier 2: reputable secondary
- Tier 3: reference sources
- Tier 4: lead-only sources
- Tier 5: unknown sources

A primary source may verify a claim alone when it directly supports it. Otherwise the verification stage requires two independent credible domains.

## AI budget

The pipeline intentionally delays expensive AI work.

```text
Raw search results
→ Python filters
→ one batch classifier
→ cheap preliminary ranking
→ only top candidates verified
→ deterministic editorial selection
→ AI writing only for selected posts
```

This reduces Cerebras calls and makes rate-limit bursts less likely.

A per-run AI budget and minimum interval are configurable through environment variables.

## Draft repair

Invalid drafts are repaired instead of being discarded immediately:

```text
Draft
 ↓
Validator
 ↓
Failure reason
 ↓
Evidence-constrained repair
 ↓
Validator
 ↓
Final grounding
```

## Telegram publishing

The bot uses a reliable two-step media flow:

1. Upload the generated visual with `sendPhoto`.
2. Upgrade its caption to Rich Message HTML with `editMessageCaption`.

If the rich upgrade fails, the already-published photo retains a plain Telegram-compatible fallback caption.

## Repository

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
│       └── published_urls.txt
├── src/sportsgames/
│   ├── config.py
│   ├── content.py
│   ├── discovery.py
│   ├── editorial.py
│   ├── media.py
│   ├── pipeline.py
│   ├── providers.py
│   ├── schemas.py
│   ├── state.py
│   ├── taxonomy.py
│   ├── telegram.py
│   ├── utils.py
│   └── verification.py
├── tests/
│   └── test_core.py
├── main.py
├── requirements.txt
├── .gitignore
└── README.md
```

## Required GitHub Secrets

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

The workflow targets `@TheSportsNewsroom` by default.

## Useful configuration

```text
CEREBRAS_MODEL=gpt-oss-120b
AI_MIN_INTERVAL_SECONDS=1.75
AI_MAX_CALLS_PER_RUN=18
HTTP_CONNECT_TIMEOUT=6
HTTP_READ_TIMEOUT=12
MAX_VERIFICATION_CANDIDATES=10
MAX_DISCOVERY_POSTS_PER_DAY=4
MAX_REPAIR_ATTEMPTS=2
```

## Local checks

```bash
python -m py_compile main.py src/sportsgames/*.py
python -m unittest discover -s tests -v
python main.py --self-test
```

Live production execution requires the three GitHub/API secrets.
