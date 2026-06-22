#!/usr/bin/env python3
"""
Delinquent Tax Sale -> Owner enrichment collector  (Task C)
===========================================================
1. Downloads the Hamilton County "Delinquent Tax Sale List <year>" PDF
   (Clerk & Master).
2. Parses each property: address, parcel, minimum bid, and whether it's
   already PAID (redeemed).
3. Searches each address on the Trustee Property Search site (by address)
   to find the CURRENT OWNER NAME -- the list itself has no owner names,
   so this is the lead value.
4. Writes everything to DelinquentTaxSale_Owners.csv

Requires: python3 + requests.   Run:  python3 DelinquentTaxCollector.py
(Set MAX_ROWS=5 in the environment to test on just a few.)
"""

import os
import re
import csv
import sys
import time
import html
import subprocess
import tempfile
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
TRUSTEE_URL = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertySearch.aspx?SearchType=3"
PDF_BASE = "https://www.hamiltontn.gov/Clerkmasterforms/taxsale"
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "DelinquentTaxSale_Owners.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
THROTTLE = 2.0
TIMEOUT = 30
MAX_ROWS = int(os.environ.get("MAX_ROWS") or 0)   # 0/blank = all; >0 = quick test

ROW = re.compile(
    r'^\s*(PAID\s+)?'
    r'(\d{4,5})\s+'                  # docket
    r'(\d+)\s+'                      # item #
    r'(\d+\s+)?'                     # optional street number
    r'([A-Z0-9][A-Z0-9 .\-]*?)\s+'   # street name
    r'(\d{3}[A-Z]?)\s+'              # MAP (urban 145L / rural 150)
    r'(?:([A-Z])\s+)?'               # optional GROUP letter
    r'([\dA-Z][\dA-Z. ]*?)\s+'       # PARCEL (may contain a space)
    r'\$([\d,]+\.\d{2})\s*$'         # minimum bid
)


def decode(s):
    return html.unescape(s)


# ----------------------------------------------------------------------------
# 1) DOWNLOAD THE PDF (find the current year's file)
# ----------------------------------------------------------------------------
def download_pdf(session):
    year = datetime.now().year
    for y in (year, year - 1, year - 2):
        url = f"{PDF_BASE}/{y}TaxSale/DELINQUENT%20TAX%20SALE%20LIST%20{y}.pdf"
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.ok and r.headers.get("content-type", "").startswith("application/pdf"):
                print(f"Got tax sale list: {url}")
                return r.content, y
        except requests.RequestException:
            pass
    sys.exit("ERROR: could not find the Delinquent Tax Sale List PDF.")


def pdf_to_text(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True, check=True)
        return out.stdout
    finally:
        os.unlink(path)


# ----------------------------------------------------------------------------
# 2) PARSE ROWS
# ----------------------------------------------------------------------------
def parse_rows(text):
    rows = []
    for ln in text.splitlines():
        if not re.search(r'\d{4,5}\s+\d+', ln):
            continue
        m = ROW.match(ln)
        if not m:
            continue
        paid = bool(m.group(1))
        sno = (m.group(4) or "").strip()
        street = m.group(5).strip()
        mp, gp, parcel = m.group(6), m.group(7), m.group(8).strip()
        pid = f"{mp}-{gp}-{parcel}" if gp else f"{mp}-{parcel}"
        rows.append({
            "paid": "PAID" if paid else "",
            "docket": m.group(2), "item": m.group(3),
            "address": (sno + " " + street).strip(),
            "has_number": bool(sno),
            "parcel": pid, "bid": "$" + m.group(9),
        })
    return rows


# ----------------------------------------------------------------------------
# 3) TRUSTEE ADDRESS SEARCH -> OWNER
# ----------------------------------------------------------------------------
def _req(session, method, url, **kw):
    for attempt in range(3):
        try:
            r = session.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def extract_hidden(page):
    fields = {}
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', page, re.I):
        n = re.search(r'name="([^"]*)"', tag)
        if n:
            v = re.search(r'value="([^"]*)"', tag)
            fields[decode(n.group(1))] = decode(v.group(1)) if v else ""
    return fields


def lookup_owner(session, address, parcel):
    getr = _req(session, "GET", TRUSTEE_URL)
    if not getr:
        return "", "site error (get)"
    payload = extract_hidden(getr.text)
    payload["ctl00$MainContent$txtPropAddress"] = address
    payload["ctl00$MainContent$cmdPropAddress_Search"] = "Search"
    postr = _req(session, "POST", TRUSTEE_URL, data=payload,
                 headers={"Referer": TRUSTEE_URL, "Origin": "https://tpti.hamiltontn.gov"})
    if not postr:
        return "", "site error (post)"
    return parse_owner(postr.text, address, parcel)


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _owner_after(text, want):
    m = re.search(r"\b" + re.escape(want) + r"\b", text)
    if not m:
        return ""
    return re.sub(r"\s{2,}", " ", text[m.end():]).strip(" .-|")[:60]


def parse_owner(page, address, parcel=""):
    idx = page.find("ctl00_MainContent_dgrResults")
    if idx < 0:
        return "", "no match"
    grid = page[idx:]
    cut = grid.find("Send any suggestions")          # drop the page footer
    if cut > 0:
        grid = grid[:cut]
    rows = re.split(r'<tr class="DG[A-Za-z]*Item"', grid)[1:]   # one element per result row
    want = re.sub(r"\s+", " ", address).upper().strip()
    target_parcel = _norm(parcel) if parcel else ""

    texts = []
    for part in rows:
        t = re.sub(r"\s+", " ", decode(re.sub(r"<[^>]+>", " ", part))).strip().upper()
        texts.append(re.sub(r"<[^>]*$", "", t).strip())

    # Best: the row whose PARCEL matches the delinquent list (pins the exact unit).
    if target_parcel:
        for t in texts:
            if target_parcel in _norm(t):
                owner = _owner_after(t, want) or _owner_after(t, "")  # after address, else whole tail
                if owner:
                    return owner, "owner found"

    # Fall back to exact-address match.
    matches = [_owner_after(t, want) for t in texts if re.search(r"\b" + re.escape(want) + r"\b", t)]
    matches = [o for o in matches if o]
    if len(matches) == 1:
        return matches[0], "owner found"
    if len(matches) > 1:
        return "", f"complex/multiple ({len(matches)} at this address) - parcel not matched"
    return "", (f"no exact match ({len(rows)} results)" if rows else "no match")


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    pdf, year = download_pdf(session)
    rows = parse_rows(pdf_to_text(pdf))
    print(f"Parsed {len(rows)} properties from the {year} list")
    if MAX_ROWS:
        rows = rows[:MAX_ROWS]
        print(f"(test mode: first {MAX_ROWS})")

    collected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for r in rows:
        if r["has_number"]:
            owner, status = lookup_owner(session, r["address"], r["parcel"])
            time.sleep(THROTTLE)
        else:
            owner, status = "", "land (no street #) - not searched"
        out.append([collected, r["paid"], r["docket"], r["item"], r["address"],
                    r["parcel"], r["bid"], owner, status])
        print(f"  {r['address'][:30]:30} {r['bid']:>12}  -> {owner[:30] or status}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Collected On", "Paid?", "Docket", "Item #", "Property Address",
                    "Parcel", "Minimum Bid", "Owner (from Trustee)", "Status"])
        w.writerows(out)
    print(f"\nDone. {len(out)} rows -> {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
