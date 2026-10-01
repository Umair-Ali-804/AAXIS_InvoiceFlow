# Deploy AAXIS InvoiceFlow for free and share the link

When you finish this guide you will have a link such as

**`https://aaxis-invoiceflow.onrender.com`**

It opens InvoiceFlow with the 5 demo invoices already loaded: a clean invoice, a poor scan, a duplicate, a math mismatch and a high-value invoice.

It runs on **Render's free plan** ($0, no credit card) as one Docker container, with Tesseract OCR included. **No API keys are needed:** OCR and the rules run inside the container, and QuickBooks uses the built-in simulator. The link is created under **your** Render account when you deploy, which takes about 15–20 minutes the first time.

> Already deployed AAXIS Assistant? This works the same way, but uses its **own** GitHub repository and its **own** Render service.

---

## 1. What you need

| | Where |
| --- | --- |
| GitHub account | <https://github.com/signup> |
| Render account | <https://dashboard.render.com/register> (choose **Sign up with GitHub**) |

Optional, later: an Anthropic or OpenAI key (AI cross-check) and a QuickBooks sandbox app (real Bills). See section 7.

---

## 2. Put the project on GitHub

1. <https://github.com/new> → name **`aaxis-invoiceflow`** → **Private** → **Create repository** (no README).
2. Upload the files. Choose **one** option:
   - **Browser:** click **uploading an existing file**, select **everything inside** the unzipped `aaxis-invoiceflow` folder (`Dockerfile`, `render.yaml`, `app`, `demo`, `scripts`, `tests`, `docs`, `requirements.txt`, …), drag it in, then **Commit changes**. `Dockerfile` must be at the **top level** of the repository.
   - **Git:**
     ```bash
     cd aaxis-invoiceflow
     git init -b main && git add . && git status     # .env and data/ must NOT be listed
     git commit -m "AAXIS InvoiceFlow"
     git remote add origin https://github.com/<your-username>/aaxis-invoiceflow.git
     git push -u origin main
     ```

---

## 3. Create the free service on Render

1. <https://dashboard.render.com> → **New +** → **Blueprint**.
2. Select the **`aaxis-invoiceflow`** repository → **Connect**.
3. Render reads `render.yaml` and shows **aaxis-invoiceflow**: Web Service, Docker, **Free**. Click **Deploy Blueprint**.

Nothing needs to be typed in. `render.yaml` already sets the port, the demo company name, the $10,000 two-person approval limit and `AUTO_LOAD_DEMO=true`.

---

## 4. Check the first deploy

1. Service → **Logs**. The first build takes **5–10 minutes**, mostly installing Tesseract and OpenCV.
2. Wait for:
   ```
   Uvicorn running on http://0.0.0.0:10000
   ==> Your service is live 🎉
   AUTO_LOAD_DEMO: loaded 5 demo invoices
   ```
   The demo loader reads the scanned invoice with OCR. On the free server's small CPU this can take **1–3 minutes**, so the invoices appear on the dashboard one by one.
3. Open your link (shown at the top of the service page). Try the demo:

   | Open | What you see |
   | --- | --- |
   | **Invoices → Evergreen Facilities** | Poor scan: evidence highlights on the page, low-confidence fields, suggested decimal fixes |
   | **Summit Office Supply (Held · duplicate)** | Side-by-side comparison with the original |
   | **BrightLine Electrical** | $90.00 math error caught; reject it with a reason |
   | **Cascade Freight** | Over $10,000: approve as one person, then switch the user (top right) and approve again |
   | A clean invoice → **QuickBooks** tab → **Approve and post to QuickBooks** | The exact Bill that is created (simulator) |
   | **Audit trail** | *Chain intact — N events verified*: hash-chained log of every action |

---

## 5. Share the link

- Send the link to clients, or put it in your portfolio or proposals. No sign-in is needed: the user picker (top right) only switches between demo reviewers.
- **Private instead?** Render → service → **Environment** → **Add Environment Variable**: `ACCESS_PASSWORD` = a password of your choice → **Save**. The browser will then ask for a username (anything) and that password. `/healthz` stays open for monitoring.

---

## 6. Free-plan behaviour

| | |
| --- | --- |
| **Sleep** | After 15 minutes without visitors the service sleeps. The next visit takes about 1 minute to wake it. A free monitor at <https://uptimerobot.com> pinging `https://<your-link>/healthz` every 10 minutes keeps it awake (about 744 of the 750 free hours per month) |
| **Data resets** | The free disk is wiped on sleep, restart or redeploy. With `AUTO_LOAD_DEMO=true`, the 5 demo invoices come back automatically. Anything visitors uploaded or approved is reset, which is ideal for a public demo. Visitors can also click **Load demo invoices** or **Clear workspace** on the Invoices page |
| **Speed** | Text PDFs: under a second. Scans and phone photos (OCR): about 30–90 seconds on the free 0.1-CPU server (about 10 s on a laptop) |
| **Memory** | About 140 MB idle, about 300 MB while reading a scan. That fits the 512 MB limit; upload one scan at a time |

Want data that persists and faster OCR? Upgrade the Render instance to **Starter** and add a **Disk** mounted at `/home/user/app/data`.

---

## 7. Optional settings

Render → service → **Environment**. Saving redeploys the service.

| Variable | Example | Effect |
| --- | --- | --- |
| `ACCESS_PASSWORD` | `my-demo-pass` | Password for the whole app |
| `AUTO_LOAD_DEMO` | `true` | Load the demo invoices when the database is empty |
| `COMPANY_NAME`, `BASE_CURRENCY` | `Harbor & Pine…`, `USD` | Shown in the app and used for currency checks |
| `SECOND_APPROVAL_LIMIT` | `10000` | Above this total, two different people must approve |
| `CONFIDENCE_THRESHOLD`, `MATH_TOLERANCE`, `DUPLICATE_WINDOW_DAYS` | `0.85`, `0.02`, `10` | Review policy |
| `LLM_PROVIDER` + `ANTHROPIC_API_KEY` *or* `OPENAI_API_KEY` | `anthropic` | Optional AI cross-check of every extraction |
| `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_ENVIRONMENT=sandbox` | from developer.intuit.com | Real QuickBooks **sandbox** Bills instead of the simulator |
| `QBO_REDIRECT_URI` | `https://<your-link>/integrations/quickbooks/callback` | Register the same URL in your Intuit app, then use **Settings → Connect QuickBooks** |
| `QBO_REALM_ID`, `QBO_REFRESH_TOKEN` | from Intuit's OAuth 2.0 Playground | Alternative to the Connect button |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | `imap.gmail.com`, app password | Import invoices from an AP inbox (**Check AP inbox** button) |

> ⚠️ If you connect a real QuickBooks company or a real mailbox, **always set `ACCESS_PASSWORD`**. Otherwise anyone with the link could approve invoices and create Bills.

---

## 8. Troubleshooting

| Problem | Fix |
| --- | --- |
| `Dockerfile not found` | The files are inside a sub-folder on GitHub. `Dockerfile` must be at the repository root |
| `No open ports detected` | `PORT` must be `10000` (set by `render.yaml`) |
| Dashboard is empty | The demo is still loading (OCR of the scan takes 1–3 minutes after start-up), or `AUTO_LOAD_DEMO` isn't `true`. You can also click **Invoices → Load demo invoices**. **Clear workspace** empties it |
| A scan says *Couldn't read* | Low-quality images can fail OCR. Try a straighter, higher-resolution photo, or use a text PDF |
| `Ran out of memory` event | Several large scans were uploaded at once. Upload one at a time, or upgrade the instance |
| First visit takes about a minute | The free service was asleep (section 6) |
| QuickBooks "Connect" fails | `QBO_REDIRECT_URI` must exactly match the redirect URI registered in your Intuit app, including `https://` |

---

## 9. Other hosting options

The same `Dockerfile` runs on Koyeb, Railway, Google Cloud Run, Fly.io, a VPS, or Hugging Face Spaces (Docker Spaces need a PRO plan; add `sdk: docker` and `app_port: 7860` to the top of `README.md`). Locally:

```bash
docker build -t invoiceflow . && docker run -p 7860:7860 -e AUTO_LOAD_DEMO=true invoiceflow
# open http://localhost:7860
```
