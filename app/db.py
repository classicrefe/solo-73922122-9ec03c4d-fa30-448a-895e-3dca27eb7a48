"""SQLite connection helpers.

SQLite is the single source of truth.  Every request opens a short-lived
connection; write transactions acquire ``BEGIN IMMEDIATE`` so concurrent
publishers serialise instead of racing on read-modify-write snapshots.
Readers use ``BEGIN DEFERRED`` and therefore see exactly one committed
snapshot (WAL mode), even if a publish commits while they are reading.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS baskets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    base_month          TEXT    NOT NULL,
    current_version_id  INTEGER,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS basket_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id           INTEGER NOT NULL REFERENCES baskets(id) ON DELETE CASCADE,
    version_no          INTEGER NOT NULL,
    parent_id           INTEGER REFERENCES basket_versions(id),
    status              TEXT    NOT NULL CHECK (status IN ('draft','published')),
    base_month          TEXT    NOT NULL,
    recomputed_from     TEXT,
    reused_point_count  INTEGER NOT NULL DEFAULT 0,
    computed_point_count INTEGER NOT NULL DEFAULT 0,
    snapshot_sha256     TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at        TEXT,
    UNIQUE (basket_id, version_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_versions_one_draft
    ON basket_versions(basket_id) WHERE status = 'draft';

-- A weighted basket position ("slot").  Its weight is fixed for the
-- lifetime of the version; a replacement swaps the representative item
-- inside the slot and never changes the slot weight.
CREATE TABLE IF NOT EXISTS basket_slots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_version_id   INTEGER NOT NULL REFERENCES basket_versions(id) ON DELETE CASCADE,
    slot_key            TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    weight_bp           INTEGER NOT NULL CHECK (weight_bp > 0 AND weight_bp <= 10000),
    UNIQUE (basket_version_id, slot_key)
);

-- Historical item representatives.  A replacement creates a second item
-- in the same slot; both old and new overlap for one month (the old item's
-- active_through equals the new item's active_from).
CREATE TABLE IF NOT EXISTS basket_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_version_id   INTEGER NOT NULL REFERENCES basket_versions(id) ON DELETE CASCADE,
    slot_key            TEXT    NOT NULL,
    code                TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    active_from         TEXT    NOT NULL,
    active_through      TEXT,
    predecessor_code    TEXT,
    UNIQUE (basket_version_id, code),
    UNIQUE (basket_version_id, slot_key, active_from),
    FOREIGN KEY (basket_version_id, slot_key)
        REFERENCES basket_slots(basket_version_id, slot_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_version_id   INTEGER NOT NULL REFERENCES basket_versions(id) ON DELETE CASCADE,
    item_code           TEXT    NOT NULL,
    month               TEXT    NOT NULL,
    price_cents         INTEGER NOT NULL CHECK (price_cents > 0),
    UNIQUE (basket_version_id, item_code, month),
    FOREIGN KEY (basket_version_id, item_code)
        REFERENCES basket_items(basket_version_id, code) ON DELETE CASCADE
);

-- Exact rational index values.  NUM/DEN are decimal strings because the
-- integers can exceed SQLite's signed 64-bit range after repeated chaining.
CREATE TABLE IF NOT EXISTS index_points (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_version_id   INTEGER NOT NULL REFERENCES basket_versions(id) ON DELETE CASCADE,
    month               TEXT    NOT NULL,
    mom_num             TEXT,
    mom_den             TEXT,
    index_num           TEXT    NOT NULL,
    index_den           TEXT    NOT NULL,
    origin              TEXT    NOT NULL CHECK (origin IN ('reused','computed')),
    UNIQUE (basket_version_id, month)
);

CREATE INDEX IF NOT EXISTS ix_versions_basket ON basket_versions(basket_id, version_no);
CREATE INDEX IF NOT EXISTS ix_items_version ON basket_items(basket_version_id, slot_key, active_from);
CREATE INDEX IF NOT EXISTS ix_prices_version ON prices(basket_version_id, item_code, month);

-- Published snapshots are append-only.  The API never edits them (revisions
-- copy into a new draft); these triggers make that a database invariant too.
CREATE TRIGGER IF NOT EXISTS trg_prices_no_update_published
BEFORE UPDATE ON prices
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published prices are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_prices_no_delete_published
BEFORE DELETE ON prices
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published prices are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_no_update_published
BEFORE UPDATE ON basket_items
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published basket items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_no_delete_published
BEFORE DELETE ON basket_items
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published basket items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_slots_no_update_published
BEFORE UPDATE ON basket_slots
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published basket weights are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_slots_no_delete_published
BEFORE DELETE ON basket_slots
WHEN (SELECT status FROM basket_versions WHERE id = OLD.basket_version_id) = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published basket weights are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_points_no_update
BEFORE UPDATE ON index_points
BEGIN
    SELECT RAISE(ABORT, 'published index points are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_points_no_delete
BEFORE DELETE ON index_points
BEGIN
    SELECT RAISE(ABORT, 'published index points are immutable');
END;
"""


def connect(db_path: Path, busy_timeout_ms: int = 5_000) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, timeout=busy_timeout_ms / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


def initialise(db_path: Path, busy_timeout_ms: int = 5_000) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def read_txn(
    db_path: Path, busy_timeout_ms: int = 5_000
) -> Iterator[sqlite3.Connection]:
    """A deferred transaction that sees one consistent committed snapshot."""
    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.execute("BEGIN DEFERRED")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def write_txn(
    db_path: Path, busy_timeout_ms: int = 5_000
) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path, busy_timeout_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
