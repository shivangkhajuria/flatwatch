"""SQLite schema and migrations. DB lives in ~/.flatwatch/flatwatch.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "flatwatch.db"

# Each entry is one migration; index + 1 == schema version. Append only.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE posts (
        id            TEXT PRIMARY KEY,          -- "{source}:{post_id}"
        source        TEXT NOT NULL,             -- facebook | reddit
        source_key    TEXT NOT NULL,             -- group url / subreddit
        post_id       TEXT NOT NULL,
        author        TEXT,
        text          TEXT,
        url           TEXT,
        image_urls    TEXT,                      -- JSON array
        created_at    INTEGER,                   -- unix seconds
        fetched_at    INTEGER NOT NULL,
        norm_text     TEXT,
        phone         TEXT,
        duplicate_of  TEXT REFERENCES posts(id),
        prefilter_pass INTEGER
    );
    CREATE INDEX posts_created ON posts(created_at);
    CREATE INDEX posts_phone ON posts(phone);

    CREATE TABLE extractions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id        TEXT NOT NULL REFERENCES posts(id),
        model          TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        listing_json   TEXT,                     -- NULL on failure
        error          TEXT,
        created_at     INTEGER NOT NULL
    );
    CREATE INDEX extractions_post ON extractions(post_id);

    CREATE TABLE decisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id       TEXT NOT NULL REFERENCES posts(id),
        extraction_id INTEGER REFERENCES extractions(id),
        tier          TEXT NOT NULL CHECK (tier IN ('P1','P2','DIGEST','DROP')),
        reasons       TEXT,                      -- JSON array
        tags          TEXT,                      -- JSON array
        unverified    INTEGER NOT NULL DEFAULT 0,
        created_at    INTEGER NOT NULL
    );
    CREATE INDEX decisions_post ON decisions(post_id);

    CREATE TABLE alerts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id         TEXT NOT NULL REFERENCES posts(id),
        kind            TEXT NOT NULL CHECK (kind IN ('instant','digest')),
        telegram_msg_id INTEGER,
        sent_at         INTEGER NOT NULL
    );
    CREATE INDEX alerts_post ON alerts(post_id);

    CREATE TABLE feedback (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id     TEXT NOT NULL REFERENCES posts(id),
        alert_id    INTEGER REFERENCES alerts(id),
        relevant    INTEGER NOT NULL,           -- 1 = 👍, 0 = 👎
        created_at  INTEGER NOT NULL
    );

    CREATE TABLE runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source        TEXT NOT NULL,
        source_key    TEXT NOT NULL,
        started_at    INTEGER NOT NULL,
        finished_at   INTEGER,
        ok            INTEGER,
        posts_seen    INTEGER DEFAULT 0,
        posts_new     INTEGER DEFAULT 0,
        error         TEXT
    );
    CREATE INDEX runs_source ON runs(source, source_key, started_at);

    CREATE TABLE source_state (
        source              TEXT NOT NULL,
        source_key          TEXT NOT NULL,
        display_name        TEXT,
        paused              INTEGER NOT NULL DEFAULT 0,
        pause_reason        TEXT,
        backoff_until       INTEGER,
        backoff_level       INTEGER NOT NULL DEFAULT 0,
        last_seen_post_time INTEGER,
        last_success_at     INTEGER,
        PRIMARY KEY (source, source_key)
    );

    CREATE TABLE llm_usage (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        backend       TEXT NOT NULL,
        model         TEXT NOT NULL,
        posts         INTEGER NOT NULL,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        cost_usd      REAL,
        created_at    INTEGER NOT NULL
    );

    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v2 (Phase 5): digest bookkeeping, cross-post edits, dedupe hints, health state
    """
    ALTER TABLE decisions ADD COLUMN digest_sent_at INTEGER;
    ALTER TABLE posts ADD COLUMN locality_hint TEXT;
    ALTER TABLE posts ADD COLUMN numbers_sig TEXT;
    ALTER TABLE alerts ADD COLUMN text TEXT;
    ALTER TABLE alerts ADD COLUMN also_in TEXT;
    ALTER TABLE source_state ADD COLUMN consecutive_empty INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX decisions_digest ON decisions(tier, digest_sent_at);
    CREATE INDEX posts_fetched ON posts(fetched_at);
    """,
    # v3: a visit that loaded posts but never reached the last seen one (possible missed posts)
    """
    ALTER TABLE runs ADD COLUMN gap INTEGER NOT NULL DEFAULT 0;
    """,
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
    return schema_version(conn)
