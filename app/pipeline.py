"""The invoice pipeline and every reviewer action. Each step writes to the audit trail."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from . import audit, db, llm, quickbooks
from .config import settings
from .documents import UnsupportedDocument, read_document
from .extract import KEY_FIELDS, extract, normalize_vendor
from .validate import CONFIDENCE_CHECKED, blocking, route, validate

LABELS = {**CONFIDENCE_CHECKED, "currency": "Currency"}

SEED_VENDORS = [
    {"name": "Summit Office Supply Co.", "aliases": ["Summit Office Supply"], "email": "billing@summitofficesupply.example",
     "category": "Office supplies", "terms_days": 30},
    {"name": "Evergreen Facilities Maintenance", "aliases": ["Evergreen Facilities"], "email": "ar@evergreenfm.example",
     "category": "Repairs & maintenance", "terms_days": 30},
    {"name": "BrightLine Electrical Services LLC", "aliases": ["BrightLine Electrical"], "email": "invoices@brightline-elec.example",
     "category": "Contractors", "terms_days": 15},
    {"name": "Cascade Freight Partners", "aliases": ["Cascade Freight"], "email": "billing@cascadefreight.example",
     "category": "Freight & delivery", "terms_days": 30},
    {"name": "Keystone Janitorial Group", "aliases": ["Keystone Janitorial"], "email": "accounts@keystonejg.example",
     "category": "Cleaning", "terms_days": 30},
    {"name": "Northgate HVAC Solutions", "aliases": ["Northgate HVAC"], "email": "billing@northgatehvac.example",
     "category": "Repairs & maintenance", "terms_days": 45},
]

REVIEW_STATES = {"needs_review", "ready", "duplicate"}
FINAL_STATES = {"posted", "rejected"}


class ActionError(ValueError):
    """Raised when a reviewer action isn't allowed in the invoice's current state."""


# --------------------------------------------------------------------------- setup

def seed_vendors() -> None:
    if db.fetch_one("SELECT id FROM vendors LIMIT 1"):
        return
    for vendor in SEED_VENDORS:
        db.execute(
            "INSERT INTO vendors(name, aliases, email, category, terms_days, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (vendor["name"], db.dumps(vendor["aliases"]), vendor["email"], vendor["category"],
             vendor["terms_days"], db.now_iso()))


def vendors() -> list[dict[str, Any]]:
    return db.fetch_all("SELECT * FROM vendors ORDER BY name")


def match_vendor_name(name: str) -> dict[str, Any] | None:
    target = normalize_vendor(name)
    if not target:
        return None
    best, best_score = None, 0.0
    for vendor in vendors():
        for candidate in [vendor["name"], *(vendor.get("aliases") or [])]:
            score = fuzz.token_set_ratio(target, normalize_vendor(candidate))
            if score > best_score:
                best, best_score = vendor, score
    return best if best_score >= 90 else None


# --------------------------------------------------------------------------- ingest

def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "document"


def ingest(content: bytes, filename: str, *, source: str = "upload", source_detail: str | None = None,
           actor: str = "system") -> int:
    if not content:
        raise UnsupportedDocument("The file is empty.")
    settings.ensure_dirs()
    file_hash = hashlib.sha256(content).hexdigest()
    safe = _safe_name(filename)
    path = settings.uploads_dir / f"{file_hash[:12]}_{safe}"
    path.write_bytes(content)
    now = db.now_iso()
    invoice_id = db.execute(
        "INSERT INTO invoices(status, source, source_detail, file_name, file_path, file_hash, mime, received_at, updated_at)"
        " VALUES ('processing', ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_detail, safe, str(path), file_hash, mimetypes.guess_type(safe)[0], now, now))
    ref = f"AP-{1000 + invoice_id}"
    db.update_invoice(invoice_id, ref=ref)
    via = {"upload": "uploaded", "email": "received by email", "demo": "loaded as a demo case"}.get(source, source)
    audit.log("received", f"{safe} {via}", invoice_id=invoice_id, actor=actor,
              detail={"file": safe, "sha256": file_hash, "source": source, "from": source_detail})
    process(invoice_id)
    return invoice_id


def process(invoice_id: int, actor: str = "system") -> None:
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise ActionError("Invoice not found.")
    started = time.perf_counter()
    try:
        document = read_document(Path(invoice["file_path"]), f"{invoice['ref']}_{invoice['file_hash'][:8]}")
        result = extract(document.words, vendors(), document.method, settings.math_tolerance)
        result, disagreements, llm_error = llm.cross_check(result)
    except Exception as exc:  # the file stays in the queue with a clear reason
        db.update_invoice(invoice_id, status="failed", processed_at=db.now_iso(), issues=[{
            "id": "processing_failed", "severity": "error", "title": "Couldn’t read this file",
            "message": str(exc), "field": None, "status": "open", "evidence": {}}])
        audit.log("processing_failed", f"Processing failed: {exc}", invoice_id=invoice_id, actor=actor)
        return

    fields = result["fields"]
    _fill_due_date(fields)
    method = document.method + ("+llm" if settings.llm_enabled and not llm_error else "")
    elapsed = int((time.perf_counter() - started) * 1000)

    words = [w.as_dict() for w in document.words]
    pages = [{"image": p.image_path, "width": p.width, "height": p.height, "deskew": p.rotation_corrected}
             for p in document.pages]
    fields["_pages"] = {"value": pages}

    _save_extraction(invoice_id, fields, result["line_items"], extraction_method=method,
                     ocr_quality=document.quality, processing_ms=elapsed, page_count=len(pages),
                     words=words, raw_text=result["text"], processed_at=db.now_iso())
    found = [k for k in KEY_FIELDS if (fields.get(k) or {}).get("value") not in (None, "")]
    audit.log("extracted",
              f"Read {len(found)} fields and {len(result['line_items'])} line items "
              f"({'text layer' if document.method == 'text-layer' else f'OCR, {document.quality:.0%} avg confidence'})",
              invoice_id=invoice_id, actor="system",
              detail={"method": method, "ms": elapsed, "fields": found, "llm_error": llm_error,
                      "disagreements": disagreements})
    revalidate(invoice_id, actor="system", first_pass=True)


def _fill_due_date(fields: dict[str, Any]) -> None:
    if (fields.get("due_date") or {}).get("value"):
        return
    inv_date = (fields.get("invoice_date") or {}).get("value")
    vendor_id = (fields.get("vendor_name") or {}).get("vendor_id")
    if not inv_date or not vendor_id:
        return
    vendor = db.fetch_one("SELECT terms_days FROM vendors WHERE id = ?", (vendor_id,))
    try:
        due = date.fromisoformat(inv_date) + timedelta(days=(vendor or {}).get("terms_days") or 30)
    except ValueError:
        return
    fields["due_date"] = {"value": due.isoformat(), "conf": 0.99, "source": "computed", "bbox": None}


def _save_extraction(invoice_id: int, fields: dict[str, Any], items: list[dict[str, Any]], **extra: Any) -> None:
    def v(name: str) -> Any:
        return (fields.get(name) or {}).get("value")
    db.update_invoice(
        invoice_id, fields=fields, line_items=items,
        vendor_id=(fields.get("vendor_name") or {}).get("vendor_id"), vendor_name=v("vendor_name"),
        invoice_number=v("invoice_number"), invoice_date=v("invoice_date"), due_date=v("due_date"),
        po_number=v("po_number"), currency=v("currency"), subtotal=v("subtotal"), tax=v("tax"),
        shipping=v("shipping"), total=v("total"), **extra)


def revalidate(invoice_id: int, actor: str = "system", first_pass: bool = False) -> dict[str, Any]:
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise ActionError("Invoice not found.")
    issues = validate(invoice, invoice["fields"], invoice["line_items"], invoice["issues"])
    status = route(issues)
    if invoice["status"] == "awaiting_second_approval" and status == "ready":
        status = "awaiting_second_approval"
    duplicate_of = next((i["evidence"].get("duplicate_of") for i in issues
                         if i["id"] in {"duplicate_file", "duplicate_number"}), None)
    db.update_invoice(invoice_id, issues=issues, status=status, duplicate_of=duplicate_of)
    if first_pass or status != invoice["status"]:
        open_counts: dict[str, int] = {}
        for issue in issues:
            if issue["status"] == "open":
                open_counts[issue["severity"]] = open_counts.get(issue["severity"], 0) + 1
        label = {"ready": "Ready to approve — all checks passed",
                 "needs_review": "Sent to review",
                 "duplicate": "Held as a duplicate",
                 "awaiting_second_approval": "Awaiting second approval"}[status]
        codes = [i["id"] for i in issues if i["status"] == "open" and i["severity"] != "info"]
        audit.log("validated" if first_pass else "rechecked",
                  label + (f" ({', '.join(i['title'].lower() for i in issues if i['status'] == 'open' and i['severity'] != 'info')})" if codes else ""),
                  invoice_id=invoice_id, actor=actor,
                  detail={"status": status, "open": open_counts, "codes": codes, "first_pass": first_pass})
    return db.get_invoice(invoice_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- reviewer actions

def _require(invoice: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any]:
    if not invoice:
        raise ActionError("Invoice not found.")
    if invoice["status"] not in allowed:
        raise ActionError(f"This invoice is {invoice['status'].replace('_', ' ')} and can’t be changed here.")
    return invoice


def _coerce(name: str, value: Any) -> Any:
    if value in ("", None):
        return None
    if name in {"subtotal", "tax", "shipping", "total"}:
        return round(float(str(value).replace(",", "").replace("$", "")), 2)
    if name in {"invoice_date", "due_date"}:
        return date.fromisoformat(str(value)[:10]).isoformat()
    return str(value).strip()


def save_review(invoice_id: int, *, fields: dict[str, Any] | None = None, confirm: list[str] | None = None,
                line_items: list[dict[str, Any]] | None = None, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), REVIEW_STATES | {"awaiting_second_approval"})
    current = invoice["fields"]
    changes = []

    for name, raw in (fields or {}).items():
        if name not in KEY_FIELDS:
            continue
        try:
            new = _coerce(name, raw)
        except ValueError:
            raise ActionError(f"“{raw}” isn’t a valid value for {name.replace('_', ' ')}.")
        old = (current.get(name) or {}).get("value")
        if new == old:
            continue
        entry = {**(current.get(name) or {"bbox": None}), "value": new, "conf": 1.0, "source": "reviewer",
                 "previous": old}
        entry.pop("alternative", None)
        if name == "vendor_name":
            match = match_vendor_name(new or "")
            entry["vendor_id"] = match["id"] if match else None
            if match:
                entry["value"] = match["name"]
        current[name] = entry
        changes.append({"field": name, "from": old, "to": entry["value"]})
        audit.log("field_corrected", f"{LABELS.get(name, name)} changed from “{old}” to “{entry['value']}”",
                  invoice_id=invoice_id, actor=actor, detail={"field": name, "from": old, "to": entry["value"]})

    for name in confirm or []:
        f = current.get(name)
        if f and f.get("source") != "reviewer":
            f.update({"source": "reviewer", "conf": 1.0, "confirmed_from": f.get("source")})
            f.pop("alternative", None)
            audit.log("field_confirmed", f"{LABELS.get(name, name)} “{f['value']}” confirmed against the page",
                      invoice_id=invoice_id, actor=actor, detail={"field": name, "value": f["value"]})

    items = invoice["line_items"]
    if line_items is not None:
        cleaned = []
        for index, raw in enumerate(line_items):
            def num(value: Any) -> float:
                return float(str(value if value not in (None, "") else 0).replace(",", "").replace("$", "").strip())
            try:
                item = {"description": str(raw.get("description", "")).strip(),
                        "quantity": num(raw.get("quantity")),
                        "unit_price": round(num(raw.get("unit_price")), 2),
                        "amount": round(num(raw.get("amount")), 2)}
            except (TypeError, ValueError):
                raise ActionError(f"Line {index + 1} has a value that isn’t a number.")
            if not item["description"] and not item["amount"]:
                continue
            original = items[index] if index < len(items) else {}
            same = all(original.get(k) == item[k] for k in ("description", "quantity", "unit_price", "amount"))
            if same and not raw.get("confirmed"):
                cleaned.append(original)
            else:
                cleaned.append({**original, **item, "source": "reviewer", "conf": 1.0})
                if not same:
                    audit.log("line_item_corrected", f"Line {index + 1} updated: {item['description'][:60]} — "
                              f"{item['quantity']:g} × {item['unit_price']:,.2f} = {item['amount']:,.2f}",
                              invoice_id=invoice_id, actor=actor,
                              detail={"row": index, "from": {k: original.get(k) for k in item}, "to": item})
        if len(cleaned) != len(items):
            audit.log("line_items_changed", f"Line items now {len(cleaned)} (was {len(items)})",
                      invoice_id=invoice_id, actor=actor)
        items = cleaned

    _save_extraction(invoice_id, current, items)
    return revalidate(invoice_id, actor=actor)


def acknowledge_issue(invoice_id: int, issue_id: str, note: str, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), REVIEW_STATES | {"awaiting_second_approval"})
    issue = next((i for i in invoice["issues"] if i["id"] == issue_id), None)
    if not issue:
        raise ActionError("That check no longer applies — the invoice has been rechecked.")
    if issue["severity"] == "error":
        raise ActionError("Errors must be fixed, not acknowledged. Correct the value or reject the invoice.")
    if issue["severity"] == "block" and len(note.strip()) < 5:
        raise ActionError("Give a reason for releasing a duplicate hold (at least a few words).")
    issue["status"] = "released" if issue["severity"] == "block" else "acknowledged"
    issue["resolution"] = {"actor": actor, "note": note.strip(), "at": db.now_iso()}
    db.update_invoice(invoice_id, issues=invoice["issues"])
    verb = "Released duplicate hold" if issue["severity"] == "block" else "Acknowledged"
    audit.log("issue_acknowledged", f"{verb}: {issue['title']}" + (f" — “{note.strip()}”" if note.strip() else ""),
              invoice_id=invoice_id, actor=actor, detail={"issue": issue_id, "note": note})
    return revalidate(invoice_id, actor=actor)


def add_vendor_from_invoice(invoice_id: int, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), REVIEW_STATES)
    name = (invoice["vendor_name"] or "").strip()
    if not name:
        raise ActionError("Enter the vendor name first.")
    existing = match_vendor_name(name)
    vendor_id = existing["id"] if existing else db.execute(
        "INSERT INTO vendors(name, aliases, category, terms_days, created_at) VALUES (?, '[]', 'Uncategorized', 30, ?)",
        (name, db.now_iso()))
    fields = invoice["fields"]
    fields["vendor_name"] = {**fields.get("vendor_name", {}), "value": name, "vendor_id": vendor_id,
                             "source": "reviewer", "conf": 1.0}
    _save_extraction(invoice_id, fields, invoice["line_items"])
    audit.log("vendor_added", f"Added “{name}” to the vendor list", invoice_id=invoice_id, actor=actor,
              detail={"vendor_id": vendor_id})
    return revalidate(invoice_id, actor=actor)


def approve(invoice_id: int, actor: str, note: str = "") -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), {"ready", "awaiting_second_approval"})
    open_items = blocking(invoice["issues"])
    if open_items:
        raise ActionError(f"Resolve {len(open_items)} open check(s) before approving.")
    approvals = invoice["approvals"] or []
    if any(a["actor"].lower() == actor.lower() for a in approvals):
        raise ActionError("You already approved this invoice. A different person must give the second approval.")
    approvals.append({"actor": actor, "at": db.now_iso(), "note": note})
    needs_second = (invoice["total"] or 0) >= settings.second_approval_limit and len(approvals) < 2
    db.update_invoice(invoice_id, approvals=approvals,
                      status="awaiting_second_approval" if needs_second else "approved")
    audit.log("approved", ("First approval" if needs_second else "Approved") +
              f" for {invoice['currency'] or ''} {invoice['total']:,.2f}".replace("  ", " ") +
              (f" — second approver required above {settings.second_approval_limit:,.0f}" if needs_second else ""),
              invoice_id=invoice_id, actor=actor, detail={"note": note, "level": len(approvals)})
    if needs_second:
        return db.get_invoice(invoice_id)  # type: ignore[return-value]
    return post_to_quickbooks(invoice_id, actor)


def post_to_quickbooks(invoice_id: int, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), {"approved", "post_failed"})
    try:
        result = quickbooks.create_bill(invoice)
    except Exception as exc:
        db.update_invoice(invoice_id, status="post_failed", qbo_response={"error": str(exc)})
        audit.log("post_failed", f"QuickBooks posting failed: {exc}", invoice_id=invoice_id, actor=actor)
        return db.get_invoice(invoice_id)  # type: ignore[return-value]
    db.update_invoice(invoice_id, status="posted", qbo_bill_id=result["bill_id"], qbo_mode=result["mode"],
                      qbo_payload=result["payload"], qbo_response=result["response"], posted_at=db.now_iso())
    where = "QuickBooks sandbox" if result["mode"] == "sandbox" else "QuickBooks simulator"
    audit.log("posted", f"Bill {result['bill_id']} created in {where} (unpaid, {len(result['payload']['Line'])} lines)",
              invoice_id=invoice_id, actor=actor, detail={"bill_id": result["bill_id"], "mode": result["mode"]})
    return db.get_invoice(invoice_id)  # type: ignore[return-value]


def reject(invoice_id: int, reason: str, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), REVIEW_STATES | {"awaiting_second_approval", "failed", "post_failed"})
    if len(reason.strip()) < 3:
        raise ActionError("Give a reason so the vendor and auditors know why it was rejected.")
    db.update_invoice(invoice_id, status="rejected", rejected_reason=reason.strip())
    audit.log("rejected", f"Rejected — “{reason.strip()}”", invoice_id=invoice_id, actor=actor,
              detail={"reason": reason, "previous_status": invoice["status"]})
    return db.get_invoice(invoice_id)  # type: ignore[return-value]


def reprocess(invoice_id: int, actor: str) -> dict[str, Any]:
    invoice = _require(db.get_invoice(invoice_id), REVIEW_STATES | {"failed"})
    audit.log("reprocess", "Extraction re-run from the original file", invoice_id=invoice_id, actor=actor)
    db.update_invoice(invoice_id, issues=[], status="processing")
    process(invoice_id, actor=actor)
    return db.get_invoice(invoice_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- reporting

def dashboard_stats() -> dict[str, Any]:
    rows = db.fetch_all("SELECT id, status, total, currency, processing_ms, issues, approvals, received_at FROM invoices")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    posted = [r for r in rows if r["status"] == "posted"]

    first_pass = db.fetch_all(
        "SELECT invoice_id, detail FROM audit_events WHERE action = 'validated' ORDER BY id")
    seen: dict[int, list[str]] = {}
    for event in first_pass:
        seen.setdefault(event["invoice_id"], (event["detail"] or {}).get("codes", []))
    categories = {"Duplicates held": 0, "Math errors": 0, "Hard-to-read fields": 0, "New vendors": 0,
                  "Date problems": 0}
    straight_through = 0
    for codes in seen.values():
        if not codes:
            straight_through += 1
        if any(c.startswith("duplicate") or c == "possible_duplicate" for c in codes):
            categories["Duplicates held"] += 1
        if any(c in {"subtotal_mismatch", "total_mismatch"} or c.startswith("line_math") for c in codes):
            categories["Math errors"] += 1
        if any(c.startswith("low_conf") or c.startswith("missing") for c in codes):
            categories["Hard-to-read fields"] += 1
        if "new_vendor" in codes:
            categories["New vendors"] += 1
        if any(c in {"future_date", "due_before_invoice", "stale_invoice"} for c in codes):
            categories["Date problems"] += 1

    dup_ids = [iid for iid, codes in seen.items() if any(c in {"duplicate_file", "duplicate_number"} for c in codes)]
    prevented = sum((r["total"] or 0) for r in rows if r["id"] in dup_ids and r["status"] != "posted")
    times = [r["processing_ms"] for r in rows if r["processing_ms"]]
    return {
        "total": len(rows),
        "counts": counts,
        "to_review": counts.get("needs_review", 0) + counts.get("duplicate", 0),
        "ready": counts.get("ready", 0) + counts.get("awaiting_second_approval", 0),
        "posted": len(posted),
        "posted_amount": round(sum(r["total"] or 0 for r in posted), 2),
        "duplicate_amount_prevented": round(prevented, 2),
        "exceptions": categories,
        "straight_through": straight_through,
        "processed": len(seen),
        "avg_seconds": round(sum(times) / len(times) / 1000, 1) if times else None,
    }
