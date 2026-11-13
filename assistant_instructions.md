# Instructions for an AI assistant setting up flatwatch

You are helping someone set up **flatwatch** on their Mac. flatwatch watches Facebook groups and subreddits for flat and flatmate listings, and sends the ones that match their criteria to Telegram. Read `README.md` first for the overview, then follow this file step by step.

Work with the user, not around them. Several steps need their own hands: logins, the Telegram bot, and their criteria. Never guess those.

## Hard rules

1. **Secrets stay in `.env`.** The Telegram token, chat id and any API key go only in the user's `.env`, which is never committed. Never print the token in full. If you must refer to it, mask it, for example `1234…xy`. Don't put it in commands others will see, logs, commit messages or URLs you share.
2. **Personal settings stay out of git.** That means their groups, subreddits and `config.yaml`. Before any commit, run `git status` and check that `.env`, `config.yaml`, `fixtures/` and `evals/labeled.jsonl` are not staged.
3. **flatwatch is read-only on Facebook and Reddit.** Never make it post, comment, react, message, join or follow anything, and never click on post content. Don't add code that does.
4. **Never log in for the user or handle their passwords.** `flatwatch login` opens Chrome, and the user logs in there themselves. Don't type into login forms, don't solve captchas, and don't ask for passwords.
5. **Don't make it more aggressive.** Keep the polling intervals as they are: Facebook 12–18 min, Reddit 15–20 min, 45 min overnight. Keep the 20–60 s gap between groups and the cap of 5 scrolls per visit. Changing any of these risks the user's account, so ask them first and explain the risk.
6. **If a site shows a checkpoint, login wall or captcha, stop.** flatwatch pauses that source by itself. Tell the user. Let them check the account in the bot's Chrome window, then they send `/resume <source>`. Never loop retries.
7. **Ask about criteria; don't assume them.** Areas, budget and flat type are the user's decisions (step 4).
8. **Keep live fetching light while testing.** Use `--dry-run` on 1–3 groups at a time; the tool caps dev runs at 3 groups per run and 9 visits per hour. Don't run the backtest without the user's OK, because it scrolls much deeper than normal.

## Setup steps

Run each step's check before moving on. If a check fails, fix it first.

### 1. Prerequisites

- macOS with Google Chrome in `/Applications`.
- Python 3.12+ and `uv`. If `uv` is missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`. Ask before installing anything.
- The `claude` CLI, logged in to the user's Claude subscription. That is the default LLM backend.

**Check:** `uv --version`, `claude --version` and `ls "/Applications/Google Chrome.app"` all succeed.

### 2. Install

```bash
uv tool install git+https://github.com/shivangkhajuria/flatwatch
```

To work on the code instead, clone it, run `uv sync`, and prefix every command below with `uv run`.

**Check:** `flatwatch --help` lists `init`, `ping`, `login`, `run` and `agent`.

### 3. Create the config files

```bash
flatwatch init
```

This creates `~/.flatwatch/config.yaml` and `~/.flatwatch/.env` (mode 600). Both are outside any repo.

### 4. Ask the user for their criteria, then edit `config.yaml`

Ask these questions, and write their answers into `criteria:`. The comments in the file explain each key.

- **Areas.** Which areas count? For each one, which spellings and sub-areas mean the same place? For example: HSR → "HSR Layout", "Sector 2". These go under `localities:`, and matching is strict.
- **Nearby areas.** Should "nearby" areas go to the digest instead? These go in `nearby_ok:`; the default is `[]`, which means drop them.
- **P1 (whole flat).** Which unit types (`1bhk`, `studio`, …) and what maximum rent?
- **P2 (room in a shared flat).** Which flat sizes, which gender preferences are acceptable, is a private bathroom required, and what maximum rent?
- **Exclusions.** The defaults are posts seeking a flat, PG / co-living, 1RK, and female-only. Confirm them with the user.
- **Over budget.** How far over budget should still reach the digest? This is `over_budget_digest_pct`, default 15.
- **No rent stated.** Should such posts be dropped or sent to the digest? This is `rent_unknown`.

The tool is tuned for Bangalore. The LLM prompt mentions Bangalore areas, and times are in IST. For another city it will still work, but say so to the user.

**Check:** step 8 (`flatwatch ping`) validates the file. Any typo in a key or value fails loudly, naming the field.

### 5. Telegram bot (the user does this)

1. The user opens [@BotFather](https://t.me/BotFather), sends `/newbot`, and pastes the token into `~/.flatwatch/.env` as `TELEGRAM_BOT_TOKEN=` themselves.
2. The user sends any message to their new bot.
3. Get the chat id without printing the token:
   ```bash
   set -a; . ~/.flatwatch/.env; set +a
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -c "import sys,json; r=json.load(sys.stdin)['result']; print(r[-1]['message']['chat']['id'] if r else 'no messages yet')"
   ```
4. Write the number into `.env` as `TELEGRAM_CHAT_ID=`.

### 6. Sources

Ask which Facebook groups and subreddits to watch. Put them in `~/.flatwatch/.env`, comma-separated:

```ini
FLATWATCH_FACEBOOK_GROUPS=https://www.facebook.com/groups/1234567890/,https://www.facebook.com/groups/some-slug/
FLATWATCH_SUBREDDITS=SomeSubreddit,AnotherOne
```

The user must already be a member of private groups. flatwatch never joins groups.

### 7. LLM login

- **Default backend (`extract.backend: agent_sdk`):** the user runs `claude auth login`. `ANTHROPIC_API_KEY` must **not** be set, or calls bill the API instead of the subscription. `flatwatch run` refuses to start if it is set.
- **API key instead:** set `extract.backend: anthropic_api` and add `ANTHROPIC_API_KEY=` to `.env`.

### 8. Ping

```bash
flatwatch ping
```

**Check:** the log shows the config and .env paths, the right number of groups and subreddits, and "db ok". The user receives "flatwatch is alive ✅" in Telegram.

### 9. Log in to Facebook and Reddit (the user does this)

```bash
flatwatch login facebook reddit
```

This opens normal Chrome on flatwatch's own profile at `~/.flatwatch/chrome-profile`. The first tab is a red "FLATWATCH BOT PROFILE" page. The user logs in by hand in the other tabs, including any 2FA, then presses Enter in the terminal. flatwatch closes Chrome cleanly and checks each session headlessly. It never sees the password.

**Check:** `flatwatch fetch facebook --group 1 --dry-run` and `flatwatch fetch reddit --sub <one sub> --dry-run` each print recent posts. Show the user a few and ask whether they match what they see in the group.

### 10. Run it in the background

```bash
flatwatch agent install
```

This installs a LaunchAgent (`local.flatwatch`) that starts at login, restarts on crash, and keeps the Mac awake while on power. Closing the lid still sleeps it.

**Check:** `flatwatch agent status` shows `state = running`. The user gets "▶️ flatwatch started" in Telegram, and `/status` in the bot chat lists every group.

### 11. Optional: backtest (only with the user's OK)

```bash
flatwatch agent stop
flatwatch backtest --days 2
flatwatch agent start
```

This sends what would have been alerted over the past 2 days, labelled 🧪 BACKTEST. It scrolls much deeper than normal (up to 25 scrolls per group), so run it once, not repeatedly.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The Facebook or Reddit login disappears after `flatwatch login` | The profile was opened by a Chrome without flatwatch's keychain flags. Always log in through `flatwatch login`, not by opening the profile some other way. |
| Reddit says "wrong password" in the login window | Reddit can reject logins in an automated browser. Plain `flatwatch login reddit` (without `--automated`) uses normal Chrome; use that. |
| Reddit `.json` returns 403 | This is expected. flatwatch reads the page instead and skips `.json` for 24 hours. |
| Telegram says "facebook paused: checkpoint / login wall" | Hard rule 6: stop. The user checks the account (`flatwatch login facebook`), then sends `/resume facebook`. |
| "no successful fetch for 60 min" alert | Look at `~/.flatwatch/logs/flatwatch.log`. Usually the Mac slept, the network is down, or the Claude login expired. For the last one, the user runs `claude auth login`. |
| "LLM degraded" message | Claude is unreachable or the login expired. A regex fallback keeps obvious matches flowing. Fix the login, and a recovery message follows. |
| `/status` shows "possible gap(s)" | A group got more posts between two visits than 5 scrolls could reach. A few a day is normal. Don't raise the scroll cap without asking (hard rule 5). |
| `another flatwatch run is already running` | The LaunchAgent is running. Use `flatwatch agent stop` before running anything that needs the lock. |

Logs: `~/.flatwatch/logs/flatwatch.log` (full, rotated) and `launchd.err.log` (warnings only).
Database: `~/.flatwatch/flatwatch.db` (SQLite).

## Changing the code

- Run `uv run pytest` before and after any change. All tests must pass.
- The matcher (`src/flatwatch/match.py`) is a pure function. Add a test in `tests/test_match.py` for any rule change.
- If you change the extraction prompt, bump `PROMPT_VERSION` in `src/flatwatch/extract.py`.
- Develop against saved fixtures (`flatwatch fetch ... --dry-run` saves them to `fixtures/`) rather than repeated live fetches. Fixtures contain real people's posts and stay gitignored.
