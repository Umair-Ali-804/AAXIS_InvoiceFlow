"""Tamper-evident audit trail.

Every event stores the SHA-256 of the previous event, so editing or deleting a
row in the database breaks the chain and `verify_chain()` reports where.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import db

GENESIS = "0" * 64


def _digest(prev_hash: str, ts: str, actor: str, action: str, invoice_id: int | None,
            summary: str, detail: dict[str, Any]) -> str:
    material = json.dumps(
        [prev_hash, ts, actor, action, invoice_id, summary, detail],
        sort_keys=True, default=str, ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def log(action: str, summary: str, *, invoice_id: int | None = None, actor: str = "system",
        detail: dict[str, Any] | None = None) -> None:
    detail = detail or {}
    with db.connect() as conn:
        last = conn.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS
        ts = db.now_iso()
        digest = _digest(prev_hash, ts, actor, action, invoice_id, summary, detail)
        conn.execute(
            "INSERT INTO audit_events(invoice_id, ts, actor, action, summary, detail, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, ts, actor, action, summary, db.dumps(detail), prev_hash, digest),
        )


def events_for(invoice_id: int) -> list[dict[str, Any]]:
    return db.fetch_all("SELECT * FROM audit_events WHERE invoice_id = ? ORDER BY id", (invoice_id,))


def recent(limit: int = 200) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT a.*, i.ref AS invoice_ref, i.vendor_name AS vendor_name FROM audit_events a "
        "LEFT JOIN invoices i ON i.id = a.invoice_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    )


def verify_chain() -> dict[str, Any]:
    rows = db.fetch_all("SELECT * FROM audit_events ORDER BY id")
    prev = GENESIS
    for row in rows:
        detail = row["detail"] if isinstance(row["detail"], dict) else {}
        expected = _digest(prev, row["ts"], row["actor"], row["action"], row["invoice_id"],
                           row["summary"], detail)
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"ok": False, "events": len(rows), "broken_at": row["id"]}
        prev = row["hash"]
    return {"ok": True, "events": len(rows), "broken_at": None, "head": prev}
