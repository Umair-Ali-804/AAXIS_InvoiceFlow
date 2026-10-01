"""Build InvoiceFlow_Colab.ipynb (run once; output is committed alongside the project)."""
import json
from pathlib import Path


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text, title=None):
    meta = {"cellView": "form"} if title else {}
    return {"cell_type": "code", "metadata": meta, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


cells = [
    md("""
# AAXIS InvoiceFlow — run in Google Colab

Invoice capture with human review, posting to QuickBooks Online.

**Before you start:** unzip `aaxis-invoiceflow.zip` into your Google Drive, e.g. `MyDrive/aaxis-invoiceflow`.
Then run the cells below **in order** (Runtime → Run all works too).

| Step | What it does | Time |
|---|---|---|
| 1 | Mount Drive and copy the project to fast local disk | ~20 s |
| 2 | Install Tesseract OCR and Python packages | ~40 s |
| 3 | Optional keys (AI cross-check, QuickBooks sandbox, IMAP) | — |
| 4 | Generate the demo invoices and run the 20 tests | ~20 s |
| 5 | Start the app | ~5 s |
| 6 | Open the app → click **Load demo invoices** | — |

Everything works without any keys: OCR + rules for reading, and a QuickBooks simulator for posting.
"""),
    md("## 1 · Mount Google Drive and copy the project"),
    code(r'''
#@title Mount Drive and locate the project { display-mode: "form" }
DRIVE_FOLDER = "/content/drive/MyDrive/aaxis-invoiceflow"  #@param {type:"string"}

from google.colab import drive
drive.mount("/content/drive")

import os, shutil, glob
from pathlib import Path

# Find the folder that contains app/main.py (handles a nested folder after unzipping)
SRC = Path(DRIVE_FOLDER) if (Path(DRIVE_FOLDER) / "app" / "main.py").exists() else None
if SRC is None:
    found = glob.glob(f"{DRIVE_FOLDER}/**/app/main.py", recursive=True) or \
        glob.glob("/content/drive/MyDrive/**/aaxis-invoiceflow*/**/app/main.py", recursive=True)
    SRC = Path(found[0]).parent.parent if found else None
if SRC is None:
    raise FileNotFoundError(f"Couldn't find app/main.py under {DRIVE_FOLDER}. Check where you unzipped the project.")

# Run from local disk: much faster than Drive, and SQLite is reliable there.
APP = Path("/content/aaxis-invoiceflow")
DATA = Path("/content/invoiceflow_data")
if APP.exists():
    shutil.rmtree(APP)
shutil.copytree(SRC, APP, ignore=shutil.ignore_patterns("data", "__pycache__", ".pytest_cache"))
os.chdir(APP)
print("Project found at:", SRC)
print("Running from:   ", APP)
print("Data stored in: ", DATA, "(use step 8 to back it up to Drive)")
'''),
    md("## 2 · Install OCR engine and packages"),
    code(r'''
#@title Install Tesseract OCR + Python packages { display-mode: "form" }
!apt-get -qq update > /dev/null 2>&1
!apt-get -qq install -y tesseract-ocr > /dev/null 2>&1
# Colab already ships numpy, pillow, opencv, jinja2 and httpx — only add what's missing.
!pip -q install fastapi "uvicorn>=0.27" python-multipart pymupdf pytesseract rapidfuzz python-dateutil reportlab pytest

!tesseract --version | head -1
import cv2, pymupdf, fastapi, rapidfuzz
print("OpenCV", cv2.__version__, "| PyMuPDF", pymupdf.VersionBind, "| FastAPI", fastapi.__version__)
'''),
    md("""
## 3 · Optional settings

Leave everything blank for the offline demo. To add keys safely, use **Colab Secrets** (🔑 icon in the left sidebar)
with these names — they're read here and never printed:

* `ANTHROPIC_API_KEY` **or** `OPENAI_API_KEY` — independent AI reading of each invoice
* `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REALM_ID`, `QBO_REFRESH_TOKEN` — real QuickBooks sandbox.
  Get the realm ID and refresh token from Intuit's **OAuth 2.0 Playground** (developer.intuit.com → your app).
* `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` — read invoices from an AP inbox (Gmail: `imap.gmail.com` + an app password)
"""),
    code(r'''
#@title Write the .env file { display-mode: "form" }
COMPANY_NAME = "Harbor & Pine Property Management"  #@param {type:"string"}
SECOND_APPROVAL_LIMIT = 10000  #@param {type:"number"}
CONFIDENCE_THRESHOLD = 0.85  #@param {type:"number"}

def secret(name):
    try:
        from google.colab import userdata
        return userdata.get(name) or ""
    except Exception:
        return ""

env = {
    "COMPANY_NAME": COMPANY_NAME,
    "SECOND_APPROVAL_LIMIT": SECOND_APPROVAL_LIMIT,
    "CONFIDENCE_THRESHOLD": CONFIDENCE_THRESHOLD,
    "DATA_DIR": str(DATA),
}
for name in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REALM_ID",
             "QBO_REFRESH_TOKEN", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"]:
    value = secret(name)
    if value:
        env[name] = value
if env.get("ANTHROPIC_API_KEY"):
    env["LLM_PROVIDER"] = "anthropic"
elif env.get("OPENAI_API_KEY"):
    env["LLM_PROVIDER"] = "openai"

(APP / ".env").write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
print("AI cross-check:", env.get("LLM_PROVIDER") or "off (OCR + rules)")
print("QuickBooks:    ", "sandbox" if env.get("QBO_REFRESH_TOKEN") else "simulator")
print("Email intake:  ", "on" if env.get("IMAP_HOST") else "off (drop .eml files instead)")
'''),
    md("## 4 · Generate the demo invoices and run the tests"),
    code(r'''
#@title Build demo invoices + run 20 end-to-end tests { display-mode: "form" }
!python demo/generate_invoices.py
!python -m pytest -q
'''),
    md("## 5 · Start the app"),
    code(r'''
#@title Start InvoiceFlow in the background { display-mode: "form" }
import subprocess, time, urllib.request, os

if "server" in globals() and server.poll() is None:
    server.terminate(); server.wait()

log = open("/content/invoiceflow.log", "w")
server = subprocess.Popen(
    ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
    cwd=str(APP), stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "DATA_DIR": str(DATA)},
)
for _ in range(40):
    try:
        print(urllib.request.urlopen("http://127.0.0.1:8000/healthz").read().decode())
        break
    except Exception:
        time.sleep(0.5)
else:
    print(open("/content/invoiceflow.log").read()[-3000:])
    raise RuntimeError("The server didn't start — see the log above.")
print("InvoiceFlow is running.")
'''),
    md("""
## 6 · Open the app

Click the link below (it opens in a new tab, only you can access it). Then:

1. Click **Load demo invoices** — five invoices go through the pipeline (the scan takes ~10 s).
2. Open **AP-1002** (the poor scan): click a field to see where it was read, click **Use unit price 42.00**, fix line 2 (qty `3.5`, amount `332.50`), **Save and recheck**, then **Confirm** the remaining fields and **Approve**.
3. Open **AP-1003** to see the duplicate hold, **AP-1004** for the math error.
4. Approve **AP-1005** as Maya, switch the reviewer (top right) to Daniel and give the second approval.
5. Check **Audit trail** — every step is hash-chained.
"""),
    code(r'''
#@title Get the app link { display-mode: "form" }
from google.colab.output import eval_js
from IPython.display import HTML, display
url = eval_js("google.colab.kernel.proxyPort(8000)")
print(url)
display(HTML(f'<a href="{url}" target="_blank" style="display:inline-block;padding:10px 18px;background:#000;color:#fff;'
             f'border-radius:6px;font:600 15px system-ui;text-decoration:none">Open AAXIS InvoiceFlow ↗</a>'))
'''),
    code(r'''
#@title (Alternative) Show the app inside this notebook { display-mode: "form" }
from google.colab import output
output.serve_kernel_port_as_iframe(8000, height=900)
'''),
    md("""
## 7 · Optional: a public link for a client demo

The link above only works for your Google account. To show the app to a client on a call,
create a temporary public link with Cloudflare Tunnel (no account needed). **Anyone with the link can use the app** —
share it only for the demo and stop it afterwards (stop this cell or run step 9).
"""),
    code(r'''
#@title Create a temporary public link { display-mode: "form" }
import re, subprocess, time
!wget -q -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /content/cloudflared
!chmod +x /content/cloudflared
if "tunnel" in globals() and tunnel.poll() is None:
    tunnel.terminate(); tunnel.wait()
tunnel_log = open("/content/cloudflared.log", "w")
tunnel = subprocess.Popen(["/content/cloudflared", "tunnel", "--url", "http://127.0.0.1:8000", "--no-autoupdate"],
                          stdout=tunnel_log, stderr=subprocess.STDOUT)
public_url = None
for _ in range(60):
    time.sleep(1)
    match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open("/content/cloudflared.log").read())
    if match:
        public_url = match.group(0)
        break
print(public_url or "Tunnel didn't start — run the cell again.")
'''),
    md("## 8 · Utilities"),
    code(r'''
#@title Show the server log { display-mode: "form" }
print(open("/content/invoiceflow.log").read()[-4000:])
'''),
    code(r'''
#@title Back up invoices, page images and audit trail to Drive { display-mode: "form" }
import shutil, datetime
target = SRC / "backups" / datetime.datetime.now().strftime("data-%Y%m%d-%H%M")
shutil.copytree(DATA, target)
print("Saved to", target)
'''),
    code(r'''
#@title Stop the app (and public link) { display-mode: "form" }
for name in ("server", "tunnel"):
    proc = globals().get(name)
    if proc and proc.poll() is None:
        proc.terminate()
        print("stopped", name)
'''),
]

nb = {"cells": cells, "metadata": {"colab": {"provenance": [], "name": "AAXIS InvoiceFlow"},
                                   "kernelspec": {"display_name": "Python 3", "name": "python3"},
                                   "language_info": {"name": "python"}},
      "nbformat": 4, "nbformat_minor": 0}
out = Path(__file__).resolve().parent.parent / "InvoiceFlow_Colab.ipynb"
out.write_text(json.dumps(nb, indent=1, ensure_ascii=False))
print("wrote", out)
