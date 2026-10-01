"""Rule-based invoice field extraction over positioned words.

Works offline for any reasonably standard invoice layout (label: value pairs,
a line-item table and a totals block). Each field carries a confidence built
from the rule that found it and the OCR confidence of the words it came from,
plus a bounding box so reviewers can see the evidence on the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from dateutil import parser as dateparser
from rapidfuzz import fuzz

from .layout import Line, bbox_of, build_lines, conf_of

# --------------------------------------------------------------------------- value parsers

_OCR_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "B": "8"})
_MONEY_TOKEN = re.compile(r"^\(?-?(?:USD|US\$|\$|S|€|£|Rs\.?)?\s?[\dOolI|B][\dOolI|B,.' ]*[.,][\dOo]{2}\)?$")
_NUMBER_TOKEN = re.compile(r"^\d+(?:\.\d+)?$")
_ID_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9\-/.]{2,24}$", re.I)

_DATE_PATTERNS = [
    r"\b\d{4}-\d{1,2}-\d{1,2}\b",
    r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b",
]
_DATE_RE = re.compile("|".join(_DATE_PATTERNS), re.I)


def clean_token(token: str) -> str:
    """Strip OCR punctuation noise around a value: “440.00  756.00.  |175.00"""
    token = token.strip().strip("“”\"'‘’`|;,~_*°")
    token = token.rstrip(".:")
    return token.lstrip(".:")


def parse_money(token: str) -> float | None:
    raw = clean_token(token).replace(" ", "")
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-")
    raw = raw.strip("()-")
    raw = re.sub(r"^(USD|US\$|\$|€|£|Rs\.?)", "", raw, flags=re.I)
    if raw[:1] == "S" and len(raw) > 1 and raw[1].isdigit():   # '$' read as 'S'
        raw = raw[1:]
    raw = raw.translate(_OCR_DIGIT_FIXES).replace("'", "").replace(";", ",")
    raw = re.sub(r":(\d{2})$", r".\1", raw)                      # 42:00 → 42.00
    if not re.fullmatch(r"[\d,.]+", raw) or not re.search(r"\d", raw):
        return None
    match = re.search(r"[.,](\d{2})$", raw)
    if match:
        whole = re.sub(r"[.,]", "", raw[: match.start()])
        value = float(f"{whole or '0'}.{match.group(1)}")
    else:
        value = float(re.sub(r"[.,]", "", raw))
    return -value if negative else value


def is_money_token(token: str) -> bool:
    cleaned = re.sub(r":(\d{2})$", r".\1", clean_token(token)).replace(";", ",")
    return bool(_MONEY_TOKEN.match(cleaned)) and parse_money(token) is not None


def parse_number(token: str) -> float | None:
    cleaned = clean_token(token).replace(",", "")
    if _NUMBER_TOKEN.match(cleaned):
        return float(cleaned)
    return parse_money(token) if is_money_token(token) else None


def is_noise(token: str, conf: float = 0.0) -> bool:
    """OCR debris between table columns: '-', '|', '‘', or a low-confidence 'a' / 'mY'.
    Real short words ('to', 'of') read with confidence — or from a PDF text layer — are kept."""
    stripped = token.strip()
    if not any(ch.isalnum() for ch in stripped):
        return True
    return len(stripped) <= 2 and stripped.isalpha() and conf < 0.6


def parse_date(text: str) -> str | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        parsed = dateparser.parse(match.group(0), dayfirst=False, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if not parsed or not (2000 <= parsed.year <= 2100):
        return None
    return parsed.date().isoformat()


def normalize_invoice_number(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    return re.sub(r"^(INV|INVOICE|NO)", "", cleaned).lstrip("0")


def normalize_vendor(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", value.lower())
    cleaned = re.sub(r"\b(llc|inc|ltd|co|corp|corporation|company|the|limited|pvt)\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# --------------------------------------------------------------------------- label rules

@dataclass
class Rule:
    field: str
    pattern: re.Pattern
    kind: str            # money | date | id | text
    priority: int = 0    # higher wins
    exclude: re.Pattern | None = None


RULES: list[Rule] = [
    Rule("invoice_number", re.compile(r"\binvoice\s*(?:no\.?|number|num\.?|#|id)\s*[:#.]?|\binv\.?\s*(?:#|no\.?)\s*:?", re.I), "id", 3),
    Rule("invoice_number", re.compile(r"\bbill\s*(?:no\.?|number|#)\s*:?|\breference\s*(?:no\.?|#)?\s*:?", re.I), "id", 1),
    Rule("invoice_date", re.compile(r"\binvoice\s*date\s*:?|\bdate\s*of\s*issue\s*:?|\bissue(?:d)?\s*date\s*:?|\bbill\s*date\s*:?", re.I), "date", 3),
    Rule("invoice_date", re.compile(r"(?<!due )(?<!ship )\bdate\s*:", re.I), "date", 1,
         exclude=re.compile(r"due|ship|service|delivery|order", re.I)),
    Rule("due_date", re.compile(r"\bdue\s*date\s*:?|\bpayment\s*due\s*:?|\bdate\s*due\s*:?|\bpay\s*by\s*:?", re.I), "date", 3),
    Rule("po_number", re.compile(r"\bp\.?\s?o\.?\s*(?:number|no\.?|#)?\s*:|\bp\.?o\.?\s*#|\bpurchase\s*order\s*(?:no\.?|#|number)?\s*:?", re.I), "id", 2),
    Rule("subtotal", re.compile(r"\bsub\s*-?\s*total\b", re.I), "money", 3),
    Rule("tax", re.compile(r"\b(?:sales\s+)?tax\b|\bvat\b|\bgst\b|\bhst\b", re.I), "money", 2,
         exclude=re.compile(r"tax\s*(?:id|no|number|exempt|registration)|\bein\b|total|incl", re.I)),
    Rule("shipping", re.compile(r"^\s*(?:shipping|freight|delivery|handling)(?:\s*(?:&|and)\s*handling)?\b", re.I), "money", 2),
    Rule("total", re.compile(r"\b(?:total\s*(?:amount\s*)?due|amount\s*due|balance\s*due|invoice\s*total|grand\s*total|total\s*payable)\b", re.I), "money", 4),
    Rule("total", re.compile(r"^\s*total\b", re.I), "money", 2,
         exclude=re.compile(r"sub\s*-?\s*total|total\s*tax|total\s*hours|total\s*qty", re.I)),
]

KEY_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number",
              "currency", "subtotal", "tax", "shipping", "total"]


def _word_offsets(line: Line, words: Sequence[Any]) -> list[tuple[int, int, int]]:
    offsets, pos = [], 0
    for wid in line.word_ids:
        text = words[wid].text
        offsets.append((pos, pos + len(text), wid))
        pos += len(text) + 1
    return offsets


def _words_between(offsets, start: int, end: int | None = None) -> list[int]:
    return [wid for s, e, wid in offsets if s >= start and (end is None or e <= end)]


def _field(value: Any, conf: float, source: str, words: Sequence[Any], ids: Sequence[int]) -> dict[str, Any]:
    return {"value": value, "conf": round(max(0.0, min(conf, 1.0)), 3), "source": source,
            "bbox": bbox_of(words, ids)}


def _value_after(rule: Rule, line: Line, label_end: int, words, offsets) -> tuple[Any, list[int]] | None:
    after_ids = _words_between(offsets, label_end)
    if not after_ids:
        return None
    if rule.kind == "money":
        for wid in reversed(after_ids):
            if is_money_token(words[wid].text):
                return parse_money(words[wid].text), [wid]
        return None
    if rule.kind == "date":
        text_after = " ".join(words[w].text for w in after_ids)
        match = _DATE_RE.search(text_after)
        if not match:
            return None
        value = parse_date(match.group(0))
        if not value:
            return None
        # map matched substring back to word ids
        consumed, ids = 0, []
        for wid in after_ids:
            start = consumed
            consumed += len(words[wid].text) + 1
            if start < match.end() and consumed > match.start():
                ids.append(wid)
        return value, ids
    if rule.kind == "id":
        for wid in after_ids[:5]:
            token = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", words[wid].text)
            if token.lower() in {"no", "number", "#", ":", ""} or is_noise(words[wid].text, words[wid].conf):
                continue
            if _ID_TOKEN.match(token) and re.search(r"\d", token):
                return token.upper(), [wid]
            break
        return None
    return None


def _value_below(rule: Rule, lines: list[Line], index: int, label_ids: list[int], words) -> tuple[Any, list[int]] | None:
    """Label printed above its value (common in header boxes). Looks at the words
    directly under the label's horizontal span on the next two lines."""
    line = lines[index]
    lx0 = min(words[i].x0 for i in label_ids)
    lx1 = max(words[i].x1 for i in label_ids)
    checked = 0
    for nxt in lines[index + 1: index + 6]:
        if nxt.page != line.page or nxt.y0 - line.y1 > 0.05 or checked >= 2:
            break
        if all(is_noise(words[w].text, words[w].conf) or len(words[w].text) <= 2 for w in nxt.word_ids):
            continue   # speckle-only line between label and value
        checked += 1
        under = [wid for wid in nxt.word_ids
                 if lx0 - 0.03 <= (words[wid].x0 + words[wid].x1) / 2 <= lx1 + 0.09
                 and not is_noise(words[wid].text, words[wid].conf)]
        if not under:
            continue
        text = " ".join(words[w].text for w in under)
        fake = Line(page=nxt.page, word_ids=under, y0=nxt.y0, y1=nxt.y1, text=text)
        found = _value_after(rule, fake, 0, words, _word_offsets(fake, words))
        if found:
            return found
    return None


# --------------------------------------------------------------------------- line items

_HEADER_RE = re.compile(r"description|item|service|details", re.I)
_QTY_RE = re.compile(r"\bqty\b|quantity|hours|hrs|units", re.I)
_AMOUNT_HDR_RE = re.compile(r"amount|total|line\s*total|price", re.I)
_END_RE = re.compile(r"sub\s*-?\s*total|^\s*total\b|total\s*due|amount\s*due|balance\s*due", re.I)


def _extract_line_items(lines: list[Line], words, source: str) -> tuple[list[dict[str, Any]], set[int]]:
    items: list[dict[str, Any]] = []
    used: set[int] = set()
    header_index = next(
        (i for i, ln in enumerate(lines)
         if _HEADER_RE.search(ln.text) and _QTY_RE.search(ln.text) and _AMOUNT_HDR_RE.search(ln.text)),
        None,
    )
    if header_index is None:
        return items, used
    used.add(header_index)
    for i in range(header_index + 1, len(lines)):
        line = lines[i]
        if _END_RE.search(line.text):
            break
        ids = [w for w in line.word_ids
               if not (is_noise(words[w].text, words[w].conf) and parse_number(words[w].text) is None)]
        numbers: list[tuple[float, int, bool]] = []
        for wid in reversed(ids):
            token = words[wid].text
            value = parse_number(token)
            if value is None:
                break
            numbers.append((value, wid, is_money_token(token)))
        numbers.reverse()
        desc_ids = [w for w in ids if w not in {n[1] for n in numbers}]
        description = " ".join(words[w].text for w in desc_ids).strip(" -|")

        if len(numbers) >= 3:
            qty, unit, amount = numbers[-3][0], numbers[-2][0], numbers[-1][0]
            num_ids = [n[1] for n in numbers[-3:]]
            # anything numeric before qty belongs to the description (e.g. a SKU)
            extra = [n[1] for n in numbers[:-3]]
            if extra:
                description = " ".join(words[w].text for w in sorted(desc_ids + extra, key=ids.index))
        elif len(numbers) == 2:
            if numbers[0][2]:
                qty, unit, amount = 1.0, numbers[0][0], numbers[1][0]
            else:
                qty, amount = numbers[0][0], numbers[1][0]
                unit = round(amount / qty, 2) if qty else amount
            num_ids = [n[1] for n in numbers]
        elif not numbers and items and description:
            # wrapped description continues the previous item
            items[-1]["description"] = f"{items[-1]['description']} {description}".strip()
            used.add(i)
            continue
        else:
            continue

        if not description:
            continue
        used.add(i)
        conf = conf_of(words, num_ids + desc_ids) * 0.97
        items.append({
            "description": description,
            "quantity": qty,
            "unit_price": unit,
            "amount": amount,
            "conf": round(conf, 3),
            "source": source,
            "bbox": bbox_of(words, ids),
        })
    return items, used


# --------------------------------------------------------------------------- vendor

def _match_vendor(lines: list[Line], words, vendors: list[dict[str, Any]], source: str):
    top_lines = [ln for ln in lines if ln.page == 0 and ln.y0 < 0.35] or lines[:12]
    best: tuple[float, dict[str, Any] | None, Line | None] = (0.0, None, None)
    for vendor in vendors:
        names = [vendor["name"], *(vendor.get("aliases") or [])]
        for name in names:
            target = normalize_vendor(name)
            if len(target) < 4:
                continue
            for ln in top_lines:
                candidate = normalize_vendor(ln.text)
                if len(candidate) < 0.7 * len(target):
                    continue   # partial_ratio would let a 1-letter OCR fragment "match" anything
                score = fuzz.partial_ratio(target, candidate)
                if score > best[0]:
                    best = (score, vendor, ln)
    score, vendor, line = best
    if vendor and line and score >= 86:
        conf = (score / 100) * conf_of(words, line.word_ids)
        field = _field(vendor["name"], conf, source, words, line.word_ids)
        field["vendor_id"] = vendor["id"]
        field["match_score"] = round(score, 1)
        field["read_as"] = line.text
        return field

    # Unknown vendor: take the most prominent line near the top that looks like a name.
    candidates = []
    for ln in top_lines[:10]:
        text = ln.text.strip()
        if len(text) < 4 or re.search(r"invoice|bill to|ship to|page \d|www\.|@|^\d|phone|tel", text, re.I):
            continue
        if parse_date(text) or sum(ch.isdigit() for ch in text) > 3:
            continue
        height = ln.y1 - ln.y0
        candidates.append((height, ln))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    ln = candidates[0][1]
    text = ln.cells[0].text if ln.cells else ln.text
    ids = ln.cells[0].word_ids if ln.cells else ln.word_ids
    return _field(text.strip(" ,"), 0.7 * conf_of(words, ids), source, words, ids)


# --------------------------------------------------------------------------- main entry

def extract(words: Sequence[Any], vendors: list[dict[str, Any]], method: str,
            math_tolerance: float = 0.02) -> dict[str, Any]:
    source = "text-layer" if method == "text-layer" else "ocr"
    lines = build_lines(words)
    line_items, item_lines = _extract_line_items(lines, words, source)

    candidates: dict[str, list[tuple[int, float, dict[str, Any]]]] = {}
    for index, line in enumerate(lines):
        if index in item_lines:
            continue
        offsets = _word_offsets(line, words)
        for rule in RULES:
            match = rule.pattern.search(line.text)
            if not match:
                continue
            if rule.exclude and rule.exclude.search(line.text):
                continue
            label_ids = [wid for s, e, wid in offsets if s < match.end() and e > match.start()]
            found = _value_after(rule, line, match.end(), words, offsets)
            rule_conf = 0.97
            if not found and rule.kind in {"id", "date"}:
                found = _value_below(rule, lines, index, label_ids, words)
                rule_conf = 0.93
            if not found:
                continue
            value, ids = found
            conf = rule_conf * (1.0 if rule.priority >= 2 else 0.92) * conf_of(words, ids)
            fld = _field(value, conf, source, words, ids)
            candidates.setdefault(rule.field, []).append((rule.priority, line.y0 + line.page, fld))

    fields: dict[str, Any] = {}
    for name, found in candidates.items():
        # highest priority wins; for money fields prefer the lowest on the page (totals block)
        found.sort(key=lambda c: (c[0], c[1] if name in {"total", "subtotal", "tax"} else -c[1]), reverse=True)
        fields[name] = found[0][2]

    vendor = _match_vendor(lines, words, vendors, source)
    if vendor:
        fields["vendor_name"] = vendor

    all_text = " ".join(w.text for w in words)
    votes = {"USD": all_text.count("$") + 3 * all_text.count("USD"),
             "EUR": all_text.count("€") + 3 * all_text.count("EUR"),
             "GBP": all_text.count("£") + 3 * all_text.count("GBP"),
             "PKR": 3 * all_text.count("PKR"), "CAD": 3 * all_text.count("CAD"), "AUD": 3 * all_text.count("AUD")}
    currency, count = max(votes.items(), key=lambda kv: kv[1])
    if count == 0:
        currency = "USD"
    total_votes = sum(votes.values()) or 1
    fields["currency"] = {"value": currency, "conf": round(0.8 + 0.18 * count / total_votes, 3),
                          "source": source, "bbox": None}

    _reconcile_confidence(fields, line_items, math_tolerance)
    return {"fields": fields, "line_items": line_items, "text": "\n".join(ln.text for ln in lines)}


def _val(fields: dict[str, Any], name: str) -> Any:
    f = fields.get(name)
    return f.get("value") if f else None


def _reconcile_confidence(fields: dict[str, Any], items: list[dict[str, Any]], tol: float) -> None:
    """If the numbers add up, the reading is corroborated: raise amount confidence."""
    subtotal, total = _val(fields, "subtotal"), _val(fields, "total")
    tax, shipping = _val(fields, "tax") or 0.0, _val(fields, "shipping") or 0.0
    if subtotal is None or total is None:
        return
    items_ok = bool(items) and abs(sum(i["amount"] for i in items) - subtotal) <= tol and all(
        abs(i["quantity"] * i["unit_price"] - i["amount"]) <= max(tol, 0.01 * i["quantity"]) for i in items)
    totals_ok = abs(subtotal + tax + shipping - total) <= tol
    if totals_ok:
        for name in ("subtotal", "tax", "shipping", "total"):
            if name in fields:
                fields[name]["conf"] = max(fields[name]["conf"], 0.96)
                fields[name]["corroborated"] = True
    if items_ok and totals_ok:
        for item in items:
            item["conf"] = max(item["conf"], 0.95)


def today() -> date:
    return date.today()
