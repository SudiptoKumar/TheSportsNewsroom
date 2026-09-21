# The Sports Newsroom v3

An automated Telegram newsroom for **@TheSportsNewsroom**. It runs on GitHub Actions and publishes verified sports and tabletop-game content: daily schedules and results, "on this date" history, and evergreen facts, rules and game discoveries.

**Scope:** real-world sports and physical/tabletop games only (cricket, football, chess, Ludo, Carrom, card games, board games, ...). No video games, esports, betting, odds, transfer rumours, or live-score spam.

---

## What it posts (Asia/Dhaka time, GMT+6)

| Desk | Post | When it opens | Source of truth |
|---|---|---|---|
| DAY IN SPORTS | Previous day's results, records, storyline | 07:00 | ESPN scoreboards, Wikipedia Current Events, RSS |
| ON THIS DATE | Sports/games history for today's date (or "100 YEARS AGO") | 09:00 | Wikipedia On-This-Day + "YYYY in sports" |
| EVERGREEN x3 | Facts, rules, how-to-play, origins, forgotten games | 10:00 / 15:00 / 20:00 | Wikipedia articles (+ official rules pages via Exa) |
| NEW ON THE TABLE | New board/card games (replaces one evergreen slot every ~3 days) | middle slot | Tabletop outlets via Exa |
| NEXT UP | Notable events on tomorrow's exact date | 19:00 | ESPN scoreboards, TheSportsDB, cricket chain |

About 6 posts per day. Daily digests always use **exact calendar dates** ("NEXT UP · 22 SEPTEMBER 2026"), never "tomorrow".

---

## How it works

```
GitHub cron (hourly, :17)
  -> scheduler: which slots are due and not yet done?   (catches up after skipped/delayed runs)
  -> desk (isolated; one failure never stops the others)
  -> data adapters (ESPN, TheSportsDB, Wikipedia, RSS, Exa)
  -> AI writes/selects from evidence only  ->  code verifies  ->  Telegram (photo card or text)
  -> ledger + dedupe state committed back to the repo  ->  run report in the Actions summary
```

**Design rules**

1. **Facts come from data, not from AI.** Fixtures and results come from APIs; history from Wikipedia. The AI only selects and phrases.
2. **Quote-then-write.** For knowledge posts the AI must return a *verbatim quote* from the source; code checks the quote exists before anything is written.
3. **Code verifies every post.** Numbers, scores, names, and words like *first / only / never / oldest / record* must appear in the evidence, or the post is sent back for one rewrite and then dropped. Comma-formatted numbers (`1,500`) are handled. Copying 14+ source words triggers a paraphrase request.
4. **Degrade, don't reject.** Formatting problems (too many sentences, missing full stop, ellipsis) are fixed in code. Digests have a no-AI version, so they still post if the AI is down.
5. **Rule posts need strong evidence** (an official/rules-publisher page, or two independent sources); otherwise that slot switches to a lower-risk format.
6. **Never silent.** Every source call is health-tracked; every run writes a report; overdue mandatory posts turn the workflow red and (optionally) DM you.
7. **No duplicates.** URL, claim, headline and topic dedupe; a delivery that may have half-succeeded is marked *uncertain* and is never re-sent.

**Data sources and fallbacks**

| Need | Primary | Fallbacks |
|---|---|---|
| Fixtures/results | ESPN public scoreboards (51 leagues) | TheSportsDB -> Exa + AI extraction (event date is verified in the source text by code) |
| Cricket | CricketData.org (optional key) | Exa restricted to ESPNcricinfo/Cricbuzz/ICC + date check |
| History | Wikipedia On-This-Day feed | "September 21" day page, "YYYY in sports" pages |
| Knowledge | Wikipedia article text | Exa for official rules and new-game news |
| Storylines | Wikipedia Current Events | BBC / ESPN / Guardian / Sky RSS |

---

## Setup

**Secrets** (Settings > Secrets and variables > Actions > Secrets):

| Secret | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot must be an **admin with "Post messages"** in the channel |
| `CEREBRAS_API_KEY` | yes | Writing/selection (`gpt-oss-120b`; auto-switches model if unavailable) |
| `EXA_API_KEY` | recommended | New-game news, rules corroboration, cricket fallback |
| `TELEGRAM_ADMIN_CHAT_ID` | optional | Your chat id; receives one short alert per problem per day |
| `CRICKETDATA_API_KEY` | optional | Free key from cricketdata.org for cricket fixtures/results |
| `CEREBRAS_MODEL` | optional | Override the model name |

**Variable** (optional): `TELEGRAM_CHANNEL`. Set it to a private test channel to trial the bot without touching the real channel; delete it to go live.

### Launch checklist

1. Import this ZIP with **Actions > Import Project ZIP > import**.
2. **Actions > TheSportsNewsroom > Run workflow > mode = `diagnose`.** Open the run summary. Rows marked FAIL must be fixed; WARN rows are optional sources/fallbacks.
3. Run **`dry-run`**: the log shows every slot's post exactly as it would appear.
4. Run **`test-post`** with `channel_override` set to your private test channel. Check the image card and formatting on your phone.
5. Set repository variable `TELEGRAM_CHANNEL` to the test channel and let the schedule run for 1-2 days. Check the run summary each day.
6. Delete the variable to go live.

### Manual modes

| Mode | Posts? | Saves state? | Use |
|---|---|---|---|
| `run` | yes (what is due) | yes | normal tick (same as the schedule) |
| `diagnose` | no | no | live health check of every dependency |
| `dry-run` | no | no | preview every slot, ignoring the clock |
| `test-post` | 2 samples | no | check delivery and formatting |
| `self-test` | no | no | full offline test suite |

Local: `python main.py --self-test`, `--demo` (simulate a day on a fake network and print every post), `--dry-run`, `--diagnose`, `--only next_up`, `--channel @Name`.

---

## Reading a run report

Each run writes a table to the Actions summary: per-slot result, per-source ok/fail counts with the last error, guard rejections, and AI usage.

| Symptom | Meaning / fix |
|---|---|
| `espn: 0/51 leagues reachable` | ESPN's unofficial endpoint is blocked/changed. TheSportsDB and Exa fallbacks take over automatically. |
| `cerebras auth failed` | Bad or expired `CEREBRAS_API_KEY`. Digests still post in no-AI mode. |
| `AI circuit opened` | 3 consecutive AI failures; AI is paused for that run only. |
| `wikipedia page unavailable` | Network problem or bad title; the topic is retried later, then retired after 3 failures. |
| `rule_evidence_too_weak` (rejection) | No official/second source found for a rules post; the slot posted a safer format instead. |
| `uncertain` slot | Network error after Telegram may have received the post. Check the channel; the bot will not re-send. |
| Workflow red (exit 2) | A mandatory post (DAY IN SPORTS / NEXT UP) is overdue; details are in the summary and the admin alert. |

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `POSTS_EVERGREEN_PER_DAY` | 3 | Evergreen slots per day |
| `NEXT_UP_OFFSET_DAYS` | 1 | 1 = post tomorrow's schedule; 0 = today's |
| `DAY_IN_SPORTS_OPEN_HOUR` / `ON_THIS_DATE_OPEN_HOUR` / `NEXT_UP_OPEN_HOUR` | 7 / 9 / 19 | Dhaka hour a slot opens |
| `RUN_DEADLINE_SECONDS` | 720 | Internal time budget per run (below the 20-minute job timeout) |
| `MAX_ATTEMPTS_PER_SLOT` | 6 | Retries per slot per day |
| `HTTP_TIMEOUT` | 15 | Seconds per request |

---

## Testing and honest limits

`python main.py --self-test` runs 100+ offline checks against a **fake network** that mimics Telegram, Cerebras, Exa, ESPN, Wikipedia, TheSportsDB and RSS: guards, parsers, HTML fuzzing (Telegram-safe output), the scheduler, one full simulated day, fault injection (each dependency down; Telegram HTML rejection, 429, photo failure, read timeout), corrupt state, dry-run isolation, and a **30-day soak** with hourly cron, 20% of runs skipped, and rotating outages (asserts every daily digest posts exactly once and nothing is ever duplicated).

What the fake network **cannot** prove is that third-party services still behave exactly as documented. That is what `diagnose` is for, and every adapter is a tolerant reader with a fallback:

- **ESPN's scoreboard API is unofficial** (no SLA). It is used for public facts only, attributed, and replaceable.
- **Cricket coverage is the weakest link**; the free CricketData.org key improves it.
- The Wikipedia Current Events parser depends on page layout; if it finds nothing, storylines fall back to RSS.
- AI-written text is verified against sources by code but is not human-proofread. Skim the channel occasionally.
- Wikipedia text is CC BY-SA: posts paraphrase and always link the source article.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entire engine (sections: core, http, state+scheduler, telegram, cards, AI, adapters, guards, desks, runner, diagnose, self-test, CLI) |
| `news_state.json` | Ledger, post archive, topic coverage, source health (schema v3) |
| `posted_urls.txt` | Article URLs already used (dedupe) |
| `requirements.txt` | `requests`, `Pillow` |
| `.github/workflows/newbot.yml` | Hourly schedule + manual modes |
| `.github/workflows/import-zip.yml` | ZIP import helper |

## v2 -> v3

v2 chained seven AI-judged gates, used a web-search engine for fixtures, swallowed every network error, and shipped dead code (`rss_discovery`, `search_history`, URL dedupe) while its self-test only covered helper functions. v3 replaces the engine while keeping the channel, formats and content rules.
