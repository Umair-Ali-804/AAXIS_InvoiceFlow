"""Load the demo invoices through the real pipeline (upload + email intake) and print the outcome.

Usage:  python scripts/run_demo.py [--reset]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, emailin, pipeline  # noqa: E402
from app.config import settings  # noqa: E402

DEMO_DIR = ROOT / "demo" / "invoices"


def ensure_demo_files() -> None:
    if not DEMO_DIR.exists() or not any(DEMO_DIR.iterdir()):
        import runpy
        runpy.run_path(str(ROOT / "demo" / "generate_invoices.py"), run_name="__main__")


def load_demo(actor: str = "Demo loader") -> list[int]:
    ensure_demo_files()
    ids = []
    for path in sorted(DEMO_DIR.iterdir()):
        if path.suffix == ".eml":
            message = emailin.parse_eml(path.read_bytes())
            for attachment in message["attachments"]:
                ids.append(pipeline.ingest(attachment["content"], attachment["filename"], source="email",
                                           source_detail=f"{message['from']} — {message['subject']}", actor=actor))
        elif path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}:
            ids.append(pipeline.ingest(path.read_bytes(), path.name, source="demo", actor=actor))
    return ids


def reset() -> None:
    if settings.data_dir.exists():
        shutil.rmtree(settings.data_dir)
    settings.ensure_dirs()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete all data first")
    args = parser.parse_args()
    if args.reset:
        reset()
    db.init_db()
    pipeline.seed_vendors()
    for invoice_id in load_demo():
        inv = db.get_invoice(invoice_id)
        open_issues = [i["title"] for i in inv["issues"] if i["status"] == "open" and i["severity"] != "info"]
        print(f"{inv['ref']}  {inv['status']:<24} {inv['vendor_name'] or '?':<36} "
              f"{inv['invoice_number'] or '?':<11} {inv['total'] or 0:>10,.2f}  "
              f"{inv['extraction_method']:<10} {inv['processing_ms']}ms")
        for title in open_issues:
            print(f"          • {title}")


if __name__ == "__main__":
    main()
