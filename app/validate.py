"""Business-rule validation and routing.

Severity levels
  block    – stops the invoice (duplicates). A reviewer can release it with a reason.
  error    – must be corrected before approval (math, missing or impossible values).
  warning  – must be confirmed by a person (low-confidence reads, new vendor, possible duplicate).
  info     – shown for context, never blocks.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import db
from .config import settings
from .extract import normalize_invoice_number, normalize_vendor

REQUIRED = {"vendor_name": "Vendor", "invoice_number": "Invoice number",
            "invoice_date": "Invoice date", "total": "Total"}
CONFIDENCE_CHECKED = {"vendor_name": "Vendor", "invoice_number": "Invoice number", "invoice_date": "Invoice date",
                      "due_date": "Due date", "po_number": "PO number", "subtotal": "Subtotal",
                      "tax": "Tax", "shipping": "Shipping", "total": "Total"}


def money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "PKR": "Rs "}.get(currency, "")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def _v(fields: dict[str, Any], name: str) -> Any:
    f = fields.get(name) or {}
    return f.get("value")


def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _issue(id_: str, severity: str, title: str, message: str, field: str | None = None,
           **evidence: Any) -> dict[str, Any]:
    return {"id": id_, "severity": severity, "title": title, "message": message, "field": field,
            "status": "open", "evidence": evidence}


def find_duplicates(invoice: dict[str, Any], fields: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    active = "status NOT IN ('rejected', 'failed') AND id != ?"
    same_file = db.fetch_one(
        f"SELECT id, ref, status FROM invoices WHERE file_hash = ? AND {active} ORDER BY id LIMIT 1",
        (invoice["file_hash"], invoice["id"]))
    if same_file:
        issues.append(_issue("duplicate_file", "block", "Same file received before",
                             f"This exact file was already received as {same_file['ref']}.",
                             None, duplicate_of=same_file["id"], duplicate_ref=same_file["ref"]))

    vendor_id = (fields.get("vendor_name") or {}).get("vendor_id")
    vendor_norm = normalize_vendor(_v(fields, "vendor_name"))
    number_norm = normalize_invoice_number(_v(fields, "invoice_number"))
    if number_norm and (vendor_id or vendor_norm):
        rows = db.fetch_all(
            f"SELECT id, ref, status, vendor_id, vendor_name, invoice_number, total, invoice_date "
            f"FROM invoices WHERE {active} AND invoice_number IS NOT NULL", (invoice["id"],))
        for row in rows:
            same_vendor = (vendor_id and row["vendor_id"] == vendor_id) or \
                (vendor_norm and normalize_vendor(row["vendor_name"]) == vendor_norm)
            if same_vendor and normalize_invoice_number(row["invoice_number"]) == number_norm:
                if not any(i["id"] == "duplicate_file" for i in issues):
                    issues.append(_issue(
                        "duplicate_number", "block", "Duplicate invoice number",
                        f"{row['vendor_name']} invoice {row['invoice_number']} was already received as "
                        f"{row['ref']}. Paying it again would be a double payment.",
                        "invoice_number", duplicate_of=row["id"], duplicate_ref=row["ref"]))
                break

    total, inv_date = _v(fields, "total"), _parse_iso(_v(fields, "invoice_date"))
    if total and inv_date and (vendor_id or vendor_norm) and not issues:
        window = settings.duplicate_window_days
        rows = db.fetch_all(
            f"SELECT id, ref, vendor_id, vendor_name, invoice_number, total, invoice_date FROM invoices "
            f"WHERE {active} AND total IS NOT NULL", (invoice["id"],))
        for row in rows:
            same_vendor = (vendor_id and row["vendor_id"] == vendor_id) or \
                (vendor_norm and normalize_vendor(row["vendor_name"]) == vendor_norm)
            other_date = _parse_iso(row["invoice_date"])
            if same_vendor and abs((row["total"] or 0) - total) < 0.01 and other_date and \
                    abs((other_date - inv_date).days) <= window:
                issues.append(_issue(
                    "possible_duplicate", "warning", "Possible duplicate",
                    f"Same vendor and same amount as {row['ref']} (invoice {row['invoice_number']}, "
                    f"{row['invoice_date']}) within {window} days, but a different invoice number.",
                    "total", duplicate_of=row["id"], duplicate_ref=row["ref"]))
                break
    return issues


def _suggest_decimal_fix(item: dict[str, Any], tol: float, other_lines: float | None = None,
                         subtotal: float | None = None) -> dict[str, Any] | None:
    """Suggest the single most likely dropped/misplaced decimal point on a line.

    Candidates that make qty × price = amount are ranked by how close they bring the
    line total to the printed subtotal, then by how often each column is misread
    (unit price first, quantity next, amount last)."""
    qty, unit, amount = item.get("quantity") or 0, item.get("unit_price") or 0, item.get("amount") or 0
    preference = {"unit_price": 0, "quantity": 1, "amount": 2}
    fits = []
    for factor in (0.01, 0.1, 10, 100):
        for name, q, u, a in (("unit_price", qty, unit * factor, amount),
                              ("quantity", qty * factor, unit, amount),
                              ("amount", qty, unit, amount * factor)):
            if q and u and abs(q * u - a) <= tol:
                gap = abs(other_lines + a - subtotal) if other_lines is not None and subtotal is not None else 0.0
                value = {"unit_price": u, "quantity": q, "amount": a}[name]
                fits.append((round(gap, 2), preference[name], {"field": name, "value": round(value, 2)}))
    if not fits:
        return None
    fits.sort(key=lambda f: (f[0], f[1]))
    return fits[0][2]


def validate(invoice: dict[str, Any], fields: dict[str, Any], items: list[dict[str, Any]],
             previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    tol = settings.math_tolerance
    cur = _v(fields, "currency") or settings.base_currency
    issues: list[dict[str, Any]] = []

    # 1. Required values
    for name, label in REQUIRED.items():
        if _v(fields, name) in (None, ""):
            issues.append(_issue(f"missing_{name}", "error", f"{label} not found",
                                 f"No {label.lower()} could be read from the document. Enter it from the page.", name))

    # 2. Confidence of each read
    threshold = settings.confidence_threshold
    for name, label in CONFIDENCE_CHECKED.items():
        f = fields.get(name)
        if not f or f.get("value") in (None, "") or f.get("source") == "reviewer":
            continue
        if f.get("conf", 1) < threshold:
            detail = f"Read as “{f['value']}” with {round(f['conf'] * 100)}% confidence."
            if f.get("alternative") not in (None, ""):
                detail += f" The AI cross-check read “{f['alternative']}”."
            issues.append(_issue(f"low_conf_{name}", "warning", f"Check {label.lower()}", detail + " Confirm or correct it.",
                                 name, conf=f["conf"]))

    weak_lines = [i for i, item in enumerate(items) if item.get("source") != "reviewer"
                  and item.get("conf", 1) < threshold]
    if weak_lines:
        issues.append(_issue("low_conf_lines", "warning", "Check line items",
                             f"{len(weak_lines)} line item(s) were hard to read. Compare them with the page.",
                             "line_items", rows=weak_lines))

    # 3. Arithmetic
    printed_subtotal = _v(fields, "subtotal")
    all_lines = sum((i.get("amount") or 0) for i in items)
    for index, item in enumerate(items):
        expected = round((item.get("quantity") or 0) * (item.get("unit_price") or 0), 2)
        if abs(expected - (item.get("amount") or 0)) > max(tol, 0.005 * abs(expected)):
            suggestion = _suggest_decimal_fix(item, tol, all_lines - (item.get("amount") or 0), printed_subtotal)
            message = (f"{item.get('quantity'):g} × {money(item.get('unit_price'), cur)} = {money(expected, cur)}, "
                       f"but the line shows {money(item.get('amount'), cur)}.")
            if suggestion:
                label = suggestion["field"].replace("_", " ")
                message += f" Looks like a dropped decimal point — {label} {suggestion['value']:,.2f}?"
            issues.append(_issue(
                f"line_math_{index}", "error", f"Line {index + 1} doesn’t multiply out", message,
                "line_items", row=index, expected=expected, suggestion=suggestion))

    subtotal, tax = _v(fields, "subtotal"), _v(fields, "tax") or 0.0
    shipping, total = _v(fields, "shipping") or 0.0, _v(fields, "total")
    lines_sum = round(sum((i.get("amount") or 0) for i in items), 2)
    if items and subtotal is not None and abs(lines_sum - subtotal) > tol:
        diff = round(subtotal - lines_sum, 2)
        issues.append(_issue(
            "subtotal_mismatch", "error", "Line items don’t add up to the subtotal",
            f"Line items add up to {money(lines_sum, cur)}, but the invoice shows a subtotal of "
            f"{money(subtotal, cur)} — a difference of {money(abs(diff), cur)}. "
            f"Ask the vendor for a corrected invoice or fix the line that was misread.",
            "subtotal", lines_sum=lines_sum, printed=subtotal, difference=diff))
    base = subtotal if subtotal is not None else (lines_sum if items else None)
    if base is not None and total is not None:
        expected_total = round(base + tax + shipping, 2)
        if abs(expected_total - total) > tol:
            issues.append(_issue(
                "total_mismatch", "error", "Total doesn’t match",
                f"Subtotal {money(base, cur)} + tax {money(tax, cur)}"
                + (f" + shipping {money(shipping, cur)}" if shipping else "")
                + f" = {money(expected_total, cur)}, but the invoice total is {money(total, cur)}.",
                "total", expected=expected_total, printed=total))
    if subtotal and tax and subtotal > 0 and tax / subtotal > 0.20:
        issues.append(_issue("tax_rate", "warning", "Unusually high tax",
                             f"Tax is {tax / subtotal:.1%} of the subtotal. Check the rate before approving.", "tax"))
    if total is not None and total <= 0:
        issues.append(_issue("non_positive_total", "error", "Total is zero or negative",
                             "Credit notes are handled separately. Check the document type.", "total"))

    # 4. Dates
    today = date.today()
    inv_date, due = _parse_iso(_v(fields, "invoice_date")), _parse_iso(_v(fields, "due_date"))
    if inv_date and inv_date > today + timedelta(days=1):
        issues.append(_issue("future_date", "error", "Invoice date is in the future",
                             f"{inv_date:%b %d, %Y} hasn’t happened yet — likely a misread date.", "invoice_date"))
    if inv_date and due and due < inv_date:
        issues.append(_issue("due_before_invoice", "error", "Due date is before invoice date",
                             f"Due {due:%b %d, %Y} is earlier than the invoice date {inv_date:%b %d, %Y}.", "due_date"))
    if inv_date and (today - inv_date).days > 180:
        issues.append(_issue("stale_invoice", "warning", "Invoice is over 180 days old",
                             "Old invoices are a common duplicate-payment source. Confirm it hasn’t been paid.",
                             "invoice_date"))
    if (fields.get("due_date") or {}).get("source") == "computed":
        issues.append(_issue("due_computed", "info", "Due date calculated",
                             "No due date on the invoice; calculated from the vendor’s payment terms.", "due_date"))

    # 5. Vendor master
    vendor = fields.get("vendor_name") or {}
    if vendor.get("value") and not vendor.get("vendor_id"):
        issues.append(_issue("new_vendor", "warning", "Vendor not in your vendor list",
                             f"“{vendor['value']}” isn’t an approved vendor. Verify who they are before paying — "
                             "fake vendors are a common invoice-fraud route.", "vendor_name"))

    if cur != settings.base_currency:
        issues.append(_issue("foreign_currency", "info", f"Billed in {cur}",
                             f"Your books are in {settings.base_currency}; QuickBooks will apply the exchange rate.",
                             "currency"))

    # 6. Duplicates
    issues.extend(find_duplicates(invoice, fields))

    # Carry forward reviewer decisions for issues that still apply.
    decided = {i["id"]: i for i in (previous or []) if i.get("status") in {"acknowledged", "released"}}
    for issue in issues:
        prior = decided.get(issue["id"])
        if prior and issue["severity"] in {"warning", "block"}:
            issue["status"] = prior["status"]
            issue["resolution"] = prior.get("resolution")
    return issues


def route(issues: list[dict[str, Any]]) -> str:
    open_issues = [i for i in issues if i["status"] == "open"]
    if any(i["severity"] == "block" for i in open_issues):
        return "duplicate"
    if any(i["severity"] in {"error", "warning"} for i in open_issues):
        return "needs_review"
    return "ready"


def blocking(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i["status"] == "open" and i["severity"] in {"block", "error", "warning"}]
