#!/usr/bin/env python3
"""
Address -> Owner + Mailing Address collector  (Steven's "upload addresses" bot)
==============================================================================

You give it a list of PROPERTY ADDRESSES; for each one it:
  1. Searches the Hamilton County Trustee (tax) property site BY ADDRESS
     (Trustee_PropertySearch.aspx, SearchType=3).
  2. Opens the matched property's detail page (Trustee_PropertyInfo.aspx?pmuid=).
  3. Pulls the OWNER NAME(S), the PROPERTY ADDRESS, and the MAILING ADDRESS
     (where the tax bill is sent -- often different from the property, e.g. an
     out-of-state / absentee owner).
  4. Splits the owner name into Last / First / Middle and the mailing address
     into Address / City / State / ZipCode.

It writes TWO files:
  - OwnerLeads_Campaign.csv : EXACTLY the campaign format Steven uses --
        LastName, FirstName, MiddleName, Address, City, State, ZipCode, Campaign
        (Address = the owner's MAILING address, so mail reaches the owner.)
  - OwnerLeads_Full.csv     : the same rows plus property address, parcel, raw
        owner/mailing text, and a Status, for review.

INPUT: a file passed on the command line (or `owner_input_sample.csv` by default).
       Either a CSV with an "Address" column (optional City/State/Zip), or a plain
       text file with one address per line.

Requires: python3 + requests.   Run:  python3 OwnerLookupCollector.py my_addresses.csv
(Set MAX_ADDR=3 in the environment to test on just a few.)

This reuses the SAME Trustee site the other collectors use -- public, no login.
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
HERE        = os.path.dirname(os.path.abspath(__file__))
SEARCH_URL  = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertySearch.aspx?SearchType=3"
DETAIL_URL  = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertyInfo.aspx?pmuid="
BASE        = "https://tpti.hamiltontn.gov/AppFolder/"
CAMPAIGN_CSV = os.path.join(HERE, "OwnerLeads_Campaign.csv")
FULL_CSV     = os.path.join(HERE, "OwnerLeads_Full.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
THROTTLE  = 2.5     # seconds between county requests (politeness / avoids timeouts)
TIMEOUT   = 30
MAX_ADDR  = int(os.environ.get("MAX_ADDR", "0"))   # 0 = all; >0 = quick test

# ----------------------------------------------------------------------------
# NAME CLEANUP
# ----------------------------------------------------------------------------
# Tokens that mean "this is an organization, not a person".
ENTITY = {
    "LLC", "INC", "CORP", "CORPORATION", "COMPANY", "BANK", "NA", "TRUST",
    "ASSOCIATION", "ASSN", "LP", "LLP", "PLLC", "PC", "FOUNDATION", "CHURCH",
    "MINISTRIES", "DEPT", "AUTHORITY", "SERVICES", "GROUP", "HOLDINGS",
    "PROPERTIES", "MANAGEMENT", "MORTGAGE", "FINANCIAL", "CAPITAL", "FUND",
    "ENTERPRISES", "INVESTMENTS", "REALTY", "CONSTRUCTION", "BUILDERS",
    "DEVELOPMENT", "HOMES", "RENTALS", "PARTNERS", "ASSOCIATES", "LTD", "HOA",
    "CONDOMINIUM", "APARTMENTS", "VENTURES", "CITY", "COUNTY", "STATE", "BOARD",
}
# Trailing role words on tax-roll names (NOT part of the person's name).
ROLE = {
    "TR", "TTEE", "TRUSTEE", "CO", "EST", "ESTATE", "ET", "AL", "UX", "LIFE",
    "REM", "REMAINDER", "REV", "LIV", "JTRS", "JT", "RS", "DECD", "HEIRS",
    "HEIR", "ETAL", "ETUX", "TEN", "COM",
}
SUFFIX = {"JR", "SR", "II", "III", "IV", "V", "VI"}


def split_owners(owner_cells):
    """Split the detail page's owner string(s) into individual people.

    Handles joint owners joined by '&' (the second name shares the first's
    surname, e.g. 'EXUM JAMES F III & JENNIFER K' -> two Exums) and drops any
    'C/O' care-of prefix.
    """
    out = []
    for cell in owner_cells:
        cell = cell.strip()
        if not cell:
            continue
        surname = None
        for j, part in enumerate(re.split(r"\s*&\s*", cell)):
            part = re.sub(r"^C/?O\s+", "", part.strip(), flags=re.I).strip()
            if not part:
                continue
            if j == 0:
                surname = part.split()[0]
                out.append(part)
            else:                           # spouse/co-owner: prepend shared surname
                out.append(f"{surname} {part}" if surname else part)
    return out


def parse_owner_name(raw):
    """'MCCOY BILLY R II CO TR' -> (last, first, middle, suffix, company, flags)."""
    flags = []
    raw = re.sub(r"^C/?O\s+", "", raw, flags=re.I)
    toks = re.sub(r"[.,]", " ", raw.upper()).split()
    while toks and toks[-1] in ROLE:        # strip trailing TR / CO TR / ET AL FIRST
        toks.pop()
    if any(t in ENTITY for t in toks):      # ...then decide company vs person
        return ("", "", "", "", raw.strip().title(), ["company"])
    suffix = " ".join(t for t in toks if t in SUFFIX)
    core = [t for t in toks if t not in SUFFIX]
    if len(core) < 2:
        flags.append("name-verify")
    last   = core[0] if len(core) > 0 else ""
    first  = core[1] if len(core) > 1 else ""
    middle = " ".join(core[2:]) if len(core) > 2 else ""
    if len(core) > 4:
        flags.append("compound-verify")
    return (last.title(), first.title(), middle.title(), suffix, "", flags)


def parse_mailing(raw_with_newline):
    """'6181 MEGANS BAY DR\\nNAPLES FL, 34113' -> (street, city, state, zip)."""
    parts = [p.strip() for p in raw_with_newline.split("\n") if p.strip()]
    street = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    zipc = state = city = ""
    m = re.search(r"(\d{5}(?:-\d{4})?)\s*$", rest)
    if m:
        zipc = m.group(1)
        rest = rest[:m.start()].strip().rstrip(",").strip()
    toks = rest.split()
    if toks and len(toks[-1]) == 2 and toks[-1].isalpha():
        state = toks[-1].upper()
        city = " ".join(toks[:-1]).title()
    else:
        city = rest.title()
    return street.title(), city, state, zipc


# ----------------------------------------------------------------------------
# HTTP (same ASP.NET form pattern as the other collectors)
# ----------------------------------------------------------------------------
HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.I)
NAME_ATTR = re.compile(r'name="([^"]*)"')
VAL_ATTR  = re.compile(r'value="([^"]*)"')


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
    for tag in HIDDEN_RE.findall(page):
        nm = NAME_ATTR.search(tag)
        if nm:
            val = VAL_ATTR.search(tag)
            fields[html.unescape(nm.group(1))] = html.unescape(val.group(1)) if val else ""
    return fields


def search_address(session, address):
    """Return list of dicts {pmuid, parcel, prop_addr} matching the address."""
    getr = _req(session, "GET", SEARCH_URL)
    if not getr:
        return None                       # site error
    payload = extract_hidden(getr.text)
    payload["ctl00$MainContent$txtPropAddress"] = address
    payload["ctl00$MainContent$cmdPropAddress_Search"] = "Search"
    postr = _req(session, "POST", SEARCH_URL, data=payload,
                 headers={"Referer": SEARCH_URL, "Origin": "https://tpti.hamiltontn.gov"})
    if not postr:
        return None
    page = postr.text
    idx = page.find("dgrResults")
    if idx < 0:
        return []                         # no matches
    grid = page[idx:]
    out = []
    # each result row: a link Trustee_PropertyInfo.aspx?pmuid=NNN, with parcel + address text
    for m in re.finditer(r"Trustee_PropertyInfo\.aspx\?pmuid=(\d+)'>", grid):
        pmuid = m.group(1)
        # the visible row text after the link holds: parcel district address
        tail = re.sub(r"<[^>]+>", " ", grid[m.end():m.end() + 400])
        tail = re.sub(r"\s+", " ", html.unescape(tail)).strip()
        out.append({"pmuid": pmuid, "row": tail})
    # de-dup pmuids preserving order
    seen, uniq = set(), []
    for r in out:
        if r["pmuid"] not in seen:
            seen.add(r["pmuid"]); uniq.append(r)
    return uniq


def _cells(page):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)          # keep line breaks
    cells = re.findall(r"<(?:td|span)[^>]*>(.*?)</(?:td|span)>", text, re.S | re.I)
    out = []
    for c in cells:
        c = re.sub(r"<[^>]+>", " ", c)
        # collapse spaces but KEEP newlines (for mailing street vs city/state/zip)
        c = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in html.unescape(c).split("\n"))
        c = c.strip()
        if c:
            out.append(c)
    return out


def fetch_detail(session, pmuid):
    """Return {prop_addr, owners:[...], mailing_raw} for one property."""
    r = _req(session, "GET", DETAIL_URL + pmuid)
    if not r:
        return None
    cells = _cells(r.text)

    def after(label):
        for i, c in enumerate(cells):
            if c.split("\n")[0].strip().rstrip(":").lower() == label.lower():
                return cells[i + 1] if i + 1 < len(cells) else ""
        return ""

    prop_addr = after("Property Address").replace("\n", " ").strip()
    owners_raw = after("Owner Names").strip()
    mailing_raw = after("Mailing Address").strip()
    # Owner Names cell may pack multiple owners; the detail page joins co-owners.
    # Split on 2+ spaces or known separators into individual owner strings.
    owners = [o.strip() for o in re.split(r"\s{2,}|\n", owners_raw) if o.strip()]
    if not owners and owners_raw:
        owners = [owners_raw]
    return {"prop_addr": prop_addr, "owners": owners, "mailing_raw": mailing_raw}


# ----------------------------------------------------------------------------
# INPUT
# ----------------------------------------------------------------------------
def read_addresses(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    # Detect a header row (any cell containing "address") and the address column.
    addr_col, has_header = 0, False
    for i, cell in enumerate(rows[0]):
        if cell and "address" in cell.lower():
            addr_col, has_header = i, True
            break
    data = rows[1:] if has_header else rows
    return [r[addr_col].strip() for r in data
            if len(r) > addr_col and r[addr_col].strip()]


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "owner_input_sample.csv")
    if not os.path.exists(inp):
        sys.exit(f"ERROR: input file not found: {inp}\n"
                 f"Pass a CSV (with an 'Address' column) or a text file of addresses.")
    addresses = read_addresses(inp)
    if not addresses:
        sys.exit("ERROR: no addresses found in the input file.")
    if MAX_ADDR:
        addresses = addresses[:MAX_ADDR]
        print(f"(test mode: first {MAX_ADDR} addresses)")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    print(f"Looking up {len(addresses)} addresses on the county property site …\n")

    campaign_rows, full_rows = [], []
    for addr in addresses:
        results = search_address(session, addr)
        time.sleep(THROTTLE)
        if results is None:
            full_rows.append([addr, "", "", "", "", "", "", "", "", "site error"])
            print(f"  {addr[:38]:38} site error")
            continue
        if not results:
            full_rows.append([addr, "", "", "", "", "", "", "", "", "no match"])
            print(f"  {addr[:38]:38} no match")
            continue
        status = "matched" if len(results) == 1 else f"multiple ({len(results)}) - review"
        for res in results:
            detail = fetch_detail(session, res["pmuid"])
            time.sleep(THROTTLE)
            if not detail:
                continue
            street, city, state, zipc = parse_mailing(detail["mailing_raw"])
            for owner in split_owners(detail["owners"]):
                last, first, middle, suffix, company, flags = parse_owner_name(owner)
                name_last = company if company else last
                campaign_rows.append([name_last, first, middle, street, city, state, zipc, ""])
                full_rows.append([
                    addr, detail["prop_addr"], owner.strip().title(),
                    (name_last + (f" {suffix}" if suffix else "")).strip(),
                    first, middle, street, f"{city} {state} {zipc}".strip(),
                    "; ".join(flags), status,
                ])
            print(f"  {addr[:38]:38} -> {detail['owners'][0][:26]:26} mail: {city} {state} {zipc}")

    with open(CAMPAIGN_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["LastName", "FirstName", "MiddleName", "Address",
                    "City", "State", "ZipCode", "Campaign"])
        w.writerows(campaign_rows)
    with open(FULL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Input Address", "Property Address", "Owner (raw)",
                    "LastName", "FirstName", "MiddleName",
                    "Mailing Street", "Mailing City/State/Zip", "Notes", "Status"])
        w.writerows(full_rows)

    print(f"\nDone. {len(campaign_rows)} owner rows.")
    print(f"  Campaign CSV : {CAMPAIGN_CSV}")
    print(f"  Full/review  : {FULL_CSV}")


if __name__ == "__main__":
    main()
