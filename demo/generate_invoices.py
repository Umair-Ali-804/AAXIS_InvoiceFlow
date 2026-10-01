"""Generate the demo invoices for AAXIS InvoiceFlow.

Scenario: accounts payable at "Harbor & Pine Property Management", a residential
property manager that receives invoices from office, maintenance, electrical and
freight vendors. All vendor names, addresses and domains are fictional.

Cases
  01 clean        Summit Office Supply   – text PDF, everything reconciles     → Ready to approve
  02 poor scan    Evergreen Facilities   – skewed, noisy scanned PDF (no text)  → Needs review (low-confidence reads)
  03 duplicate    Summit Office Supply   – same invoice re-sent as a "second notice" by email (.eml) → Held as duplicate
  04 mismatch     BrightLine Electrical  – lines add to 7,340.00, subtotal printed 7,430.00        → Needs review (math error)
  05 high value   Cascade Freight        – clean but over the two-approver limit                   → Needs 2 approvals

Run:  python demo/generate_invoices.py
"""
from __future__ import annotations

import io
import random
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

OUT = Path(__file__).resolve().parent / "invoices"
W, H = LETTER

BILL_TO = ["Harbor & Pine Property Management", "Attn: Accounts Payable", "1200 Harbor Blvd, Suite 400",
           "Tacoma, WA 98402"]


def money(v: float) -> str:
    return f"{v:,.2f}"


# --------------------------------------------------------------------------- layouts

def layout_classic(c: canvas.Canvas, inv: dict, accent=colors.HexColor("#1F3A5F"), stamp: str | None = None) -> None:
    """Vendor block left, label: value details right, boxed table, totals right."""
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(0.75 * inch, H - 0.95 * inch, inv["vendor"])
    c.setFillColor(colors.HexColor("#333333"))
    c.setFont("Helvetica", 9)
    for i, line in enumerate(inv["vendor_address"]):
        c.drawString(0.75 * inch, H - 1.2 * inch - i * 12, line)

    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(W - 0.75 * inch, H - 0.95 * inch, "INVOICE")
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.black)
    details = [("Invoice #:", inv["number"]), ("Invoice Date:", inv["date_str"]),
               ("Due Date:", inv["due_str"]), ("PO Number:", inv["po"]), ("Terms:", inv["terms"])]
    for i, (label, value) in enumerate(details):
        y = H - 1.3 * inch - i * 15
        c.setFont("Helvetica-Bold", 10)
        c.drawString(W - 3.3 * inch, y, label)
        c.setFont("Helvetica", 10)
        c.drawRightString(W - 0.75 * inch, y, value)

    y0 = H - 2.75 * inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(0.75 * inch, y0, "Bill To")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(BILL_TO):
        c.drawString(0.75 * inch, y0 - 14 - i * 13, line)

    _table(c, inv, top=H - 3.85 * inch, header_bg=accent,
           headers=("Description", "Qty", "Unit Price", "Amount"))
    _totals(c, inv, accent)
    _footer(c, inv)
    if stamp:
        c.saveState()
        c.translate(W / 2, H / 2 + 40)
        c.rotate(18)
        c.setStrokeColor(colors.HexColor("#B42318"))
        c.setFillColor(colors.HexColor("#B42318"))
        c.setLineWidth(2.5)
        c.roundRect(-190, -26, 380, 60, 8, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 22)
        c.drawCentredString(0, 10, stamp)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(0, -14, "PAYMENT REMINDER — PLEASE REMIT")
        c.restoreState()


def layout_boxed(c: canvas.Canvas, inv: dict) -> None:
    """Coloured header band; header fields as label-over-value boxes."""
    band = colors.HexColor("#2E5E3E")
    c.setFillColor(band)
    c.rect(0, H - 1.35 * inch, W, 1.35 * inch, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(0.75 * inch, H - 0.75 * inch, inv["vendor"])
    c.setFont("Helvetica", 9)
    c.drawString(0.75 * inch, H - 0.98 * inch, "  ·  ".join(inv["vendor_address"][:2]))
    c.drawString(0.75 * inch, H - 1.15 * inch, "  ·  ".join(inv["vendor_address"][2:]))
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 0.75 * inch, H - 0.85 * inch, "Invoice")

    c.setFillColor(colors.black)
    boxes = [("Invoice No.", inv["number"]), ("Invoice Date", inv["date_str"]),
             ("Due Date", inv["due_str"]), ("PO #", inv["po"])]
    bw = (W - 1.5 * inch) / 4
    for i, (label, value) in enumerate(boxes):
        x = 0.75 * inch + i * bw
        c.setStrokeColor(colors.HexColor("#9DB5A5"))
        c.rect(x + 2, H - 2.25 * inch, bw - 4, 0.62 * inch, stroke=1, fill=0)
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#4A5A50"))
        c.drawString(x + 10, H - 1.83 * inch, label)
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(colors.black)
        c.drawString(x + 10, H - 2.08 * inch, value)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(0.75 * inch, H - 2.65 * inch, "Customer")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(BILL_TO):
        c.drawString(0.75 * inch, H - 2.85 * inch - i * 13, line)
    c.setFont("Helvetica", 9)
    c.drawString(W - 3.4 * inch, H - 2.85 * inch, "Service location: Harbor Point Apartments")
    c.drawString(W - 3.4 * inch, H - 3.03 * inch, "Work order: WO-58213 / WO-58240")

    _table(c, inv, top=H - 3.75 * inch, header_bg=band, headers=("Service Description", "Hours/Qty", "Rate", "Amount"))
    _totals(c, inv, band, total_label="Amount Due")
    _footer(c, inv)


def layout_modern(c: canvas.Canvas, inv: dict) -> None:
    """Minimal layout; invoice details block left under the logo line."""
    accent = colors.HexColor("#C2410C")
    c.setFillColor(accent)
    c.rect(0.75 * inch, H - 0.9 * inch, 0.12 * inch, 0.42 * inch, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#111827"))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(1.0 * inch, H - 0.82 * inch, inv["vendor"])
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#4B5563"))
    c.drawString(1.0 * inch, H - 1.02 * inch, " | ".join(inv["vendor_address"]))

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.75 * inch, H - 1.55 * inch, "Tax Invoice")
    c.setFont("Helvetica", 10)
    rows = [("Invoice Number:", inv["number"]), ("Date:", inv["date_str"]),
            ("Payment Due:", inv["due_str"]), ("Purchase Order:", inv["po"])]
    for i, (label, value) in enumerate(rows):
        y = H - 1.8 * inch - i * 14
        c.drawString(0.75 * inch, y, label)
        c.drawString(2.1 * inch, y, value)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(W - 3.2 * inch, H - 1.55 * inch, "Billed To")
    c.setFont("Helvetica", 10)
    for i, line in enumerate(BILL_TO):
        c.drawString(W - 3.2 * inch, H - 1.75 * inch - i * 13, line)

    _table(c, inv, top=H - 3.1 * inch, header_bg=colors.HexColor("#111827"),
           headers=("Description", "Qty", "Price", "Total"))
    _totals(c, inv, accent, total_label="Total Due")
    _footer(c, inv)


def _table(c, inv, top, header_bg, headers):
    left, right = 0.75 * inch, W - 0.75 * inch
    cols = [left + 8, right - 3.1 * inch, right - 1.75 * inch, right - 8]
    c.setFillColor(header_bg)
    c.rect(left, top - 6, right - left, 22, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(cols[0], top + 2, headers[0])
    c.drawRightString(cols[1] + 0.35 * inch, top + 2, headers[1])
    c.drawRightString(cols[2] + 0.35 * inch, top + 2, headers[2])
    c.drawRightString(cols[3], top + 2, headers[3])
    c.setFillColor(colors.black)
    y = top - 26
    for i, (desc, qty, price, amount) in enumerate(inv["lines"]):
        if i % 2 == 1:
            c.setFillColor(colors.HexColor("#F3F4F6"))
            c.rect(left, y - 5, right - left, 19, stroke=0, fill=1)
            c.setFillColor(colors.black)
        c.setFont("Helvetica", 10)
        c.drawString(cols[0], y, desc)
        c.drawRightString(cols[1] + 0.35 * inch, y, f"{qty:g}")
        c.drawRightString(cols[2] + 0.35 * inch, y, money(price))
        c.drawRightString(cols[3], y, money(amount))
        y -= 19
    c.setStrokeColor(colors.HexColor("#D1D5DB"))
    c.line(left, y + 6, right, y + 6)
    inv["_table_bottom"] = y


def _totals(c, inv, accent, total_label="Total Due"):
    right = W - 0.75 * inch
    y = inv["_table_bottom"] - 22
    rows = [("Subtotal", inv["subtotal_printed"])]
    rows.append((inv.get("tax_label", "Sales Tax"), inv["tax"]))
    if inv.get("shipping"):
        rows.append(("Shipping", inv["shipping"]))
    for label, value in rows:
        c.setFont("Helvetica", 10)
        c.drawString(right - 2.6 * inch, y, label)
        c.drawRightString(right - 8, y, money(value))
        y -= 16
    y -= 10
    c.setFillColor(accent)
    c.rect(right - 2.75 * inch, y - 8, 2.75 * inch, 24, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(right - 2.6 * inch, y, total_label)
    c.drawRightString(right - 8, y, "$" + money(inv["total"]))
    c.setFillColor(colors.black)


def _footer(c, inv):
    c.setFont("Helvetica", 8.5)
    c.setFillColor(colors.HexColor("#4B5563"))
    c.drawString(0.75 * inch, 1.15 * inch, "Payment: ACH to account ending 4417 or check payable to " + inv["vendor"] + ".")
    c.drawString(0.75 * inch, 0.98 * inch, f"Questions about this invoice? {inv['email']}")
    c.drawString(0.75 * inch, 0.81 * inch, "Thank you for your business.")


def render(inv: dict, layout, **kwargs) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(f"{inv['vendor']} invoice {inv['number']}")
    c.setAuthor(inv["vendor"])
    layout(c, inv, **kwargs)
    c.showPage()
    c.save()
    return buf.getvalue()


def degrade_to_scan(pdf_bytes: bytes, seed: int = 7) -> bytes:
    """Rasterise at low resolution and simulate an office scanner: skew, blur, noise, toner, JPEG."""
    random.seed(seed)
    page = fitz.open(stream=pdf_bytes, filetype="pdf")[0]
    pix = page.get_pixmap(dpi=150)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")

    # uneven toner / lighting
    shade = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(shade)
    for _ in range(4):
        x, y = random.randint(0, img.width), random.randint(0, img.height)
        r = random.randint(220, 420)
        d.ellipse((x - r, y - r, x + r, y + r), fill=random.randint(14, 26))
    shade = shade.filter(ImageFilter.GaussianBlur(90))
    img = Image.eval(img, lambda p: p)
    img = Image.composite(Image.new("L", img.size, 0), img, shade.point(lambda v: v))

    # a coffee ring near the header
    ring = Image.new("L", img.size, 0)
    rd = ImageDraw.Draw(ring)
    rd.ellipse((img.width - 520, 170, img.width - 250, 440), outline=70, width=16)
    ring = ring.filter(ImageFilter.GaussianBlur(6))
    img = Image.composite(Image.new("L", img.size, 150), img, ring)

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = img.rotate(-1.6, resample=Image.BICUBIC, expand=True, fillcolor=236)
    # speckle noise
    px = img.load()
    for _ in range(int(img.width * img.height * 0.007)):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice((35, 60, 210))
    img = img.point(lambda p: int(40 + p * 0.78))  # grey paper, faded ink

    jpeg = io.BytesIO()
    img.save(jpeg, "JPEG", quality=45)
    out = fitz.open()
    pg = out.new_page(width=W, height=H)
    pg.insert_image(pg.rect, stream=jpeg.getvalue())
    return out.tobytes(deflate=True)


def build_eml(pdf_bytes: bytes, filename: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Summit Office Supply Billing <billing@summitofficesupply.example>"
    msg["To"] = "ap@harborandpine.example"
    msg["Subject"] = "Reminder: Invoice INV-24817 is due — second notice"
    msg["Date"] = format_datetime(datetime(2026, 9, 26, 9, 12, tzinfo=timezone.utc))
    msg["Message-ID"] = "<reminder-inv-24817@summitofficesupply.example>"
    msg.set_content(
        "Hello Harbor & Pine AP team,\n\n"
        "This is a friendly reminder that invoice INV-24817 for $3,453.59 is due on October 14, 2026.\n"
        "A copy is attached for your records. Please disregard if payment has already been sent.\n\n"
        "Thank you,\nSummit Office Supply Co. — Billing\n")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    return bytes(msg)


# --------------------------------------------------------------------------- data

def invoice(vendor, address, email, number, date_str, due_str, po, terms, lines, tax_rate,
            subtotal_override=None, tax_label="Sales Tax", shipping=0.0):
    subtotal = round(sum(l[3] for l in lines), 2)
    printed = subtotal_override if subtotal_override is not None else subtotal
    tax = round(printed * tax_rate, 2)
    return {"vendor": vendor, "vendor_address": address, "email": email, "number": number,
            "date_str": date_str, "due_str": due_str, "po": po, "terms": terms, "lines": lines,
            "subtotal_printed": printed, "tax": tax, "shipping": shipping,
            "tax_label": f"{tax_label} ({tax_rate * 100:g}%)" if tax_rate else tax_label,
            "total": round(printed + tax + shipping, 2)}


SUMMIT = invoice(
    "Summit Office Supply Co.", ["4410 Industrial Way", "Kent, WA 98032", "(253) 555-0142"],
    "billing@summitofficesupply.example", "INV-24817", "Sep 14, 2026", "Oct 14, 2026", "HP-4471", "Net 30",
    [("Copy paper, letter, 10-ream case", 12, 54.99, 659.88),
     ("Toner cartridge, high-yield black", 6, 189.50, 1137.00),
     ("Ergonomic mesh task chair", 4, 289.00, 1156.00),
     ("Dry-erase markers, 12-pack", 10, 14.25, 142.50),
     ("Delivery and assembly", 1, 95.00, 95.00)],
    0.0825)

EVERGREEN = invoice(
    "Evergreen Facilities Maintenance", ["88 Cedar Ridge Rd", "Puyallup, WA 98371", "(253) 555-0199",
                                         "ar@evergreenfm.example"],
    "ar@evergreenfm.example", "EFM-260917", "09/17/2026", "10/17/2026", "HP-4502", "Net 30",
    [("Quarterly HVAC filter replacement", 18, 42.00, 756.00),
     ("Plumbing repair, unit 3B leak", 3.5, 95.00, 332.50),
     ("Parking lot light fixture repair", 4, 110.00, 440.00),
     ("Emergency call-out fee", 1, 175.00, 175.00),
     ("Materials: fittings and sealant", 1, 86.40, 86.40)],
    0.065)

BRIGHTLINE = invoice(
    "BrightLine Electrical Services LLC", ["301 Voltage Ave", "Tacoma, WA 98409", "Lic. BRIGHLE812KQ"],
    "invoices@brightline-elec.example", "BLE-10382", "Sep 21, 2026", "Oct 6, 2026", "HP-4519", "Net 15",
    [("Main panel upgrade 200A, Building C", 1, 4850.00, 4850.00),
     ("Labor, licensed electrician", 24, 85.00, 2040.00),
     ("Conduit and wire, materials", 1, 312.00, 312.00),
     ("Permit and inspection fee", 1, 138.00, 138.00)],
    0.08, subtotal_override=7430.00)  # true sum is 7,340.00 — digits transposed

CASCADE = invoice(
    "Cascade Freight Partners", ["2200 Port Access Rd", "Seattle, WA 98134", "(206) 555-0177"],
    "billing@cascadefreight.example", "CFP-88412", "Sep 24, 2026", "Oct 24, 2026", "HP-4533", "Net 30",
    [("Linehaul Seattle to Tacoma, FTL 53ft", 6, 1450.00, 8700.00),
     ("Fuel surcharge", 6, 348.00, 2088.00),
     ("Liftgate and inside delivery", 4, 185.00, 740.00),
     ("Storage, pallet-days", 120, 9.50, 1140.00)],
    0.0, tax_label="Tax")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "01_clean_summit_office_INV-24817.pdf": render(dict(SUMMIT), layout_classic),
        "02_poor_scan_evergreen_EFM-260917.pdf": degrade_to_scan(render(dict(EVERGREEN), layout_boxed)),
        "04_math_mismatch_brightline_BLE-10382.pdf": render(dict(BRIGHTLINE), layout_modern),
        "05_high_value_cascade_CFP-88412.pdf": render(dict(CASCADE), layout_classic,
                                                        accent=colors.HexColor("#0F4C5C")),
    }
    reminder_pdf = render(dict(SUMMIT), layout_classic, stamp="SECOND NOTICE")
    files["03_duplicate_summit_reminder.eml"] = build_eml(reminder_pdf, "Summit_INV-24817_second_notice.pdf")
    for name, content in files.items():
        (OUT / name).write_bytes(content)
        print(f"  wrote {name} ({len(content) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
