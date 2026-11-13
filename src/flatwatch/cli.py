"""flatwatch command line."""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

from . import db
from .config import ConfigError, auth_source, load_config, load_secrets
from .telegram import Telegram, TelegramError

IST = ZoneInfo("Asia/Kolkata")
DEV_MAX_GROUPS_PER_RUN = 3
DEV_MAX_VISITS_PER_HOUR = 9     # ~3 dry runs of 3 groups


# ------------------------------------------------------------------ helpers --

async def _telegram_alert(text: str) -> None:
    try:
        s = load_secrets()
        async with Telegram(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id) as tg:
            await tg.send_message(text)
    except (ConfigError, TelegramError) as e:
        logger.error("could not send Telegram alert: {}", e)


def _source_paused(conn, source: str) -> str | None:
    row = conn.execute(
        "SELECT pause_reason FROM source_state WHERE source=? AND source_key='*' AND paused=1", (source,)
    ).fetchone()
    return row["pause_reason"] if row else None


def _pause_source(conn, source: str, reason: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO source_state(source, source_key, paused, pause_reason) VALUES (?, '*', 1, ?)
               ON CONFLICT(source, source_key) DO UPDATE SET paused=1, pause_reason=excluded.pause_reason""",
            (source, reason),
        )


def _cache_display_name(conn, source: str, key: str, name: str) -> None:
    if not name:
        return
    with conn:
        conn.execute(
            """INSERT INTO source_state(source, source_key, display_name) VALUES (?, ?, ?)
               ON CONFLICT(source, source_key) DO UPDATE SET display_name=excluded.display_name""",
            (source, key, name),
        )


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "time unknown"
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    ago = f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago" if mins < 2880 else f"{mins // 1440} d ago"
    return f"{dt.astimezone(IST):%a %d %b %H:%M} IST ({ago})"


# ----------------------------------------------------------------- commands --

async def _ping() -> int:
    cfg = load_config()
    secrets = load_secrets()
    from .config import config_path, env_path
    logger.info("config ok ({}, {}): {} FB groups, {} subreddits", config_path(), env_path(),
                len(cfg.sources.facebook), len(cfg.sources.reddit))
    logger.info("LLM auth source: {}", auth_source(cfg))
    conn = db.connect()
    logger.info("db ok: {} (schema v{})", db.DB_PATH, db.schema_version(conn))
    conn.close()
    async with Telegram(secrets.telegram_bot_token.get_secret_value(), secrets.telegram_chat_id) as tg:
        msg = await tg.send_message("flatwatch is alive ✅")
    logger.info("sent to chat {} (message_id {})", msg["chat"]["id"], msg["message_id"])
    return 0


async def _login(site: str) -> int:
    from .browser import HOME_URLS, is_logged_in, open_context

    async with open_context(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(HOME_URLS[site])
        print(f"\nLog in to {site} by hand in the Chrome window (complete any 2FA there).")
        print("flatwatch does not read or type anything on the page.")
        print("Waiting for the login cookie (up to 15 min); Enter here also continues.", flush=True)
        loop = asyncio.get_running_loop()
        enter: asyncio.Future = loop.create_future()

        def _wait_enter() -> None:
            sys.stdin.readline()
            loop.call_soon_threadsafe(lambda: enter.done() or enter.set_result(None))

        # daemon thread: asyncio.run() would otherwise block on exit waiting for stdin
        threading.Thread(target=_wait_enter, daemon=True).start()
        deadline = time.monotonic() + 900
        ok = await is_logged_in(ctx, site)
        while not ok and not enter.done() and time.monotonic() < deadline:
            await asyncio.sleep(3)
            ok = await is_logged_in(ctx, site)
        if ok and not enter.done():
            print("Login detected; letting the session settle for 15 s...", flush=True)
            await asyncio.sleep(15)  # let 2FA "remember browser" etc. finish and cookies flush
        ok = await is_logged_in(ctx, site)
        if not ok:
            from .browser import LOGIN_COOKIES

            names = sorted({c["name"] for c in await ctx.cookies(LOGIN_COOKIES[site][0])})
            logger.info("cookie names present for {} (values not shown): {}", site, ", ".join(names) or "none")
    if ok:
        logger.info("{} session cookie present; profile saved", site)
        return 0
    logger.error("no {} session cookie found; run `flatwatch login {}` again", site, site)
    return 1


CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


BANNER_HTML = (
    "data:text/html;charset=utf-8,<title>FLATWATCH BOT PROFILE</title>"
    "<body style='background:%23b00020;color:white;font:700 44px system-ui;padding:60px'>"
    "FLATWATCH BOT PROFILE<p style='font-size:22px;font-weight:400'>Log in to each site in the other tabs "
    "of THIS window, then press Enter in the terminal.</p></body>"
)


async def _login_plain(sites: list[str]) -> int:
    """Open the bot profile in ordinary Chrome (no Playwright / automation at all) with
    a red banner tab plus one login tab per site, wait for Enter (or quit), then verify."""
    import subprocess
    from urllib.parse import quote

    from .browser import HOME_URLS, LOGIN_COOKIES, PROFILE_DIR, is_logged_in, open_context

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nOpening a Chrome window on the bot profile ({PROFILE_DIR}).")
    print("Its first tab is a red 'FLATWATCH BOT PROFILE' page; log in to "
          + " and ".join(sites) + " in the other tabs of THAT window (not your usual Chrome).")
    print("Then press Enter HERE; flatwatch quits that window cleanly.", flush=True)
    proc = await asyncio.to_thread(
        subprocess.Popen,
        # --use-mock-keychain / --password-store=basic match Playwright's defaults, so cookies are
        # encrypted with the same key. Without them each side deletes the other's cookies.
        [CHROME_BIN, f"--user-data-dir={PROFILE_DIR}", "--use-mock-keychain", "--password-store=basic",
         "--no-first-run", "--no-default-browser-check", "--new-window",
         quote(BANNER_HTML, safe=":;,=<>/'%"), *(HOME_URLS[s] for s in sites)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    loop = asyncio.get_running_loop()
    enter: asyncio.Future = loop.create_future()

    def _wait_enter() -> None:
        sys.stdin.readline()
        loop.call_soon_threadsafe(lambda: enter.done() or enter.set_result(None))

    threading.Thread(target=_wait_enter, daemon=True).start()
    exited = asyncio.ensure_future(asyncio.to_thread(proc.wait))
    await asyncio.wait({exited, enter}, return_when=asyncio.FIRST_COMPLETED)
    if not exited.done():
        proc.terminate()  # SIGTERM = Chrome's normal, cookie-flushing shutdown
        await exited
    print("Chrome closed; verifying sessions headlessly...", flush=True)
    failed = []
    async with open_context(headless=True) as ctx:
        for site in sites:
            if await is_logged_in(ctx, site):
                logger.info("{}: logged in; profile saved", site)
            else:
                failed.append(site)
                names = sorted({c["name"] for c in await ctx.cookies(LOGIN_COOKIES[site][0])})
                logger.error("{}: NOT logged in (cookie names, values not shown: {})", site, ", ".join(names) or "none")
    if failed:
        logger.error("run `flatwatch login {}` again", " ".join(failed))
        return 1
    return 0


def _select_groups(cfg, conn, wanted: list[str]) -> list[str]:
    groups = cfg.sources.facebook
    if not wanted:
        return groups[:1]
    names = {
        r["source_key"]: (r["display_name"] or "")
        for r in conn.execute("SELECT source_key, display_name FROM source_state WHERE source='facebook'")
    }
    out = []
    for w in wanted:
        wl = w.lower()
        if w.isdigit() and 1 <= int(w) <= len(groups) and len(w) < 3:
            hits = [groups[int(w) - 1]]
        else:
            hits = [g for g in groups if wl in g.lower() or wl in names.get(g, "").lower()]
        if len(hits) != 1:
            raise ConfigError(f"--group {w!r} matched {len(hits)} groups; use a number 1-{len(groups)} or a URL part")
        if hits[0] not in out:
            out.append(hits[0])
    return out


DEV_GAPS = {"facebook": (20, 60), "reddit": (8, 20)}
SOURCE_NOUN = {"facebook": "group", "reddit": "sub"}


async def _dev_fetch(args, source: str, keys: list[str], fetch_one) -> int:
    """Shared dry-run loop: rate cap, pause-on-block + Telegram alert, run log, printing."""
    from .browser import BlockedError, is_logged_in, open_context

    if not args.dry_run:
        raise ConfigError("only --dry-run is supported until Phase 5")
    conn = db.connect()
    if reason := _source_paused(conn, source):
        logger.error("{} is paused ({}). Check the account by hand, then `flatwatch resume {}`.", source, reason, source)
        return 1
    noun = SOURCE_NOUN[source]
    if len(keys) > DEV_MAX_GROUPS_PER_RUN:
        raise ConfigError(f"dev runs are capped at {DEV_MAX_GROUPS_PER_RUN} {noun}s")
    recent = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE source=? AND started_at > ?", (source, int(time.time()) - 3600)
    ).fetchone()[0]
    if recent + len(keys) > DEV_MAX_VISITS_PER_HOUR:
        raise ConfigError(
            f"{recent} {source} {noun} visits in the last hour; dev cap is {DEV_MAX_VISITS_PER_HOUR}/h. "
            f"Iterate on fixtures/ instead, or wait."
        )

    headless = not args.headed
    async with open_context(headless=headless, offscreen=args.offscreen) as ctx:
        if not await is_logged_in(ctx, source):
            logger.error("not logged in to {}; run `flatwatch login {}` first", source, source)
            return 1
        for i, key in enumerate(keys):
            if i:
                gap = random.uniform(*DEV_GAPS[source])
                logger.info("waiting {:.0f}s before next {}", gap, noun)
                await asyncio.sleep(gap)
            cur = conn.execute(
                "INSERT INTO runs(source, source_key, started_at) VALUES (?, ?, ?)", (source, key, int(time.time()))
            )
            conn.commit()
            run_id = cur.lastrowid
            try:
                title, posts, stats = await fetch_one(ctx, key)
            except BlockedError as e:
                with conn:
                    conn.execute("UPDATE runs SET finished_at=?, ok=0, error=? WHERE id=?",
                                 (int(time.time()), f"blocked: {e}", run_id))
                _pause_source(conn, source, str(e))
                logger.error("BLOCKED on {}: {}. {} paused; not retrying.", key, e, source)
                await _telegram_alert(
                    f"⛔ <b>flatwatch: {source} paused</b>\n{e}\n{noun.title()}: {key}\n"
                    f"Check the account in the bot's Chrome profile, then run <code>flatwatch resume {source}</code>."
                )
                return 1
            except Exception as e:
                with conn:
                    conn.execute("UPDATE runs SET finished_at=?, ok=0, error=? WHERE id=?",
                                 (int(time.time()), f"{type(e).__name__}: {e}"[:500], run_id))
                raise
            with conn:
                conn.execute("UPDATE runs SET finished_at=?, ok=1, posts_seen=? WHERE id=?",
                             (int(time.time()), len(posts), run_id))
            _cache_display_name(conn, source, key, title)

            print(f"\n=== {title or key}  [{'headless' if headless else 'headed'}]")
            print(f"    {key}")
            print("    " + ", ".join(f"{k}={v}" for k, v in stats.items()))
            for n, p in enumerate(posts[: args.limit], 1):
                text = " ".join(p.text.split())
                if not args.full and len(text) > 300:
                    text = text[:300] + f"… (+{len(p.text) - 300} chars)"
                print(f"\n[{n}] {_fmt_time(p.created_at)} · {p.author or '?'} · via {p.via}"
                      f"{' · ' + str(len(p.image_urls)) + ' img' if p.image_urls else ''}")
                print(f"    {p.url}")
                if p.shared_from:
                    print(f"    ↪ reshare of {p.shared_from}")
                print(f"    {text or '(no text)'}")
            if not posts:
                print("    (no posts parsed — inspect the fixture dir above)")
    return 0


async def _fetch_facebook(args) -> int:
    from .browser import detect_block
    from .sources.facebook import fetch_group

    cfg = load_config()
    groups = _select_groups(cfg, db.connect(), args.group)

    async def one(ctx, url):
        return await fetch_group(ctx, url, save_fixtures=True, detect_block=detect_block)

    return await _dev_fetch(args, "facebook", groups, one)


async def _fetch_reddit(args) -> int:
    from .browser import detect_block
    from .sources.reddit import fetch_sub

    cfg = load_config()
    subs = cfg.sources.reddit
    if args.sub:
        by_lower = {s.lower(): s for s in subs}
        unknown = [s for s in args.sub if s.lower() not in by_lower]
        if unknown:
            raise ConfigError(f"--sub {unknown} not in your configured subreddits")
        subs = list(dict.fromkeys(by_lower[s.lower()] for s in args.sub))
    else:
        subs = subs[:1]

    async def one(ctx, sub):
        return await fetch_sub(ctx, sub, save_fixtures=True, detect_block=detect_block, force_dom=args.dom)

    return await _dev_fetch(args, "reddit", subs, one)


async def _eval(args) -> int:
    from . import evals

    cfg = load_config()
    logger.info("LLM auth source: {}", auth_source(cfg))
    if args.build_corpus or not evals.CORPUS.exists():
        n = len(evals.build_corpus())
        logger.info("corpus rebuilt from fixtures/: {} posts", n)
    if not evals.LABELS.exists():
        raise ConfigError(f"no labels at {evals.LABELS}")
    report = await evals.run_eval(cfg)
    print(evals.format_report(report, verbose=args.verbose))
    e = report["e2e"]
    ok = (report["prefilter_recall"] >= evals.TARGETS["prefilter_recall"]
          and e["recall"] >= evals.TARGETS["e2e_recall"] and e["precision"] >= evals.TARGETS["e2e_precision"])
    return 0 if ok else 3


async def _run(args) -> int:
    import fcntl
    import os
    import signal

    from .config import DATA_DIR
    from .scheduler import Runner

    cfg = load_config()
    secrets = load_secrets()
    if cfg.extract.backend == "agent_sdk" and os.environ.get("ANTHROPIC_API_KEY"):
        raise ConfigError("ANTHROPIC_API_KEY is set: Agent SDK calls would bill the API. Unset it before `flatwatch run`.")
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger.add(logs / "flatwatch.log", rotation="10 MB", retention=5, enqueue=True, level="INFO")
    if not sys.stderr.isatty():
        # under launchd stderr goes to a file that never rotates: keep only warnings/errors there
        logger.remove(0)
        logger.add(sys.stderr, level="WARNING")
    lock_file = open(DATA_DIR / "flatwatch.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ConfigError("another `flatwatch run` is already running (it holds ~/.flatwatch/flatwatch.lock)") from None
    logger.info("flatwatch run starting; LLM auth source: {}", auth_source(cfg))

    conn = db.connect()
    tg = Telegram(secrets.telegram_bot_token.get_secret_value(), secrets.telegram_chat_id)
    runner = Runner(cfg, conn, tg, headless=not args.headed)
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await runner.run()
    except asyncio.CancelledError:
        logger.info("shutting down")
    finally:
        try:
            await asyncio.wait_for(runner.notifier.flush(), 10)
            await asyncio.wait_for(tg.send_message("⏹ flatwatch stopped", silent=True), 10)
        except Exception:
            pass
        await tg.aclose()
        conn.close()
    return 0


async def _backtest(args) -> int:
    import fcntl

    from .backtest import run_backtest
    from .config import DATA_DIR

    cfg, secrets = load_config(), load_secrets()
    lock_file = open(DATA_DIR / "flatwatch.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ConfigError("the live bot is running; stop it first: flatwatch agent stop") from None
    conn = db.connect()
    async with Telegram(secrets.telegram_bot_token.get_secret_value(), secrets.telegram_chat_id) as tg:
        return await run_backtest(cfg, conn, tg, days=args.days, fb_max_scrolls=args.fb_max_scrolls,
                                  reddit_max_scrolls=args.reddit_max_scrolls)


async def _resume(source: str) -> int:
    from . import store

    n = store.resume_source(db.connect(), source)
    logger.info("{} resumed (pause and backoff cleared)" if n else "{} was not paused", source)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flatwatch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create config.yaml and .env (default in ~/.flatwatch)")
    p_init.add_argument("--dir", type=Path, default=None, help="where to put them (default ~/.flatwatch)")
    p_agent = sub.add_parser("agent", help="manage the macOS LaunchAgent that keeps `flatwatch run` going")
    p_agent.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    sub.add_parser("ping", help="validate config and send a Telegram test message")

    p_login = sub.add_parser("login", help="open headed Chrome and wait for you to log in by hand")
    p_login.add_argument("sites", nargs="+", choices=["facebook", "reddit"])
    p_login.add_argument("--automated", action="store_true",
                         help="open the login page via Playwright instead of plain Chrome (some sites reject it)")

    p_fetch = sub.add_parser("fetch", help="fetch one source (dev)")
    fsub = p_fetch.add_subparsers(dest="source", required=True)
    p_fb = fsub.add_parser("facebook")
    p_fb.add_argument("--group", action="append", default=[],
                      help="group number (1-based, config order), URL part or cached name; repeatable, max 3")
    p_rd = fsub.add_parser("reddit")
    p_rd.add_argument("--sub", action="append", default=[], help="subreddit from config; repeatable, max 3")
    p_rd.add_argument("--dom", action="store_true", help="skip .json and read shreddit-post elements")
    for p in (p_fb, p_rd):
        p.add_argument("--dry-run", action="store_true", help="print posts; no DB posts, no alerts")
        p.add_argument("--headed", action="store_true", help="show the browser window (default headless)")
        p.add_argument("--offscreen", action="store_true", help="with --headed: place the window off-screen")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--full", action="store_true", help="print full post text")

    p_eval = sub.add_parser("eval", help="run prefilter + LLM extraction + matcher against evals/labeled.jsonl")
    p_eval.add_argument("--build-corpus", action="store_true", help="rebuild evals/corpus.jsonl from fixtures/ first")
    p_eval.add_argument("-v", "--verbose", action="store_true", help="list every field and tier error")

    p_run = sub.add_parser("run", help="run the bot in the foreground: fetch, filter, alert (`flatwatch agent install` runs it in the background)")
    p_run.add_argument("--headed", action="store_true", help="show the browser window (default headless)")

    p_bt = sub.add_parser("backtest", help="one-off: deep-fetch the past N days and send what would have been alerted")
    p_bt.add_argument("--days", type=float, default=2)
    p_bt.add_argument("--fb-max-scrolls", type=int, default=25)
    p_bt.add_argument("--reddit-max-scrolls", type=int, default=15)

    p_resume = sub.add_parser("resume", help="clear a paused source after you've checked it by hand")
    p_resume.add_argument("source", choices=["facebook", "reddit"])

    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            from .agent import init
            return init(args.dir)
        if args.cmd == "agent":
            from .agent import agent
            return agent(args.action)
        if args.cmd == "ping":
            return asyncio.run(_ping())
        if args.cmd == "login":
            if args.automated:
                rc = 0
                for site in dict.fromkeys(args.sites):
                    rc = rc or asyncio.run(_login(site))
                return rc
            return asyncio.run(_login_plain(list(dict.fromkeys(args.sites))))
        if args.cmd == "fetch" and args.source == "facebook":
            return asyncio.run(_fetch_facebook(args))
        if args.cmd == "fetch" and args.source == "reddit":
            return asyncio.run(_fetch_reddit(args))
        if args.cmd == "run":
            return asyncio.run(_run(args))
        if args.cmd == "backtest":
            return asyncio.run(_backtest(args))
        if args.cmd == "eval":
            return asyncio.run(_eval(args))
        if args.cmd == "resume":
            return asyncio.run(_resume(args.source))
    except (ConfigError, TelegramError) as e:
        logger.error("{}", e)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
