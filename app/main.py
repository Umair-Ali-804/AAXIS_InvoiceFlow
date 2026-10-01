"""AAXIS InvoiceFlow — FastAPI application (pages + JSON API)."""
from __future__ import annotations

import base64
import csv
import io
import logging
import secrets
import shutil
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audit, db, emailin, pipeline, quickbooks
from .config import settings
from .documents import UnsupportedDocument
from .validate import money

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent

log = logging.getLogger("invoiceflow")


def _auto_load_demo() -> None:
    """Free hosts wipe the disk on restart; refill an empty database with the demo cases."""
    try:
        if db.fetch_one("SELECT COUNT(*) AS n FROM invoices")["n"]:
            return
        import runpy
        ids = runpy.run_path(str(ROOT / "scripts" / "run_demo.py"))["load_demo"](actor="Demo loader")
        log.warning("AUTO_LOAD_DEMO: loaded %d demo invoices", len(ids))
    except Exception:  # never stop the app because of the demo
        log.exception("AUTO_LOAD_DEMO failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    pipeline.seed_vendors()
    if settings.auto_load_demo:
        threading.Thread(target=_auto_load_demo, daemon=True, name="demo-loader").start()
    yield


app = FastAPI(title="AAXIS InvoiceFlow", version="1.0.0", lifespan=lifespan,
              description="Invoice capture, validation and human review, posting to QuickBooks Online.")
settings.ensure_dirs()


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """Optional site-wide password (ACCESS_PASSWORD). The browser shows its own sign-in box; any username works."""
    if not settings.access_password or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            password = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[1]
        except (ValueError, IndexError, UnicodeDecodeError):
            password = ""
        if secrets.compare_digest(password.encode(), settings.access_password.encode()):
            return await call_next(request)
    return JSONResponse({"error": "Password required"}, status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="AAXIS InvoiceFlow", charset="UTF-8"'})


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.mount("/pages", StaticFiles(directory=settings.pages_dir), name="pages")
templates = Jinja2Templates(directory=APP_DIR / "templates")

STATUS = {
    "processing": ("Processing", "neutral"),
    "needs_review": ("Needs review", "warn"),
    "duplicate": ("Held · duplicate", "danger"),
    "ready": ("Ready to approve", "ok"),
    "awaiting_second_approval": ("Needs 2nd approval", "info"),
    "approved": ("Approved", "ok"),
    "posted": ("Posted to QuickBooks", "posted"),
    "post_failed": ("Posting failed", "danger"),
    "rejected": ("Rejected", "muted"),
    "failed": ("Couldn’t read", "danger"),
}
QUEUES = [
    ("review", "To review", ("needs_review", "duplicate", "failed", "post_failed")),
    ("ready", "Ready to approve", ("ready", "awaiting_second_approval")),
    ("posted", "Posted", ("posted",)),
    ("rejected", "Rejected", ("rejected",)),
    ("all", "All", ()),
]


# --------------------------------------------------------------------------- template helpers

def _fmt_date(value: Any, fmt: str = "%b %d, %Y") -> str:
    if not value:
        return "—"
    try:
        return date.fromisoformat(str(value)[:10]).strftime(fmt)
    except ValueError:
        return str(value)


def _ago(value: Any) -> str:
    if not value:
        return ""
    try:
        then = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return then.strftime("%b %d")


def _clock(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%b %d, %H:%M:%S")
    except ValueError:
        return str(value)


templates.env.filters.update(money=lambda v, c="USD": money(v, c or "USD"), date=_fmt_date, ago=_ago, clock=_clock)
templates.env.globals.update(STATUS=STATUS, settings=settings)


def _actor(request: Request) -> str:
    return (request.headers.get("x-actor") or "AP Reviewer").strip()[:60] or "AP Reviewer"


def _render(request: Request, name: str, **context: Any) -> HTMLResponse:
    counts = {r["status"]: r["n"] for r in db.fetch_all("SELECT status, COUNT(*) AS n FROM invoices GROUP BY status")}
    nav_review = sum(counts.get(s, 0) for s in QUEUES[0][2])
    nav_ready = sum(counts.get(s, 0) for s in QUEUES[1][2])
    return templates.TemplateResponse(request, name, {"nav_review": nav_review, "nav_ready": nav_ready,
                                                      "qbo": quickbooks.status(), **context})


@app.exception_handler(pipeline.ActionError)
async def action_error(_: Request, exc: pipeline.ActionError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=409)


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = pipeline.dashboard_stats()
    recent = db.fetch_all("SELECT * FROM invoices ORDER BY id DESC LIMIT 6")
    activity = audit.recent(9)
    return _render(request, "dashboard.html", page="dashboard", stats=stats, recent=recent, activity=activity)


@app.get("/invoices", response_class=HTMLResponse)
def inbox(request: Request, queue: str = "review", q: str = ""):
    queue_def = next((x for x in QUEUES if x[0] == queue), QUEUES[0])
    sql, params = "SELECT * FROM invoices", []
    where = []
    if queue_def[2]:
        where.append(f"status IN ({','.join('?' * len(queue_def[2]))})")
        params.extend(queue_def[2])
    if q.strip():
        where.append("(vendor_name LIKE ? OR invoice_number LIKE ? OR ref LIKE ? OR file_name LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 4)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    rows = db.fetch_all(sql, params)
    counts = {}
    for key, _, statuses in QUEUES:
        if statuses:
            counts[key] = db.fetch_one(
                f"SELECT COUNT(*) AS n FROM invoices WHERE status IN ({','.join('?' * len(statuses))})",
                list(statuses))["n"]
        else:
            counts[key] = db.fetch_one("SELECT COUNT(*) AS n FROM invoices")["n"]
    for row in rows:
        row["open_issues"] = [i for i in row["issues"] if i["status"] == "open" and i["severity"] != "info"]
    return _render(request, "inbox.html", page="invoices", rows=rows, queue=queue_def[0], queues=QUEUES,
                   counts=counts, q=q)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def review(request: Request, invoice_id: int):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    events = audit.events_for(invoice_id)
    neighbours = db.fetch_all(
        "SELECT id, ref FROM invoices WHERE status IN ('needs_review','duplicate','ready','awaiting_second_approval') "
        "ORDER BY id")
    ids = [n["id"] for n in neighbours]
    next_id = next((i for i in ids if i > invoice_id), None)
    duplicate = db.get_invoice(invoice["duplicate_of"]) if invoice.get("duplicate_of") else None
    return _render(request, "review.html", page="invoices", inv=invoice, events=events, next_id=next_id,
                   duplicate=duplicate, public=_public(invoice))


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    return _render(request, "audit.html", page="audit", events=audit.recent(400), chain=audit.verify_chain())


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, qbo_error: str = ""):
    return _render(request, "settings.html", page="settings", vendors=pipeline.vendors(), qbo_error=qbo_error,
                   imap=settings.imap_configured)


# --------------------------------------------------------------------------- QuickBooks OAuth

@app.get("/integrations/quickbooks/connect")
def qbo_connect():
    if not settings.qbo_configured:
        return RedirectResponse("/settings?qbo_error=Add+QBO_CLIENT_ID+and+QBO_CLIENT_SECRET+to+.env+first.", 303)
    return RedirectResponse(quickbooks.authorize_url(), 303)


@app.get("/integrations/quickbooks/callback")
def qbo_callback(code: str = "", state: str = "", realmId: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse(f"/settings?qbo_error={error or 'Connection+cancelled'}", 303)
    try:
        quickbooks.exchange_code(code, state, realmId)
    except quickbooks.QuickBooksError as exc:
        return RedirectResponse(f"/settings?qbo_error={exc}", 303)
    audit.log("quickbooks_connected", f"QuickBooks company {realmId} connected", actor="admin")
    return RedirectResponse("/settings", 303)


# --------------------------------------------------------------------------- API

def _public(invoice: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in invoice.items() if k not in {"words", "raw_text", "file_path"}}
    out["pages"] = (invoice["fields"].get("_pages") or {}).get("value", [])
    out["fields"] = {k: v for k, v in invoice["fields"].items() if not k.startswith("_")}
    out["status_label"], out["status_tone"] = STATUS.get(invoice["status"], (invoice["status"], "neutral"))
    out["second_approval_limit"] = settings.second_approval_limit
    out["confidence_threshold"] = settings.confidence_threshold
    out["events"] = audit.events_for(invoice["id"])
    out["qbo_mode"] = invoice.get("qbo_mode") or quickbooks.mode()
    return out


@app.post("/api/invoices/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    actor = _actor(request)
    created, errors = [], []
    for upload_file in files:
        content = await upload_file.read()
        name = upload_file.filename or "document"
        try:
            if name.lower().endswith(".eml"):
                message = emailin.parse_eml(content)
                if not message["attachments"]:
                    errors.append(f"{name}: the email has no PDF or image attachment.")
                for att in message["attachments"]:
                    created.append(pipeline.ingest(att["content"], att["filename"], source="email",
                                                   source_detail=f"{message['from']} — {message['subject']}", actor=actor))
            else:
                created.append(pipeline.ingest(content, name, source="upload", actor=actor))
        except UnsupportedDocument as exc:
            errors.append(f"{name}: {exc}")
    return {"created": created, "errors": errors}


@app.post("/api/demo/load")
def demo_load(request: Request):
    import runpy
    module = runpy.run_path(str(ROOT / "scripts" / "run_demo.py"))
    ids = module["load_demo"](actor=_actor(request))
    return {"created": ids}


@app.post("/api/demo/reset")
def demo_reset(request: Request):
    for folder in (settings.uploads_dir, settings.pages_dir):
        shutil.rmtree(folder, ignore_errors=True)
    settings.ensure_dirs()
    with db.connect() as conn:
        for table in ("invoices", "audit_events", "qbo_simulator_bills"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','audit_events','qbo_simulator_bills')")
    audit.log("workspace_reset", "Demo workspace cleared", actor=_actor(request))
    return {"ok": True}


@app.post("/api/email/poll")
def email_poll(request: Request):
    try:
        messages = emailin.poll_inbox()
    except Exception as exc:
        raise HTTPException(400, str(exc))
    created = []
    for message in messages:
        for att in message["attachments"]:
            created.append(pipeline.ingest(att["content"], att["filename"], source="email",
                                           source_detail=f"{message['from']} — {message['subject']}",
                                           actor=_actor(request)))
    return {"messages": len(messages), "created": created}


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return _public(invoice)


@app.post("/api/invoices/{invoice_id}/save")
def save(request: Request, invoice_id: int, payload: dict = Body(...)):
    invoice = pipeline.save_review(invoice_id, fields=payload.get("fields"), confirm=payload.get("confirm"),
                                   line_items=payload.get("line_items"), actor=_actor(request))
    return _public(invoice)


@app.post("/api/invoices/{invoice_id}/issues/{issue_id}/acknowledge")
def acknowledge(request: Request, invoice_id: int, issue_id: str, payload: dict = Body(default={})):
    return _public(pipeline.acknowledge_issue(invoice_id, issue_id, payload.get("note", ""), _actor(request)))


@app.post("/api/invoices/{invoice_id}/vendor")
def add_vendor(request: Request, invoice_id: int):
    return _public(pipeline.add_vendor_from_invoice(invoice_id, _actor(request)))


@app.post("/api/invoices/{invoice_id}/approve")
def approve(request: Request, invoice_id: int, payload: dict = Body(default={})):
    return _public(pipeline.approve(invoice_id, _actor(request), payload.get("note", "")))


@app.post("/api/invoices/{invoice_id}/retry-post")
def retry_post(request: Request, invoice_id: int):
    return _public(pipeline.post_to_quickbooks(invoice_id, _actor(request)))


@app.post("/api/invoices/{invoice_id}/reject")
def reject(request: Request, invoice_id: int, payload: dict = Body(...)):
    return _public(pipeline.reject(invoice_id, payload.get("reason", ""), _actor(request)))


@app.post("/api/invoices/{invoice_id}/reprocess")
def reprocess(request: Request, invoice_id: int):
    return _public(pipeline.reprocess(invoice_id, _actor(request)))


@app.get("/api/invoices/{invoice_id}/quickbooks-preview")
def quickbooks_preview(invoice_id: int):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    if invoice.get("qbo_payload"):
        return {"posted": True, "mode": invoice["qbo_mode"], "payload": invoice["qbo_payload"]}
    vendor_ref = "(looked up or created in QuickBooks)" if quickbooks.mode() == "sandbox" \
        else f"SIM-V{invoice.get('vendor_id') or 0:03d}"
    payload = quickbooks.build_bill(invoice, vendor_ref, "(expense account)", None)
    return {"posted": False, "mode": quickbooks.mode(), "payload": payload}


@app.get("/files/{invoice_id}/original")
def original(invoice_id: int):
    invoice = db.get_invoice(invoice_id)
    if not invoice or not Path(invoice["file_path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(invoice["file_path"], filename=invoice["file_name"])


@app.get("/api/audit/verify")
def audit_verify():
    return audit.verify_chain()


@app.get("/api/audit/export.csv")
def audit_export():
    rows = db.fetch_all(
        "SELECT a.id, a.ts, a.actor, a.action, i.ref AS invoice, a.summary, a.prev_hash, a.hash "
        "FROM audit_events a LEFT JOIN invoices i ON i.id = a.invoice_id ORDER BY a.id")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id", "ts", "actor", "action", "invoice", "summary", "prev_hash", "hash"])
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=invoiceflow_audit_trail.csv"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "quickbooks": quickbooks.mode(), "llm": settings.llm_enabled}
