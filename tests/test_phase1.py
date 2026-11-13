import json

import httpx
import pytest

from flatwatch import db
from flatwatch.config import (
    EXAMPLE_CONFIG,
    ConfigError,
    load_config,
    load_secrets,
    normalize_fb_url,
)
from flatwatch.telegram import Telegram, TelegramError

FAKE_TOKEN = "123:FAKE-test-token"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
              "FLATWATCH_FACEBOOK_GROUPS", "FLATWATCH_SUBREDDITS", "FLATWATCH_CONFIG", "FLATWATCH_ENV"):
        monkeypatch.delenv(k, raising=False)


def test_example_config_valid():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.sources.facebook == [] and cfg.sources.reddit == []     # no personal sources in git
    assert cfg.criteria.tiers.P1.offering_type == ["full_flat"]
    assert cfg.criteria.tiers.P1.unit == ["1bhk", "studio"]
    assert "1rk" in cfg.criteria.exclude.unit
    assert set(cfg.criteria.localities) == {"HSR", "Indiranagar", "Domlur"}


def test_sources_from_env_file_replace_config(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FLATWATCH_FACEBOOK_GROUPS=https://facebook.com/groups/111?ref=x, https://www.facebook.com/groups/222\n"
                   "FLATWATCH_SUBREDDITS=r/SubOne,SubTwo\n")
    cfg = load_config(EXAMPLE_CONFIG, env_file=env)
    assert cfg.sources.facebook == ["https://www.facebook.com/groups/111/", "https://www.facebook.com/groups/222/"]
    assert cfg.sources.reddit == ["SubOne", "SubTwo"]


def test_real_env_wins_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FLATWATCH_SUBREDDITS=FromFile\n")
    monkeypatch.setenv("FLATWATCH_SUBREDDITS", "FromEnv")
    assert load_config(EXAMPLE_CONFIG, env_file=env).sources.reddit == ["FromEnv"]


def test_explicit_path_ignores_discovered_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FLATWATCH_SUBREDDITS=Leaky\n")
    monkeypatch.chdir(tmp_path)
    assert load_config(EXAMPLE_CONFIG).sources.reddit == []


def test_discovery_prefers_cwd_then_home(tmp_path, monkeypatch):
    from flatwatch import config
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", home)
    monkeypatch.chdir(tmp_path)
    assert config.config_path() == home / "config.yaml"             # nothing anywhere: default location
    (home / "config.yaml").write_text("x")
    assert config.config_path() == home / "config.yaml"
    (tmp_path / "config.yaml").write_text("x")
    assert config.config_path() == tmp_path / "config.yaml"
    monkeypatch.setenv("FLATWATCH_CONFIG", "/elsewhere/c.yaml")
    assert str(config.config_path()) == "/elsewhere/c.yaml"


def test_fb_urls_deduped(tmp_path):
    raw = EXAMPLE_CONFIG.read_text().replace(
        "  facebook: []\n",
        "  facebook:\n"
        "    - https://www.facebook.com/groups/1234567890/\n"
        "    - https://facebook.com/groups/1234567890?ref=share\n"
        "    - https://www.facebook.com/groups/1234567890\n"
        "    - https://www.facebook.com/groups/example-group/\n",
    )
    p = tmp_path / "c.yaml"
    p.write_text(raw)
    assert len(load_config(p).sources.facebook) == 2


def test_normalize_fb_url():
    assert (
        normalize_fb_url("http://m.facebook.com/groups/abc?sorting_setting=X#y")
        == "https://www.facebook.com/groups/abc/"
    )


def test_invalid_config_fails_loudly(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(EXAMPLE_CONFIG.read_text().replace("unit: [1bhk, studio]", "unit: [1bhkk]"))
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_env_fails(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_CHAT_ID=1\n")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_secrets(env)


def test_secrets_masked(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=42\n")
    s = load_secrets(env)
    assert s.telegram_chat_id == 42
    assert FAKE_TOKEN not in repr(s) and FAKE_TOKEN not in str(s)


def test_db_migrates(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("posts", "extractions", "decisions", "alerts", "feedback", "runs", "source_state", "llm_usage"):
        assert t in tables
    # idempotent
    assert db.migrate(conn) == len(db.MIGRATIONS)


def _tg(handler):
    return Telegram(FAKE_TOKEN, 42, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _run(coro):
    return await coro


def test_send_and_edit():
    seen = []

    def handler(req):
        seen.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": 42}}})

    import asyncio

    async def go():
        async with _tg(handler) as tg:
            await tg.send_message("<b>hi</b>")
            await tg.edit_message_text(7, "edited")
            await tg.get_updates(offset=5, timeout=0)

    asyncio.run(go())
    assert [m for m, _ in seen] == ["sendMessage", "editMessageText", "getUpdates"]
    assert seen[0][1]["parse_mode"] == "HTML" and seen[0][1]["chat_id"] == 42
    assert seen[1][1]["message_id"] == 7
    assert seen[2][1]["offset"] == 5


def test_errors_do_not_leak_token():
    import asyncio

    def bad(req):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    def boom(req):
        raise httpx.ConnectError(f"failed {req.url}")

    for h in (bad, boom):
        with pytest.raises(TelegramError) as ei:
            asyncio.run(_tg(h).send_message("x"))
        assert FAKE_TOKEN not in str(ei.value)
        assert ei.value.__cause__ is None
