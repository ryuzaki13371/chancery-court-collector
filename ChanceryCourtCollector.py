#!/usr/bin/env python3
"""
Chancery Court Docket Name Collector
====================================

Pulls individual case-party names from the Hamilton County Chancery Court
"Current Motion Call Docket" PDFs and writes them to a clean CSV.

What it does (matches the client's spec exactly):
  1. Opens the Chancery Court Dockets page.
  2. Finds the links labeled "Current Motion Call Docket Part 1" and
     "...Part 2" -- by their LABEL, so it always grabs whatever is current
     that week (no hardcoded links).
  3. Downloads both PDFs.
  4. Extracts the individual party names from the cases.
  5. Saves them to ChanceryCourt_Names.csv  (columns: Docket, Docket Date, Name)

It deliberately IGNORES these (they hold no case names):
  - "Procedural Steps List for Docket Part 1 / 2"
  - "Motion Call Schedule First Half / Second Half"

Requirements: python3, the `requests` library, and `pdftotext` (poppler-utils).
Run it:   python3 ChanceryCourtCollector.py
"""

import os
import re
import csv
import sys
import subprocess
import tempfile
from urllib.parse import urljoin

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DOCKETS_PAGE = "https://www.hamiltontn.gov/ChanceryCourt_Dockets.aspx"
OUTPUT_CSV   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ChanceryCourt_Names.csv")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; docket-collector/1.0)"}
TIMEOUT = 30

# ----------------------------------------------------------------------------
# NAME PARSING RULES
# ----------------------------------------------------------------------------
# A name run = 2 to 4 capitalized tokens (first + optional middle/suffix + last).
NAMERUN = re.compile(r"^([A-Z][A-Z.'’\-]+(?:\s+[A-Z][A-Z.'’\-]+){1,3})")

# If ANY token is one of these, it's an organization, not an individual.
ENTITY = {
    "LLC", "INC", "CORP", "CORPORATION", "COMPANY", "CO", "BANK", "NA", "TRUST",
    "ASSOCIATION", "ASSN", "ASSOC", "LP", "LLP", "PLLC", "PC", "FOUNDATION",
    "CHURCH", "MINISTRIES", "DEPT", "DEPARTMENT", "AUTHORITY", "SERVICES",
    "SERVICE", "SYSTEMS", "SYSTEM", "GROUP", "HOLDINGS", "PROPERTIES",
    "PROPERTY", "MANAGEMENT", "MORTGAGE", "FINANCIAL", "FINANCE", "CREDIT",
    "UNION", "CAPITAL", "FUND", "FUNDING", "ENTERPRISES", "ENTERPRISE",
    "INVESTMENTS", "INVESTMENT", "REALTY", "CONSTRUCTION", "BUILDERS",
    "DEVELOPMENT", "SITE", "OUTDOOR", "ADVENTURES", "HOSPITAL", "HEALTH",
    "HEALTHCARE", "CENTER", "CENTRE", "UNIVERSITY", "COLLEGE", "SCHOOL",
    "BOARD", "COMMISSION", "AGENCY", "OFFICE", "DIVISION", "INSURANCE",
    "LENDING", "LOANS", "SOLUTIONS", "PARTNERS", "ASSOCIATES", "NATIONAL",
    "FEDERAL", "LTD", "HOA", "CONDOMINIUM", "CONDO", "APARTMENTS", "VENTURES",
    "LOGISTICS", "TRUCKING", "MOTORS", "RENTALS", "REVOCABLE", "IRREVOCABLE",
    "HOME", "HOMES", "FUNERAL", "TRANSFER", "BOTTLE", "SHOP", "STORE", "MARKET",
    "RESTAURANT", "GRILL", "CAFE", "SALON", "AUTO",
}

# If ANY token is one of these legal/docket/filler words, it isn't a person.
STOP = {
    # legal terms
    "MOTION", "MOTIONS", "JUDGMENT", "DEFAULT", "SETTLEMENT", "AGREEMENT",
    "ESTATE", "COMPLAINT", "ORDER", "DECREE", "PETITION", "SUMMONS", "DISMISS",
    "DISMISSAL", "SUMMARY", "HEARING", "CONTINUANCE", "FORECLOSURE",
    "PARTITION", "RECEIVER", "INJUNCTION", "CONTEMPT", "ATTORNEY", "ESQ",
    "GUARDIAN", "CONSERVATOR", "ADMINISTRATOR", "ADMINISTRATRIX", "EXECUTOR",
    "EXECUTRIX", "TRUSTEE", "PLAINTIFF", "PLAINTIFFS", "DEFENDANT",
    "DEFENDANTS", "RESPONDENT", "PETITIONER", "COUNTER", "REAL", "TITLE",
    "QUIET", "RELIEF", "DAMAGES", "BREACH", "CONTRACT", "NOTICE", "PROCESS",
    "CLERK", "MASTER", "DOCKET", "CASE", "VERSUS", "DISPOSITION", "DECEASED",
    "DECD", "MINOR", "MINORS", "AD", "LITEM", "DELINQUENT", "TAXPAYERS",
    "TAXPAYER", "BANKRUPTCY", "STATISTICS", "DISTRICT", "COUNTY", "CITY",
    "STATE",
    # docket headers / notes
    "TAX", "YEAR", "CHANCERY", "COURT", "PART", "PAGE", "VIA", "WEBEX",
    "DISTRICT", "DELINQUENT", "VITAL", "RECORDS", "RECORD", "LAW", "HEIRS",
    "HEIR", "UNKNOWN", "CLAIMANTS", "CLAIMANT", "PASSED", "PREVIOUS",
    "DISPOSITION", "CONTD",
    # connectors / filler (never part of a real personal name run)
    "OF", "IN", "IS", "AT", "FROM", "ANY", "FOR", "THE", "AND", "ET", "UX",
    "AL", "DBA", "AKA", "FKA", "NKA", "TO", "ON", "BY", "AS", "OR", "ALL",
}

# Last token = a street/place type (these are essentially never surnames).
PLACE_LAST = {
    "PARKWAY", "PKWY", "BOULEVARD", "BLVD", "HIGHWAY", "HWY", "PLAZA", "TOWER",
    "AVENUE", "AVE", "EXPRESSWAY", "FREEWAY", "DRIVE", "DR", "STREET", "ST",
    "ROAD", "RD", "LANE", "LN", "CIRCLE", "CIR", "TRAIL", "TRL", "PIKE",
    "TERRACE", "TER", "COURT", "CT", "PLACE", "PL",
}
# If ANY token is one of these, it's a place reference, not a person.
PLACE_ANY = {"CHATTANOOGA", "TENNESSEE"}


def is_person(name: str) -> bool:
    """True if `name` looks like an individual (not a company/place/legal phrase)."""
    toks = name.split()
    if len(toks) < 2:
        return False
    upper = [t.strip(".,'’-").upper() for t in toks]
    if any(t in ENTITY for t in upper):
        return False
    if any(t in STOP for t in upper):
        return False
    if any(t in PLACE_ANY for t in upper):
        return False
    if upper[-1] in PLACE_LAST:
        return False
    if len(upper[-1]) < 2:            # truncated, e.g. "LON B"
        return False
    return True


def extract_names(layout_text: str):
    """Pull party names from the LEFT column of a -layout docket dump."""
    names, seen = [], set()
    for raw in layout_text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent > 35:               # right column = attorneys/motions; skip
            continue
        line = raw.strip()
        line = re.sub(r"^\d[\d\-]*\s+", "", line)        # strip leading case no. FIRST
        line = re.split(r"\s{3,}", line)[0]              # then drop the right column
        line = re.sub(r"^(vs\.?|v\.?|and|&)\s+", "", line, flags=re.I)  # co-party
        line = line.strip()
        if not line:
            continue
        m = NAMERUN.match(line)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
        if not is_person(name):
            continue
        key = name.upper()
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


# ----------------------------------------------------------------------------
# PDF DISCOVERY + DOWNLOAD
# ----------------------------------------------------------------------------
ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def find_docket_pdfs(page_html: str):
    """Return {'Part 1': url, 'Part 2': url} for the CURRENT motion call dockets."""
    found = {}
    for href, inner in ANCHOR.findall(page_html):
        label = re.sub(r"<[^>]+>", " ", inner)
        label = re.sub(r"\s+", " ", label).strip().lower()
        if not label:
            continue
        # Skip the ones the client said NOT to use.
        if "procedural" in label or "schedule" in label:
            continue
        m = re.search(r"current motion call docket part\s*([12])", label)
        if m:
            found["Part " + m.group(1)] = urljoin(DOCKETS_PAGE, href)
    return found


def docket_date_from_url(url: str) -> str:
    """PDF files are named MMDDYY.pdf -> return YYYY-MM-DD (best effort)."""
    m = re.search(r"(\d{2})(\d{2})(\d{2})\.pdf", url)
    if m:
        mm, dd, yy = m.groups()
        return f"20{yy}-{mm}-{dd}"
    return ""


def pdf_to_text(pdf_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    finally:
        os.unlink(path)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    print(f"Opening dockets page: {DOCKETS_PAGE}")
    try:
        page = requests.get(DOCKETS_PAGE, headers=HEADERS, timeout=TIMEOUT)
        page.raise_for_status()
    except Exception as e:
        sys.exit(f"ERROR: could not load the dockets page: {e}")

    pdfs = find_docket_pdfs(page.text)
    if not pdfs:
        sys.exit("ERROR: could not find the 'Current Motion Call Docket' links. "
                 "The page layout may have changed.")

    rows = []
    for part in sorted(pdfs):                 # Part 1, then Part 2
        url = pdfs[part]
        date = docket_date_from_url(url)
        print(f"  {part}: {url}  (date {date or 'unknown'})")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            print(f"    !! download failed: {e}")
            continue
        text = pdf_to_text(resp.content)
        names = extract_names(text)
        print(f"    -> {len(names)} names")
        for n in names:
            rows.append((part, date, n))

    if not rows:
        sys.exit("ERROR: downloaded the PDFs but extracted no names.")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Docket", "Docket Date", "Name"])
        w.writerows(rows)

    print(f"\nDone. {len(rows)} names written to:\n  {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
