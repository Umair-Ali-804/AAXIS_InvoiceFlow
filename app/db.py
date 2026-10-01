"""SQLite persistence. Plain sqlite3 keeps the project dependency-light and Colab-friendly."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    aliases         TEXT NOT NULL DEFAULT '[]',
    email           TEXT,
    tax_id          TEXT,
    category        TEXT,
    terms_days      INTEGER DEFAULT 30,
    qbo_vendor_id   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ref                 TEXT UNIQUE,
    status              TEXT NOT NULL,
    source              TEXT NOT NULL,
    source_detail       TEXT,
    file_name           TEXT NOT NULL,
    file_path           TEXT NOT NULL,
    file_hash           TEXT NOT NULL,
    mime                TEXT,
    page_count          INTEGER DEFAULT 0,
    extraction_method   TEXT,
    ocr_quality         REAL,
    processing_ms       INTEGER,
    vendor_id           INTEGER REFERENCES vendors(id),
    vendor_name         TEXT,
    invoice_number      TEXT,
    invoice_date        TEXT,
    due_date            TEXT,
    po_number           TEXT,
    currency            TEXT,
    subtotal            REAL,
    tax                 REAL,
    shipping            REAL,
    total               REAL,
    fields              TEXT NOT NULL DEFAULT '{}',
    line_items          TEXT NOT NULL DEFAULT '[]',
    issues              TEXT NOT NULL DEFAULT '[]',
    approvals           TEXT NOT NULL DEFAULT '[]',
    words               TEXT NOT NULL DEFAULT '[]',
    raw_text            TEXT,
    duplicate_of        INTEGER,
    qbo_bill_id         TEXT,
    qbo_mode            TEXT,
    qbo_payload         TEXT,
    qbo_response        TEXT,
    rejected_reason     TEXT,
    received_at         TEXT NOT NULL,
    processed_at        TEXT,
    posted_at           TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS ix_invoices_hash ON invoices(file_hash);
CREATE INDEX IF NOT EXISTS ix_invoices_vendor_number ON invoices(vendor_id, invoice_number);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_invoice ON audit_events(invoice_id);

CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qbo_simulator_bills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

JSON_COLUMNS = {"fields", "line_items", "issues", "approvals", "words", "aliases", "detail",
                "qbo_payload", "qbo_response", "payload"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        out[key] = value
    return out


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def fetch_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute(sql, params).fetchone())


def fetch_all(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]  # type: ignore[misc]


def execute(sql: str, params: tuple | list = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def update_invoice(invoice_id: int, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = now_iso()
    cols, params = [], []
    for key, value in values.items():
        cols.append(f"{key} = ?")
        params.append(dumps(value) if key in JSON_COLUMNS and not isinstance(value, str) else value)
    params.append(invoice_id)
    execute(f"UPDATE invoices SET {', '.join(cols)} WHERE id = ?", params)


def get_invoice(invoice_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))


def kv_get(key: str, default: Any = None) -> Any:
    row = fetch_one("SELECT value FROM kv WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def kv_set(key: str, value: Any) -> None:
    execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, dumps(value)))
