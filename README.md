# Chancery Court Docket Name Collector

Pulls individual case-party names from the Hamilton County Chancery Court
**Current Motion Call Docket** PDFs and saves them to `ChanceryCourt_Names.csv`.

## What it does
1. Opens https://www.hamiltontn.gov/ChanceryCourt_Dockets.aspx
2. Finds the links labeled **Current Motion Call Docket Part 1** and **Part 2**
   (by their label, so it always grabs the current week — no stale links).
3. Downloads both PDFs and extracts the individual party names.
4. Writes them to `ChanceryCourt_Names.csv` (columns: Docket, Docket Date, Name).

It deliberately **ignores** the *Procedural Steps List* and *Motion Call
Schedule* links — those contain no case names.

## How it runs
- **Automatically** every Monday (see `.github/workflows/weekly.yml`) — GitHub
  runs it in the cloud for free, no server needed.
- **On demand** — go to the **Actions** tab → *Weekly Chancery Court name
  collection* → **Run workflow**.

Each run:
- updates `ChanceryCourt_Names.csv` in this repo, and
- uploads the CSV as a downloadable artifact on the run page.

## Run it locally (optional)
Requires `python3`, `requests`, and `pdftotext` (poppler-utils):

```
pip install requests
sudo apt-get install -y poppler-utils   # Debian/Ubuntu
python3 ChanceryCourtCollector.py
```
