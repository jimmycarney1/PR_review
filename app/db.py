"""SQLite storage. One file, no migrations framework -- schema is created on demand."""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("PIGSKIN_DB", "pigskin.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per call we actually spend against the free-tier quota, so the UI
-- can show how many of the 500 monthly credits are left.
CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    called_at   TEXT NOT NULL,
    remaining   INTEGER,
    used        INTEGER,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT
);

-- A single refresh of the odds board. Every line belongs to one snapshot so we
-- can prove which fetch a pick was made against and how old it was.
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    week        INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL,
    source      TEXT NOT NULL,          -- 'api' | 'manual'
    book        TEXT
);

CREATE TABLE IF NOT EXISTS games (
    id            INTEGER PRIMARY KEY,
    event_id      TEXT UNIQUE,          -- The Odds API event id; NULL for hand-entered games
    week          INTEGER NOT NULL,
    commence_time TEXT,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    home_score    INTEGER,
    away_score    INTEGER,
    completed     INTEGER NOT NULL DEFAULT 0,
    score_source  TEXT,                 -- 'api' | 'manual'
    scored_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_week ON games(week);

-- Spread for one side of one game as of one snapshot.
CREATE TABLE IF NOT EXISTS lines (
    id          INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    team        TEXT NOT NULL,
    spread      REAL NOT NULL,
    price       INTEGER,
    UNIQUE(snapshot_id, game_id, team)
);

CREATE TABLE IF NOT EXISTS picks (
    id              INTEGER PRIMARY KEY,
    week            INTEGER NOT NULL,
    slot            INTEGER NOT NULL,   -- 1..6, draft position within the week
    player          TEXT NOT NULL,
    game_id         INTEGER NOT NULL REFERENCES games(id),
    team            TEXT NOT NULL,
    spread          REAL NOT NULL,
    -- Provenance of the number above.
    line_source     TEXT NOT NULL,      -- 'api' | 'override'
    snapshot_id     INTEGER REFERENCES snapshots(id),
    line_fetched_at TEXT,               -- when the book showed this line
    override_reason TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(week, slot)
);
CREATE INDEX IF NOT EXISTS idx_picks_week ON picks(week);

-- Append-only audit trail. Every create, field edit and delete lands here.
CREATE TABLE IF NOT EXISTS pick_ledger (
    id         INTEGER PRIMARY KEY,
    pick_id    INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    player     TEXT NOT NULL,
    action     TEXT NOT NULL,           -- 'create' | 'edit' | 'delete'
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_week ON pick_ledger(week);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
