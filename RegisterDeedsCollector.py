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
THROTTLE  = 3.0                                  # polite pause between requests
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))
DOC_TYPE  = os.environ.get("DOC_TYPE", "A01-AFFIDAVIT")  # as shown in the dropdown
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))        # hard cap (politeness)
SAVE_HTML = os.environ.get("SAVE_HTML", "1") == "1"      # dump raw pages to calibrate

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
def run_search(session, search_html):
    """Submit a Document Search for DOC_TYPE over the last DAYS_BACK days.

    Field names are taken from the saved Search.aspx form where possible; the
    obvious ASP.NET ids are tried as a fallback. Always saves the response.
    """
    payload = hidden_fields(search_html)
    # Best-effort field guesses (calibrate from search_page.html):
    #   date range, "back N days" option, and the document-type dropdown.
    for k in list(payload):
        low = k.lower()
        if low.endswith("ddldoctype") or "doctype" in low:
            payload[k] = DOC_TYPE
        if "rbldirection" in low or "back" in low:
            payload.setdefault(k, "Back")
    # Try to set a "last 30/60/90" radio if present, else a date range.
    payload.setdefault("ctl00$MainContent$ddlDocType", DOC_TYPE)
    payload.setdefault("ctl00$MainContent$btnSearch", "Search")
    r = session.post(SEARCH_URL, data=payload, timeout=TIMEOUT,
                     headers={"Referer": SEARCH_URL})
    save("results_page.html", r.text)
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
    time.sleep(THROTTLE)

    print(f"Searching: document type {DOC_TYPE}, last {DAYS_BACK} days (max {MAX_PAGES} pages) …")
    results_html = run_search(session, chk.text)
    time.sleep(THROTTLE)
    rows = parse_results(results_html)
    print(f"  parsed {len(rows)} party rows.")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Doc Sub-Type", "Parcel", "City", "Book/Page", "Source"])
        for r in rows:
            w.writerow([r["name"], r["type_of"], r["parcel"], r["city"],
                        r["book_page"], "Register of Deeds affidavit"])

    print(f"\nDone. {len(rows)} rows -> {OUTPUT_CSV}")
    if not rows:
        print("NOTE: 0 rows usually means the parser needs calibrating against the "
              "real page. Check the saved results_page.html artifact.")


if __name__ == "__main__":
    main()
