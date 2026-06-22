# Lead Collector

Automations that turn Hamilton County (TN) public records into lead lists and
deliver them to Telegram as a neat **PDF** + a **CSV** — free, no server.

## What it collects
| Task | Source | Output |
|---|---|---|
| **Court Dockets** | Chancery Court "Current Motion Call Docket" PDFs (Part 1 & 2) | case-party names |
| **Obituary → Addresses** | chattanoogan.com obituaries → Trustee property site | name + property address |
| **Tax Sale → Owners** | Delinquent Tax Sale List PDF → Trustee site (by address + parcel) | address, parcel, min bid, current owner |

Each collector is a small Python script (`*Collector.py`). `tools/csv_to_pdf.py`
renders any result CSV into a tidy PDF; `tools/notify.py` sends the files to Telegram.

## How it runs
- **Automatically every Monday** (Court Dockets + Obituary → Addresses) via GitHub
  Actions — free, runs in the cloud.
- **On demand** via the Telegram bot buttons / commands, or the **Actions** tab →
  *Run workflow*. (Tax Sale is on-demand only — it does ~140 lookups.)

Results are **delivered to Telegram and uploaded as run artifacts** — the lead data
is **not** committed to this repo (this repo is public; only the code lives here).

## The Telegram bot
A Cloudflare Worker (`telegram-bot/`) listens for button taps / commands and triggers
the right workflow, delivering the file to whoever asked. Pressing **/start** also
subscribes that chat to the weekly auto-delivery. See `telegram-bot/DEPLOY.md`.

## Run a collector locally
Requires `python3`, `requests`, `reportlab`, and `pdftotext` (poppler-utils):

```
pip install requests reportlab
sudo apt-get install -y poppler-utils    # Debian/Ubuntu
python3 ChanceryCourtCollector.py        # or ObituaryPropertyCollector.py / DelinquentTaxCollector.py
python3 tools/csv_to_pdf.py <result.csv> out.pdf "Title"
```

Secrets (Telegram + GitHub tokens) live in GitHub Actions secrets and on the Worker —
never in this repo.
