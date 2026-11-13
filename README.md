# flatwatch

Watches Facebook groups and subreddits for flat and flatmate listings, keeps the ones that match your criteria, and sends them to you on Telegram within about 15 minutes of being posted.

Built for Bangalore. The areas, budget and flat type are all set in a config file.

```
launchd ─► headless Chrome (your logged-in profile)
              ├─ Facebook groups: reads the feed's GraphQL responses
              └─ subreddits: reads the page
         ─► skip seen posts and cross-posts ─► keyword prefilter
         ─► Claude Haiku extracts rent / area / unit / gender / bathroom
         ─► rule matcher ─► instant alert │ twice-daily digest │ dropped
```

- **Instant alerts** go out for clear matches: a whole 1BHK or studio in your areas within budget (P1), or a room in a shared flat with a private bathroom (P2).
- **Digest** at 09:00 and 21:00 lists near-misses, such as up to 15% over budget, an unknown unit, or an unknown gender preference.
- **Read-only:** flatwatch only opens feed pages and scrolls. It never posts, comments, reacts, messages, joins groups or types a password. You log in by hand once.
- **Polite polling:** Facebook every 12–18 minutes, Reddit every 15–20 minutes, every 45 minutes overnight. Groups are visited 20–60 s apart, with at most 5 scrolls per visit. If it hits a checkpoint or block page, it pauses that source and messages you. It never retries on its own.

> Automated reading of Facebook may break Facebook's terms. Use your own account at your own risk.

## Requirements

- macOS with Google Chrome installed. Other systems can run `flatwatch run` under their own service manager.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your chat id.
- For the LLM step, either:
  - a Claude subscription with the [Claude Code](https://claude.com/claude-code) CLI logged in (`claude auth login`), which is the default; or
  - an Anthropic API key (set `extract.backend: anthropic_api`).

## Install

```bash
uv tool install git+https://github.com/shivangkhajuria/flatwatch
```

To work on the code instead, clone it and run `uv sync`, then use `uv run flatwatch ...`.

## Setup

```bash
flatwatch init                    # creates ~/.flatwatch/config.yaml and ~/.flatwatch/.env
```

1. **`~/.flatwatch/.env`:** your personal settings. This file is never committed.
   ```ini
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   FLATWATCH_FACEBOOK_GROUPS=https://www.facebook.com/groups/1234567890/,https://www.facebook.com/groups/some-slug/
   FLATWATCH_SUBREDDITS=SomeSubreddit,AnotherOne
   ```
2. **`~/.flatwatch/config.yaml`:** your criteria (areas with their spellings, tiers, budget, exclusions) and timings. See the comments in the file.
3. Run these:
   ```bash
   claude auth login                 # skip if using anthropic_api
   flatwatch ping                    # validates everything, sends "flatwatch is alive ✅"
   flatwatch login facebook reddit   # opens Chrome on flatwatch's own profile; log in by hand
   flatwatch agent install           # runs in the background and starts at login
   ```

flatwatch looks for its files in this order:
- **config.yaml:** `$FLATWATCH_CONFIG`, then `./config.yaml`, then `~/.flatwatch/config.yaml`.
- **.env:** `$FLATWATCH_ENV`, then `./.env`, then `~/.flatwatch/.env`.

Its data lives in `~/.flatwatch`; set `FLATWATCH_HOME` to move it. That covers the SQLite database, the Chrome profile and the logs.

## Commands

| Command | What it does |
|---|---|
| `flatwatch init [--dir D]` | Create config.yaml and .env from the templates |
| `flatwatch ping` | Check the config and send a Telegram test message |
| `flatwatch login facebook reddit` | Log in by hand in flatwatch's Chrome profile |
| `flatwatch run [--headed]` | Run in the foreground |
| `flatwatch agent install\|uninstall\|start\|stop\|restart\|status` | Manage the macOS LaunchAgent |
| `flatwatch fetch facebook --group 1 --dry-run` | Print the newest posts from one source (debugging) |
| `flatwatch backtest --days 2` | One-off: what would have been sent over the past N days (messages labelled BACKTEST) |
| `flatwatch resume facebook` | Clear a pause after you've checked the account |

In Telegram, the bot answers `/status`, `/pause [hours]`, `/resume [source]`, `/digest`, `/last [n]` and `/help`. Every alert has 👍/👎 buttons, and the votes are stored for tuning.

## How matching works

1. **Prefilter.** A post must mention flat keywords (BHK, flatmate, rent, …) or one of your areas, otherwise it never reaches the LLM.
2. **Extraction.** Claude Haiku turns each post into structured fields (intent, unit, area, rent, gender preference, private bathroom, broker, …), 15 posts per call. If the LLM is down, a regex fallback still catches obvious matches.
3. **Matching.** A pure function applies `config.yaml`:
   - Exclusions come first (people seeking a flat, PG / co-living, 1RK, female-only).
   - Then area, flat shape and rent.
   - A missing rent is dropped or sent to the digest, depending on `rent_unknown`.
4. **Dedupe.** Fuzzy text matching plus phone and area folds cross-posts together. The original alert gets an "also in …" note.

## Development

```bash
uv sync
uv run pytest
```

Saved fixtures (`fixtures/`), hand-labelled evals (`evals/labeled.jsonl`) and run logs are gitignored, because they contain real people's posts.
