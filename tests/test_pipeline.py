"""End-to-end tests for AAXIS InvoiceFlow.  Run:  python -m pytest -q"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="invoiceflow-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["LLM_PROVIDER"] = ""          # tests never call an external model
os.environ["QBO_CLIENT_ID"] = ""         # always use the simulator

from app import audit, db, pipeline  # noqa: E402
from app.config import settings  # noqa: E402
from app.extract import normalize_invoice_number, parse_date, parse_money  # noqa: E402

settings.data_dir = Path(_TMP)
settings.qbo_client_id = ""
settings.llm_provider = ""
DEMO = ROOT / "demo" / "invoices"


@pytest.fixture(scope="module")
def loaded():
    if not DEMO.exists() or not any(DEMO.iterdir()):
        import runpy
        runpy.run_path(str(ROOT / "demo" / "generate_invoices.py"), run_name="__main__")
    shutil.rmtree(_TMP, ignore_errors=True)
    settings.ensure_dirs()
    db.init_db()
    pipeline.seed_vendors()
    import runpy
    ids = runpy.run_path(str(ROOT / "scripts" / "run_demo.py"))["load_demo"](actor="test")
    return {db.get_invoice(i)["file_name"]: i for i in ids}


def by_prefix(loaded, prefix):
    name = next(n for n in loaded if n.startswith(prefix))
    return db.get_invoice(loaded[name])


def open_ids(inv):
    return {i["id"] for i in inv["issues"] if i["status"] == "open" and i["severity"] != "info"}


# ---------------------------------------------------------------- parsers

@pytest.mark.parametrize("raw,expected", [
    ("$1,234.56", 1234.56), ("1.234,56", 1234.56), ("S756.00", 756.0), ("42:00", 42.0),
    ("1;789.90", 1789.90), ("“440.00", 440.0), ("756.00.", 756.0), ("(12.50)", -12.5), ("abc", None),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_date_and_numbers():
    assert parse_date("Sep 14, 2026") == "2026-09-14"
    assert parse_date("09/17/2026") == "2026-09-17"
    assert parse_date("2026-10-06") == "2026-10-06"
    assert normalize_invoice_number("INV-024817") == normalize_invoice_number("24817")


# ---------------------------------------------------------------- the four demo cases (+ bonus)

def test_clean_invoice_is_ready(loaded):
    inv = by_prefix(loaded, "01_")
    assert inv["status"] == "ready"
    assert inv["vendor_name"] == "Summit Office Supply Co."
    assert inv["invoice_number"] == "INV-24817"
    assert inv["invoice_date"] == "2026-09-14" and inv["due_date"] == "2026-10-14"
    assert (inv["subtotal"], inv["tax"], inv["total"]) == (3190.38, 263.21, 3453.59)
    assert len(inv["line_items"]) == 5
    assert inv["extraction_method"] == "text-layer"


def test_poor_scan_goes_to_review_with_correct_header(loaded):
    inv = by_prefix(loaded, "02_")
    assert inv["extraction_method"].startswith("ocr")
    assert inv["status"] == "needs_review"
    assert inv["fields"]["_pages"]["value"][0]["deskew"] != 0      # page was straightened
    assert inv["vendor_name"] == "Evergreen Facilities Maintenance"
    assert inv["invoice_number"] == "EFM-260917"
    assert inv["total"] == 1906.24
    assert any(i.startswith("low_conf") for i in open_ids(inv))     # a person must look at it


def test_duplicate_from_email_is_held(loaded):
    name = next(n for n in loaded if "second_notice" in n)
    inv = db.get_invoice(loaded[name])
    assert inv["source"] == "email"
    assert inv["status"] == "duplicate"
    assert "duplicate_number" in open_ids(inv)
    assert inv["line_items"][1]["description"] == "Toner cartridge, high-yield black"   # stamp text ignored


def test_math_mismatch_is_caught(loaded):
    inv = by_prefix(loaded, "04_")
    assert inv["status"] == "needs_review"
    issue = next(i for i in inv["issues"] if i["id"] == "subtotal_mismatch")
    assert issue["evidence"]["lines_sum"] == 7340.00
    assert issue["evidence"]["printed"] == 7430.00


def test_text_layer_keeps_short_words(loaded):
    inv = by_prefix(loaded, "05_")
    assert inv["line_items"][0]["description"] == "Linehaul Seattle to Tacoma, FTL 53ft"


# ---------------------------------------------------------------- reviewer workflow

def test_errors_block_approval_until_fixed(loaded):
    inv = by_prefix(loaded, "04_")
    with pytest.raises(pipeline.ActionError):
        pipeline.approve(inv["id"], "Maya Chen")
    with pytest.raises(pipeline.ActionError):   # errors can't be waved through
        pipeline.acknowledge_issue(inv["id"], "subtotal_mismatch", "looks fine", "Maya Chen")


def test_scan_can_be_corrected_and_posted(loaded):
    inv = by_prefix(loaded, "02_")
    fixed = [{"description": "Quarterly HVAC filter replacement", "quantity": 18, "unit_price": 42, "amount": 756},
             {"description": "Plumbing repair, unit 3B leak", "quantity": 3.5, "unit_price": 95, "amount": "332.50"},
             {"description": "Parking lot light fixture repair", "quantity": 4, "unit_price": 110, "amount": 440},
             {"description": "Emergency call-out fee", "quantity": 1, "unit_price": 175, "amount": 175},
             {"description": "Materials: fittings and sealant", "quantity": 1, "unit_price": "86.40", "amount": "86.40"}]
    for item in fixed:
        item["confirmed"] = True
    confirm = [k for k in ("invoice_number", "invoice_date", "due_date", "po_number", "vendor_name", "subtotal", "tax", "total")]
    after = pipeline.save_review(inv["id"], line_items=fixed, confirm=confirm, actor="Maya Chen")
    assert after["status"] == "ready", open_ids(after)
    posted = pipeline.approve(inv["id"], "Maya Chen")
    assert posted["status"] == "posted" and posted["qbo_bill_id"].startswith("SIM-")
    bill_total = round(sum(line["Amount"] for line in posted["qbo_payload"]["Line"]), 2)
    assert bill_total == posted["total"]                               # bill balances to the invoice


def test_high_value_needs_two_different_people(loaded):
    inv = by_prefix(loaded, "05_")
    first = pipeline.approve(inv["id"], "Maya Chen")
    assert first["status"] == "awaiting_second_approval"
    with pytest.raises(pipeline.ActionError):
        pipeline.approve(inv["id"], "maya chen")
    second = pipeline.approve(inv["id"], "Daniel Ortiz")
    assert second["status"] == "posted"
    assert [a["actor"] for a in second["approvals"]] == ["Maya Chen", "Daniel Ortiz"]


def test_release_duplicate_requires_reason(loaded):
    name = next(n for n in loaded if "second_notice" in n)
    invoice_id = loaded[name]
    with pytest.raises(pipeline.ActionError):
        pipeline.acknowledge_issue(invoice_id, "duplicate_number", "", "Maya Chen")
    rejected = pipeline.reject(invoice_id, "Duplicate of AP-1001", "Maya Chen")
    assert rejected["status"] == "rejected"


def test_audit_chain_detects_tampering(loaded):
    assert audit.verify_chain()["ok"]
    with db.connect() as conn:
        conn.execute("UPDATE audit_events SET summary = 'nothing to see here' WHERE id = 3")
    result = audit.verify_chain()
    assert not result["ok"] and result["broken_at"] == 3


# ---------------------------------------------------------------- hosting options

def test_password_gate(loaded):
    from fastapi.testclient import TestClient
    from app import main
    settings.access_password = "s3cret-pass"
    try:
        client = TestClient(main.app)
        assert client.get("/healthz").status_code == 200              # health checks stay open
        r = client.get("/api/audit/verify")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert client.get("/api/audit/verify", auth=("anyone", "wrong")).status_code == 401
        assert client.get("/api/audit/verify", auth=("anyone", "s3cret-pass")).status_code == 200
    finally:
        settings.access_password = ""
    assert TestClient(main.app).get("/api/audit/verify").status_code == 200  # off by default


def test_auto_load_demo_only_fills_an_empty_database(loaded):
    from app import main
    before = db.fetch_one("SELECT COUNT(*) AS n FROM invoices")["n"]
    main._auto_load_demo()
    assert db.fetch_one("SELECT COUNT(*) AS n FROM invoices")["n"] == before
