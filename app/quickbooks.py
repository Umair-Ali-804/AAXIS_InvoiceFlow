"""QuickBooks Online integration.

Two modes, same payload:
  sandbox    – real Intuit sandbox company via OAuth 2.0 (Accounting API, /bill endpoint).
  simulator  – no credentials: the exact Bill payload is built and stored locally so the
               demo runs anywhere. Switching to the sandbox is only a matter of adding keys.

Note: QuickBooks Online has no "draft" state for Bills. Approved invoices are created
as unpaid Bills (Balance = total); nothing is paid until someone schedules payment.
"""
from __future__ import annotations

import base64
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import settings

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"


class QuickBooksError(RuntimeError):
    pass


def api_base() -> str:
    host = "sandbox-quickbooks.api.intuit.com" if settings.qbo_environment == "sandbox" \
        else "quickbooks.api.intuit.com"
    return f"https://{host}/v3/company/{realm_id()}"


def realm_id() -> str:
    return db.kv_get("qbo_realm_id") or settings.qbo_realm_id


def _refresh_token() -> str:
    return db.kv_get("qbo_refresh_token") or settings.qbo_refresh_token


def mode() -> str:
    return "sandbox" if settings.qbo_configured and realm_id() and _refresh_token() else "simulator"


def status() -> dict[str, Any]:
    return {
        "mode": mode(),
        "configured": settings.qbo_configured,
        "realm_id": realm_id() or None,
        "environment": settings.qbo_environment,
        "connected_at": db.kv_get("qbo_connected_at"),
    }


# --------------------------------------------------------------------------- OAuth

def authorize_url() -> str:
    state = secrets.token_urlsafe(16)
    db.kv_set("qbo_oauth_state", state)
    query = {"client_id": settings.qbo_client_id, "response_type": "code", "scope": SCOPE,
             "redirect_uri": settings.qbo_redirect_uri, "state": state}
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def _basic_auth() -> str:
    raw = f"{settings.qbo_client_id}:{settings.qbo_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _store_tokens(payload: dict[str, Any]) -> None:
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
    db.kv_set("qbo_access_token", payload["access_token"])
    db.kv_set("qbo_access_expires", expires.isoformat())
    if payload.get("refresh_token"):
        db.kv_set("qbo_refresh_token", payload["refresh_token"])  # Intuit rotates refresh tokens


def exchange_code(code: str, state: str, realm: str) -> None:
    if state != db.kv_get("qbo_oauth_state"):
        raise QuickBooksError("OAuth state mismatch — start the connection again.")
    resp = httpx.post(TOKEN_URL, headers={"Authorization": _basic_auth(), "Accept": "application/json"},
                      data={"grant_type": "authorization_code", "code": code,
                            "redirect_uri": settings.qbo_redirect_uri}, timeout=30)
    if resp.status_code != 200:
        raise QuickBooksError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    _store_tokens(resp.json())
    db.kv_set("qbo_realm_id", realm)
    db.kv_set("qbo_connected_at", db.now_iso())


def _access_token() -> str:
    token, expires = db.kv_get("qbo_access_token"), db.kv_get("qbo_access_expires")
    if token and expires and datetime.fromisoformat(expires) > datetime.now(timezone.utc):
        return token
    refresh = _refresh_token()
    if not refresh:
        raise QuickBooksError("QuickBooks is not connected.")
    resp = httpx.post(TOKEN_URL, headers={"Authorization": _basic_auth(), "Accept": "application/json"},
                      data={"grant_type": "refresh_token", "refresh_token": refresh}, timeout=30)
    if resp.status_code != 200:
        raise QuickBooksError(f"Could not refresh the QuickBooks token ({resp.status_code}). Reconnect QuickBooks.")
    _store_tokens(resp.json())
    return db.kv_get("qbo_access_token")


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    params = {"minorversion": MINOR_VERSION, **kwargs.pop("params", {})}
    headers = {"Authorization": f"Bearer {_access_token()}", "Accept": "application/json",
               "Content-Type": "application/json"}
    resp = httpx.request(method, f"{api_base()}{path}", headers=headers, params=params, timeout=30, **kwargs)
    if resp.status_code >= 400:
        try:
            fault = resp.json().get("Fault", {}).get("Error", [{}])[0]
            message = f"{fault.get('Message')}: {fault.get('Detail')}"
        except Exception:
            message = resp.text[:300]
        raise QuickBooksError(f"QuickBooks returned {resp.status_code} — {message}")
    return resp.json()


def _query(sql: str) -> dict[str, Any]:
    return _request("GET", "/query", params={"query": sql}).get("QueryResponse", {})


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# --------------------------------------------------------------------------- Bill building

def build_bill(invoice: dict[str, Any], vendor_ref: str, expense_account: str,
               tax_account: str | None) -> dict[str, Any]:
    lines = []
    for item in invoice["line_items"]:
        lines.append({
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": round(float(item["amount"]), 2),
            "Description": f"{item['description']} ({item['quantity']:g} × {item['unit_price']:,.2f})"[:4000],
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": expense_account}},
        })
    for label, key in (("Sales tax", "tax"), ("Shipping / freight", "shipping")):
        amount = invoice.get(key) or 0
        if amount:
            lines.append({
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": round(float(amount), 2),
                "Description": label,
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": tax_account or expense_account}},
            })
    approvers = ", ".join(a["actor"] for a in invoice.get("approvals") or [])
    bill = {
        "VendorRef": {"value": vendor_ref, "name": invoice["vendor_name"]},
        "DocNumber": (invoice["invoice_number"] or "")[:21],
        "TxnDate": invoice["invoice_date"],
        "Line": lines,
        "PrivateNote": f"AAXIS InvoiceFlow {invoice['ref']} · approved by {approvers}"[:4000],
    }
    if invoice.get("due_date"):
        bill["DueDate"] = invoice["due_date"]
    if invoice.get("currency") and invoice["currency"] != settings.base_currency:
        bill["CurrencyRef"] = {"value": invoice["currency"]}
    return bill


def _vendor_ref_sandbox(invoice: dict[str, Any]) -> str:
    vendor = db.fetch_one("SELECT * FROM vendors WHERE id = ?", (invoice["vendor_id"],)) if invoice.get("vendor_id") else None
    name = (vendor or {}).get("name") or invoice["vendor_name"]
    found = _query(f"select Id, DisplayName from Vendor where DisplayName = '{_escape(name)}'").get("Vendor", [])
    if found:
        ref = found[0]["Id"]
    else:
        ref = _request("POST", "/vendor", json={"DisplayName": name[:100]})["Vendor"]["Id"]
    if vendor:
        db.execute("UPDATE vendors SET qbo_vendor_id = ? WHERE id = ?", (ref, vendor["id"]))
    return ref


def _expense_account_sandbox() -> str:
    if settings.qbo_expense_account_id:
        return settings.qbo_expense_account_id
    cached = db.kv_get("qbo_expense_account_id")
    if cached:
        return cached
    accounts = _query("select Id, Name from Account where AccountType = 'Expense' maxresults 1").get("Account", [])
    if not accounts:
        raise QuickBooksError("No expense account found in the QuickBooks company. Set QBO_EXPENSE_ACCOUNT_ID.")
    db.kv_set("qbo_expense_account_id", accounts[0]["Id"])
    return accounts[0]["Id"]


def create_bill(invoice: dict[str, Any]) -> dict[str, Any]:
    """Create the Bill. Returns {mode, bill_id, payload, response}. Safe to retry."""
    if mode() == "simulator":
        vendor_ref = f"SIM-V{invoice.get('vendor_id') or 0:03d}"
        payload = build_bill(invoice, vendor_ref, "SIM-6100 Repairs & Maintenance", "SIM-2200 Sales Tax Payable")
        existing = db.fetch_one("SELECT id FROM qbo_simulator_bills WHERE invoice_id = ?", (invoice["id"],))
        sim_id = existing["id"] if existing else db.execute(
            "INSERT INTO qbo_simulator_bills(invoice_id, payload, created_at) VALUES (?, ?, ?)",
            (invoice["id"], db.dumps(payload), db.now_iso()))
        bill_id = f"SIM-{1000 + sim_id}"
        response = {"Bill": {**payload, "Id": bill_id, "SyncToken": "0", "Balance": invoice["total"],
                             "TotalAmt": invoice["total"],
                             "MetaData": {"CreateTime": db.now_iso()}},
                    "time": db.now_iso(), "_simulated": True}
        return {"mode": "simulator", "bill_id": bill_id, "payload": payload, "response": response}

    # Idempotency: if this vendor already has a Bill with this DocNumber, reuse it.
    vendor_ref = _vendor_ref_sandbox(invoice)
    doc_number = _escape((invoice["invoice_number"] or "")[:21])
    existing = _query(f"select * from Bill where DocNumber = '{doc_number}'").get("Bill", [])
    existing = [b for b in existing if b.get("VendorRef", {}).get("value") == vendor_ref]
    payload = build_bill(invoice, vendor_ref, _expense_account_sandbox(), settings.qbo_tax_account_id or None)
    if existing:
        return {"mode": "sandbox", "bill_id": existing[0]["Id"], "payload": payload,
                "response": {"Bill": existing[0], "_reused_existing": True}}
    response = _request("POST", "/bill", json=payload,
                        params={"requestid": f"aaxis-{invoice['id']}-{invoice['file_hash'][:12]}"})
    return {"mode": "sandbox", "bill_id": response["Bill"]["Id"], "payload": payload, "response": response}
