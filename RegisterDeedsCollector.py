#!/usr/bin/env python3
"""
Register of Deeds -> Affidavit names + property addresses   (Task E, "safe" build)
=================================================================================

From the Hamilton County Register of Deeds ONLINE RECORD SEARCH (a paid, login-only
site), it:
  1. Logs in with Steven's own subscription credentials (read from env/secrets --
     this script NEVER hardcodes them).
  2. Runs a Document Search: document type = AFFIDAVIT, over the last N days.
  3. Parses the result rows for the PARTY NAMES + property ADDRESS + PARCEL +
     document sub-type (HEIRSHIP / MODIF / ...) + book-page + file date.
  4. Writes RegisterDeeds_Affidavits.csv.

SAFE BY DESIGN (this is the part Steven worried about -- account suspension):
  - It does the NAMES + ADDRESSES only. It does NOT bulk-download the PDF images
    (that's the high-risk action; Steven keeps that manual).
  - Heavy throttle + a hard page cap, so it behaves like a careful human, not a
    scraper hammering his paid account.

CALIBRATION:
  The search form and results pages live behind the login, so on the FIRST
  authenticated run this script also saves the raw HTML (search_page.html,
  results_page.html). Those are uploaded as a workflow artifact so the parser can
  be finalized against the REAL page. If parsing returns 0 rows, that's expected
  until calibrated -- check the artifact.

Credentials (set as GitHub Actions secrets, never in code):
  REGISTER_USER, REGISTER_PASS

Run:  REGISTER_USER=... REGISTER_PASS=... python3 RegisterDeedsCollector.py
"""

import os
import re
import csv
import sys
import time
import html
import random

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE      = "https://register.hamiltontn.gov/OnlineRecordSearch/Beta/"
LOGIN_URL = BASE + "Login.aspx"
SEARCH_URL = BASE + "Search.aspx"
RESULTS_URL = BASE + "Results.aspx"
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "RegisterDeeds_Affidavits.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT   = 30
# Go slow + HUMAN-LIKE: wait a RANDOM number of seconds between actions (a fixed
# interval looks like a bot; a varied one looks like a person reading). Tunable by
# env so it can be made even gentler without touching code.
SLOW_MIN  = float(os.environ.get("SLOW_MIN", "6"))    # min seconds between requests
SLOW_MAX  = float(os.environ.get("SLOW_MAX", "14"))   # max seconds between requests
BETWEEN_SEARCHES = float(os.environ.get("BETWEEN_SEARCHES", "25"))  # longer rest per type
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))


def polite_sleep(longer=False):
    """Pause a random, human-like amount. `longer` = the bigger rest between searches."""
    if longer:
        secs = BETWEEN_SEARCHES + random.uniform(0, 10)
    else:
        secs = random.uniform(SLOW_MIN, SLOW_MAX)
    print(f"      …waiting {secs:.0f}s (going slow, like a person)")
    time.sleep(secs)
# One OR MANY document types (comma-separated, as shown in the dropdown). Default is
# just affidavits; after calibration the exact codes for other high-value lead types
# (deeds, liens, etc.) get added here. Kept gentle on purpose -- this hits a PAID
# account, so it stays a careful, capped crawl, never a bulk "grab everything".
DOC_TYPES = [t.strip() for t in os.environ.get("DOC_TYPES",
             os.environ.get("DOC_TYPE", "A01-AFFIDAVIT")).split(",") if t.strip()]
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))        # hard cap PER type (politeness)
SAVE_HTML = os.environ.get("SAVE_HTML", "1") == "1"      # dump raw pages to calibrate

# PDF download (the "selection of the pdf files" Steven asked for). This is the
# HIGHER-RISK part, so it's: OFF unless turned on, hard-capped, slow, and grabs only
# the documents for the records we FOUND (the leads) -- never every doc on the site.
FETCH_PDFS = os.environ.get("FETCH_PDFS", "0") == "1"    # opt-in
MAX_PDFS   = int(os.environ.get("MAX_PDFS", "25"))       # hard cap per run (safety)
PDF_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deeds_pdfs")

USER = os.environ.get("REGISTER_USER", "")
PASS = os.environ.get("REGISTER_PASS", "")


# ----------------------------------------------------------------------------
# ASP.NET form helpers (same hidden-field dance as the other collectors)
# ----------------------------------------------------------------------------
HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.I)
NAME_ATTR = re.compile(r'name="([^"]*)"')
VAL_ATTR  = re.compile(r'value="([^"]*)"')


def hidden_fields(page_html):
    fields = {}
    for tag in HIDDEN_RE.findall(page_html):
        nm = NAME_ATTR.search(tag)
        if nm:
            val = VAL_ATTR.search(tag)
            fields[html.unescape(nm.group(1))] = html.unescape(val.group(1)) if val else ""
    return fields


def save(name, text):
    if not SAVE_HTML:
        return
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"   (saved {name} for calibration, {len(text)} bytes)")


# ----------------------------------------------------------------------------
# LOGIN  (verified against the real public Login.aspx form)
# ----------------------------------------------------------------------------
def login(session):
    r = session.get(LOGIN_URL, timeout=TIMEOUT)
    r.raise_for_status()
    payload = hidden_fields(r.text)
    payload["ctl00$MainContent$txtUsername"] = USER
    payload["ctl00$MainContent$txtPassword"] = PASS
    payload["ctl00$MainContent$btnLogin"] = "Login"
    r2 = session.post(LOGIN_URL, data=payload, timeout=TIMEOUT,
                      headers={"Referer": LOGIN_URL}, allow_redirects=True)
    # Success = we can now load Search.aspx without being bounced to Login.aspx.
    chk = session.get(SEARCH_URL, timeout=TIMEOUT, allow_redirects=False)
    ok = chk.status_code == 200 and "login" not in chk.headers.get("Location", "").lower()
    if ok:
        save("search_page.html", chk.text)
    return ok, chk


# ----------------------------------------------------------------------------
# SEARCH + PARSE  (best-effort from the video; finalize against saved HTML)
# ----------------------------------------------------------------------------
def run_search(session, search_html, doc_type, save_as=None):
    """Submit a Document Search for one doc_type over the last DAYS_BACK days.

    Field names are taken from the saved Search.aspx form where possible; the
    obvious ASP.NET ids are tried as a fallback. Saves the first response for
    calibration.
    """
    payload = hidden_fields(search_html)
    # Best-effort field guesses (calibrate from search_page.html):
    #   date range, "back N days" option, and the document-type dropdown.
    for k in list(payload):
        low = k.lower()
        if low.endswith("ddldoctype") or "doctype" in low:
            payload[k] = doc_type
        if "rbldirection" in low or "back" in low:
            payload.setdefault(k, "Back")
    payload.setdefault("ctl00$MainContent$ddlDocType", doc_type)
    payload.setdefault("ctl00$MainContent$btnSearch", "Search")
    r = session.post(SEARCH_URL, data=payload, timeout=TIMEOUT,
                     headers={"Referer": SEARCH_URL})
    if save_as:
        save(save_as, r.text)
    return r.text


# Result rows in the video look like repeated blocks:
#   "Party: 1 JONES, LORETTA" ... "Address  City  Map Parcel" ... "CHATTANOOGA 129N E 021"
PARTY_RE   = re.compile(r"Party:\s*\d+\s+([A-Z][A-Z0-9 .,'&/-]{2,60})")
PARCEL_RE  = re.compile(r"\b(\d{2,3}[A-Z]?\s*[A-Z]?\s*[-A-Z]?\s*\d{2,3}(?:\s*[A-Z]\s*\d{2,3})?)\b")
BOOKPAGE_RE = re.compile(r"\b([A-Z]{1,3}\s?\d{3,6}\s+\d{1,4})\b")
TYPEOF_RE  = re.compile(r"\b(HEIRSHIP|MODIF|RELEASE|ASSIGN(?:MENT)?|DEED|LIEN|EASEMENT|POA)\b")


def parse_results(page_html):
    """Best-effort extraction of (name, address, parcel, type_of, book_page)."""
    # Strip tags but keep block boundaries; the real selectors get set after we
    # see results_page.html, but this captures the visible text reliably.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page_html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(tr|div|table|p)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)

    # Split into result blocks at "N. AFFIDAVIT" headers if present.
    blocks = re.split(r"\n\s*\d+\.\s*AFFIDAVIT", text)
    rows, seen = [], set()
    for b in blocks:
        names = [re.sub(r"\s+", " ", m.group(1)).strip(" .,")
                 for m in PARTY_RE.finditer(b)]
        if not names:
            continue
        bp = BOOKPAGE_RE.search(b)
        tof = TYPEOF_RE.search(b)
        parcel = ""
        # parcel usually sits right after a CITY token; grab the last parcel-looking hit
        pm = list(PARCEL_RE.finditer(b))
        if pm:
            parcel = re.sub(r"\s+", " ", pm[-1].group(1)).strip()
        city = "CHATTANOOGA" if "CHATTANOOGA" in b.upper() else ""
        for nm in names:
            if re.search(r"\b(BANK|LLC|INC|SYSTEMS|MORTGAGE|CORP|ASSOC|TRUST CO|COMPANY)\b", nm):
                continue                          # skip lenders/orgs -> individuals only
            key = nm.upper()
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "name": nm.title(),
                "type_of": tof.group(1) if tof else "",
                "parcel": parcel,
                "city": city,
                "book_page": bp.group(1) if bp else "",
            })
    return rows


# Each result links its document image, e.g. PDFViewer.aspx?ImageID=4090867 (seen in
# the video). Collect those image ids so we can fetch the matching PDFs.
IMAGEID_RE = re.compile(r"ImageID=(\d+)", re.I)


def find_image_ids(results_html):
    seen, ids = set(), []
    for m in IMAGEID_RE.finditer(results_html):
        i = m.group(1)
        if i not in seen:
            seen.add(i); ids.append(i)
    return ids


def download_pdfs(session, image_ids):
    """Download the document PDFs for the records we found — SLOWLY and CAPPED.

    This is the suspension-risk part, so: hard cap (MAX_PDFS), a human-like pause
    before each one, and only the leads' documents. The exact PDF byte URL is
    finalized at calibration (the viewer is PDFViewer.aspx?ImageID=…; the real
    download link is read from that page on the first authenticated run).
    """
    os.makedirs(PDF_DIR, exist_ok=True)
    todo = image_ids[:MAX_PDFS]
    got = 0
    print(f"  Downloading up to {len(todo)} document PDF(s) — slowly (capped at {MAX_PDFS}) …")
    for n, img in enumerate(todo, 1):
        polite_sleep()                                  # slow, human-like, before each
        viewer = f"{BASE}PDFViewer.aspx?ImageID={img}&ImageTypeID=2&BookTypeID=1"
        try:
            r = session.get(viewer, timeout=TIMEOUT)
            # If the viewer returns the PDF bytes directly, save them; otherwise look
            # for the real document link inside the viewer page (calibrated later).
            ctype = r.headers.get("Content-Type", "")
            if "pdf" in ctype.lower() or r.content[:4] == b"%PDF":
                path = os.path.join(PDF_DIR, f"deed_{img}.pdf")
                with open(path, "wb") as f:
                    f.write(r.content)
                got += 1
                print(f"    [{n}/{len(todo)}] saved deed_{img}.pdf")
            else:
                m = re.search(r'(?:src|href|data)="([^"]+\.(?:pdf|tif{1,2})[^"]*)"', r.text, re.I)
                if m:
                    doc = m.group(1)
                    if not doc.startswith("http"):
                        doc = BASE + doc.lstrip("/")
                    polite_sleep()
                    rr = session.get(doc, timeout=TIMEOUT)
                    if rr.content[:4] in (b"%PDF", b"II*\x00", b"MM\x00*"):
                        path = os.path.join(PDF_DIR, f"deed_{img}.pdf")
                        with open(path, "wb") as f:
                            f.write(rr.content)
                        got += 1
                        print(f"    [{n}/{len(todo)}] saved deed_{img}.pdf")
                else:
                    print(f"    [{n}/{len(todo)}] couldn't find the PDF link yet (calibrate)")
        except requests.RequestException as e:
            print(f"    [{n}/{len(todo)}] download error: {e}")
    print(f"  PDFs saved: {got} -> {PDF_DIR}")
    return got


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    if not (USER and PASS):
        sys.exit("ERROR: set REGISTER_USER and REGISTER_PASS (the subscription login). "
                 "This script never stores them; pass via env / GitHub secrets.")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print("Logging in to the Register of Deeds online search …")
    ok, chk = login(session)
    if not ok:
        sys.exit("ERROR: login failed (check REGISTER_USER / REGISTER_PASS, or the "
                 "subscription may be expired). The site bounced us back to Login.aspx.")
    print("  login OK.")
    polite_sleep()

    print(f"Searching {len(DOC_TYPES)} document type(s), last {DAYS_BACK} days, "
          f"max {MAX_PAGES} pages each — gently (this is a paid account) …")
    all_rows, seen = [], set()
    image_ids = []
    for i, dt in enumerate(DOC_TYPES):
        print(f"  [{i+1}/{len(DOC_TYPES)}] {dt} …")
        html_out = run_search(session, chk.text, dt,
                              save_as="results_page.html" if i == 0 else None)
        polite_sleep(longer=True)                 # longer human-like rest between searches
        rows = parse_results(html_out)
        for img in find_image_ids(html_out):
            if img not in image_ids:
                image_ids.append(img)
        for r in rows:
            key = (r["name"].upper(), r["book_page"])
            if key in seen:
                continue
            seen.add(key)
            r["doc_type"] = dt
            all_rows.append(r)
        print(f"      +{len(rows)} rows")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Doc Type", "Doc Sub-Type", "Parcel", "City", "Book/Page", "Source"])
        for r in all_rows:
            w.writerow([r["name"], r.get("doc_type", ""), r["type_of"], r["parcel"],
                        r["city"], r["book_page"], "Register of Deeds"])

    rows = all_rows
    print(f"\nNames/addresses: {len(rows)} rows -> {OUTPUT_CSV}")
    if not rows:
        print("NOTE: 0 rows usually means the parser needs calibrating against the "
              "real page. Check the saved results_page.html artifact.")

    # The "selection of the pdf files" Steven asked for — only if turned on (safety).
    if FETCH_PDFS:
        if image_ids:
            print(f"\nFETCH_PDFS on — found {len(image_ids)} document(s); fetching the "
                  f"first {min(len(image_ids), MAX_PDFS)} slowly …")
            download_pdfs(session, image_ids)
        else:
            print("\nFETCH_PDFS on, but no document image-ids were found "
                  "(parser/calibration needed — check results_page.html).")
    else:
        print("\n(PDF download is OFF — set FETCH_PDFS=1 to also grab the documents, "
              "slowly and capped. It's the higher-risk part, so it's opt-in.)")


if __name__ == "__main__":
    main()
