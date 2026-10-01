"""Optional LLM cross-check (Anthropic or OpenAI).

The rules engine always runs. When an API key is configured, a model reads the
same OCR text independently; agreeing values gain confidence, disagreements are
sent to a reviewer instead of being silently resolved.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import settings
from .extract import normalize_invoice_number, normalize_vendor

PROMPT = """You are an accounts-payable data extraction engine. Read the invoice text below
(it may contain OCR errors) and return ONLY a JSON object with these keys:

vendor_name (string, the company that ISSUED the invoice, not the bill-to customer),
invoice_number (string), invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD or null),
po_number (string or null), currency (ISO code), subtotal (number), tax (number or 0),
shipping (number or 0), total (number),
line_items (array of {{description, quantity, unit_price, amount}}).

Copy numbers exactly as printed; do not correct arithmetic. Use null when a value is absent.

INVOICE TEXT:
<<<
{text}
>>>"""

DEFAULT_MODELS = {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-4o-mini"}


def _call(text: str) -> dict[str, Any]:
    model = settings.llm_model or DEFAULT_MODELS.get(settings.llm_provider, "")
    prompt = PROMPT.format(text=text[:12000])
    with httpx.Client(timeout=60) as client:
        if settings.llm_provider == "anthropic":
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": 2000, "temperature": 0,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            content = "".join(block.get("text", "") for block in resp.json().get("content", []))
        else:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    return json.loads(match.group(0) if match else content)


def _same(name: str, a: Any, b: Any) -> bool:
    if a in (None, "") or b in (None, ""):
        return False
    if name == "vendor_name":
        return normalize_vendor(str(a)) == normalize_vendor(str(b)) or \
            normalize_vendor(str(a)) in normalize_vendor(str(b)) or normalize_vendor(str(b)) in normalize_vendor(str(a))
    if name in {"invoice_number", "po_number"}:
        return normalize_invoice_number(str(a)) == normalize_invoice_number(str(b))
    if name in {"subtotal", "tax", "shipping", "total"}:
        try:
            return abs(float(a) - float(b)) < 0.01
        except (TypeError, ValueError):
            return False
    return str(a).strip().lower() == str(b).strip().lower()


def cross_check(result: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """Merge model output into rule output. Returns (result, disagreements, error)."""
    if not settings.llm_enabled:
        return result, [], None
    try:
        model = _call(result.get("text", ""))
    except Exception as exc:  # network/API errors must never block the pipeline
        return result, [], f"{type(exc).__name__}: {exc}"

    fields = result["fields"]
    disagreements = []
    for name in ("vendor_name", "invoice_number", "invoice_date", "due_date", "po_number",
                 "subtotal", "tax", "shipping", "total"):
        llm_value = model.get(name)
        current = fields.get(name)
        if current is None:
            if llm_value not in (None, "", 0):
                fields[name] = {"value": llm_value, "conf": 0.8, "source": "llm", "bbox": None}
            continue
        if llm_value in (None, ""):
            continue
        if _same(name, current["value"], llm_value):
            current["conf"] = max(current["conf"], 0.97)
            current["source"] = f"{current['source']}+llm"
        else:
            disagreements.append({"field": name, "rules": current["value"], "model": llm_value})
            current["conf"] = min(current["conf"], 0.6)
            current["alternative"] = llm_value

    if not result["line_items"] and model.get("line_items"):
        for item in model["line_items"]:
            try:
                result["line_items"].append({
                    "description": str(item.get("description", "")),
                    "quantity": float(item.get("quantity") or 1),
                    "unit_price": float(item.get("unit_price") or item.get("amount") or 0),
                    "amount": float(item.get("amount") or 0),
                    "conf": 0.8, "source": "llm", "bbox": None,
                })
            except (TypeError, ValueError):
                continue
    return result, disagreements, None
