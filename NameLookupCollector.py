#!/usr/bin/env python3
"""
Owner Name -> Address collector   (Steven's "upload names" bot — the REVERSE of
OwnerLookupCollector, and the way chancery-court case names get an address)
==============================================================================

You give it a list of OWNER NAMES (last name first); for each one it:
  1. Searches the Hamilton County Trustee (tax) property site BY LAST NAME
     (Trustee_PropertySearch.aspx, SearchType=1) — the exact search the
     chancery-court collector uses to put an address on a case-party name.
  2. Matches the right person by FIRST name and reads the PROPERTY ADDRESS.
  3. Best-effort: opens that property's detail page to also pull the MAILING
     address (where the tax bill is sent — often the best mail-to address),
     so the campaign file comes out complete.
  4. Reports WHICH NAMES HAD ADDRESSES (a Status on every input name).

It writes THREE files:
  - NameLeads_Campaign.csv : EXACTLY Steven's campaign format —
        LastName, FirstName, MiddleName, Address, City, State, ZipCode, Campaign
        (Address = the MAILING address when found, else the property address.)
        One row per address found; a name with no match is simply not in here.
  - NameLeads_Full.csv     : one row per INPUT name (found or not) with the
        property address, the record owner, the mailing city/state/zip, and a
        Status — this is the "which names had addresses" review file.
  - NameLeads_Sheet.csv    : a tidy, de-dupable copy for the Google Sheet.

INPUT: a file (command-line arg, else names_input.csv). Accepts either:
  - a CSV with LastName / FirstName (+ optional MiddleName) columns — i.e.
    Steven's own campaign CSV re-uploaded; OR
  - a CSV with a "Name" column, or a plain .txt with one name per line, written
    "Last, First" (comma) or "Last First" (space) — last name first, the way
    the county site searches.

This uses the SAME public Trustee site as the other collectors — no login,
nothing that can get suspended.  Requires: python3 + requests.
Run:  python3 NameLookupCollector.py my_names.csv     (set MAX_NAMES=3 to test.)
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
SEARCH_URL  = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertySearch.aspx?SearchType=1"
DETAIL_URL  = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertyInfo.aspx?pmuid="
CAMPAIGN_CSV = os.path.join(HERE, "NameLeads_Campaign.csv")
FULL_CSV     = os.path.join(HERE, "NameLeads_Full.csv")
SHEET_CSV    = os.path.join(HERE, "NameLeads_Sheet.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
THROTTLE  = 2.5     # seconds between county requests (politeness / avoids timeouts)
TIMEOUT   = 30
MAX_NAMES = int(os.environ.get("MAX_NAMES", "0"))   # 0 = all; >0 = quick test
# Skip the (slower) mailing-address detail lookup — property address only.
NO_MAILING = os.environ.get("NO_MAILING", "") not in ("", "0", "false", "False")

# ----------------------------------------------------------------------------
# ASP.NET form helpers (same pattern as the other collectors)
# ----------------------------------------------------------------------------
HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.I)
NAME_ATTR = re.compile(r'name="([^"]*)"')
VAL_ATTR  = re.compile(r'value="([^"]*)"')
# A street address inside a results row (mirrors ChanceryCourtCollector.ADDR_RE).
ADDR_RE   = re.compile(
    r"\b(\d{1,6}(?:\s+[A-Z0-9][A-Z0-9.\-/]*){1,5}\s+"
    r"(?:LN|RD|DR|ST|AVE|CT|BLVD|PIKE|CIR|PL|TER|WAY|HWY|TRL|LOOP|XING|CV|PT|"
    r"RUN|ROW|PKWY|SQ|GLN|TRCE|PASS|BND|CRES))\b"
)


def decode(s):
    return html.unescape(s)


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
            fields[decode(nm.group(1))] = decode(val.group(1)) if val else ""
    return fields


# ----------------------------------------------------------------------------
# NAME SEARCH  (Trustee last-name search -> rows with pmuid + address + owner)
# ----------------------------------------------------------------------------
def fetch_name_page(session, query, cache):
    """POST a "Last First" name search; return (results_html, hit_network).

    The Trustee site wants the name typed "Last First" in the single box — its
    own on-page instruction is: "Jones Robert or, Jones Rob or, Jones R." (a bare
    surname may not match). We pass "{last} {first}". Cached per query so a
    repeated name only hits the county site once.
    """
    if query in cache:
        return cache[query], False
    getr = _req(session, "GET", SEARCH_URL)
    if not getr:
        return None, True
    payload = extract_hidden(getr.text)
    payload["ctl00$MainContent$txtLName"] = query
    payload["ctl00$MainContent$cmdLName_Search"] = "Search"
    postr = _req(session, "POST", SEARCH_URL, data=payload,
                 headers={"Referer": SEARCH_URL, "Origin": "https://tpti.hamiltontn.gov"})
    if not postr:
        return None, True
    cache[query] = postr.text
    return postr.text, True


def parse_matches(page, first):
    """Find the rows matching `first`. Return (matches, status).

    matches = [{pmuid, address, owner}]. Strong match = the first name appears in
    the owner text; weak match = same first initial (only used if there's no
    strong hit). Mirrors ChanceryCourtCollector.parse_results but also keeps each
    row's pmuid so we can open the detail page for the mailing address.
    """
    idx = page.find("ctl00_MainContent_dgrResults")
    if idx < 0:
        return [], "no match"
    grid = page[idx:]
    rows = re.split(r'<tr class="DG[A-Za-z]*Item"', grid)[1:]
    initial = first[0] if first else ""
    re_first = re.compile(r"\b" + re.escape(first) + r"\b") if first else None
    strong, weak = [], []
    for part in rows:
        pm = re.search(r"Trustee_PropertyInfo\.aspx\?pmuid=(\d+)", part)
        pmuid = pm.group(1) if pm else ""
        text = re.sub(r"\s+", " ", decode(re.sub(r"<[^>]+>", " ", part))).strip()
        am = ADDR_RE.search(text)
        if not am:
            continue
        address = re.sub(r"\s+", " ", am.group(1)).strip()
        owner = text[am.end():].strip()                  # "LAST FIRST ..." (Name column)
        toks = owner.split()
        rec = {"pmuid": pmuid, "address": address, "owner": owner}
        if not first:                                    # only a surname given -> take all
            strong.append(rec)
        elif re_first.search(owner):
            strong.append(rec)
        elif len(toks) >= 2 and re.match(r"[A-Z]", toks[1]) and toks[1][0] == initial:
            weak.append(rec)

    def uniq(lst):
        seen, out = set(), []
        for r in lst:
            if r["address"] not in seen:
                seen.add(r["address"]); out.append(r)
        return out

    S, W = uniq(strong), uniq(weak)
    if S:
        return S, ("matched" if len(S) == 1 else f"multiple ({len(S)}) - review")
    if W and len(W) <= 6:
        return W, f"possible ({len(W)} initial match{'es' if len(W) > 1 else ''}) - verify"
    if W:
        return [], f"common name ({len(W)} same-initial owners) - no confident match"
    return [], f"no match ({len(rows)} recs for surname)"


# ----------------------------------------------------------------------------
# DETAIL PAGE  (mailing address) — reused from OwnerLookupCollector, best-effort
# ----------------------------------------------------------------------------
def _cells(page):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    cells = re.findall(r"<(?:td|span)[^>]*>(.*?)</(?:td|span)>", text, re.S | re.I)
    out = []
    for c in cells:
        c = re.sub(r"<[^>]+>", " ", c)
        c = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in html.unescape(c).split("\n"))
        c = c.strip()
        if c:
            out.append(c)
    return out


def fetch_detail(session, pmuid):
    """Return {prop_addr, owners:[...], mailing_raw} for one property, or None."""
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
    owners = [o.strip() for o in re.split(r"\s{2,}|\n", owners_raw) if o.strip()]
    if not owners and owners_raw:
        owners = [owners_raw]
    return {"prop_addr": prop_addr, "owners": owners, "mailing_raw": mailing_raw}


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
# INPUT
# ----------------------------------------------------------------------------
def split_name_lastfirst(cell):
    """'Smith, John A' or 'Smith John A' -> (last, first, middle). Last name first."""
    cell = cell.strip()
    if "," in cell:
        a, b = cell.split(",", 1)
        last = a.strip()
        rest = b.strip().split()
    else:
        toks = cell.split()
        last = toks[0] if toks else ""
        rest = toks[1:]
    first = rest[0] if rest else ""
    middle = " ".join(rest[1:]) if len(rest) > 1 else ""
    return last, first, middle


def read_names(path):
    """Return [(last, first, middle, display)] from a CSV or text file."""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]

    # (a) structured campaign CSV with LastName / FirstName (+ MiddleName) columns
    if any("last" in c for c in header) and any("first" in c for c in header):
        li = next((i for i, c in enumerate(header) if "last" in c), None)
        fi = next((i for i, c in enumerate(header) if "first" in c), None)
        mi = next((i for i, c in enumerate(header) if "middle" in c), None)
        out = []
        for r in rows[1:]:
            last = r[li].strip() if li is not None and li < len(r) else ""
            first = r[fi].strip() if fi is not None and fi < len(r) else ""
            mid = r[mi].strip() if mi is not None and mi < len(r) else ""
            if last:
                disp = f"{last}, {first}".strip().strip(",").strip()
                out.append((last, first, mid, disp))
        return out

    # (b) a "Name" column, or plain lines, "Last, First" / "Last First"
    name_col, has_header = 0, False
    for i, c in enumerate(header):
        if "name" in c:
            name_col, has_header = i, True
            break
    data = rows[1:] if has_header else rows
    out = []
    for r in data:
        cell = (r[name_col] if name_col < len(r) else (r[0] if r else "")).strip()
        if not cell:
            continue
        last, first, mid = split_name_lastfirst(cell)
        if last:
            out.append((last, first, mid, cell))
    return out


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "names_input.csv")
    if not os.path.exists(inp):
        sys.exit(f"ERROR: input file not found: {inp}\n"
                 f"Pass a CSV (LastName/FirstName columns, or a Name column) or a "
                 f"text file with one name per line, written Last, First.")
    names = read_names(inp)
    if not names:
        sys.exit("ERROR: no names found in the input file.")
    if MAX_NAMES:
        names = names[:MAX_NAMES]
        print(f"(test mode: first {MAX_NAMES} names)")

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    print(f"Looking up {len(names)} names on the county property site …\n")

    surname_cache = {}
    campaign_rows, full_rows, sheet_rows = [], [], []
    hits = 0
    for last, first, mid, display in names:
        query = f"{last} {first}".strip().upper()      # site wants "Last First" in one box
        page, hit = fetch_name_page(session, query, surname_cache)
        if hit:
            time.sleep(THROTTLE)
        if page is None:
            full_rows.append([display, f"{last} {first}".strip(), "", "", "", "site error", ""])
            print(f"  {display[:32]:32} site error")
            continue
        matches, status = parse_matches(page, first.upper())
        if matches:
            hits += 1
        if not matches:
            full_rows.append([display, f"{last} {first}".strip(), "", "", "", status, ""])
            sheet_rows.append([f"{last} {first}".strip(), "", last.title(), first.title(),
                               mid.title(), "", "", "", "", status,
                               f"{display}|{last} {first} {mid}".strip()])
            print(f"  {display[:32]:32} {status}")
            continue

        for m in matches:
            street = city = state = zipc = ""
            owner_raw = m["owner"]
            prop_addr = m["address"]
            if m["pmuid"] and not NO_MAILING:
                detail = fetch_detail(session, m["pmuid"])
                time.sleep(THROTTLE)
                if detail:
                    street, city, state, zipc = parse_mailing(detail["mailing_raw"])
                    if detail["prop_addr"]:
                        prop_addr = detail["prop_addr"]
                    if detail["owners"]:
                        owner_raw = detail["owners"][0]
            camp_addr = street or prop_addr                  # prefer mailing, else property
            campaign_rows.append([last.title(), first.title(), mid.title(),
                                  camp_addr, city, state, zipc, ""])
            full_rows.append([display, f"{last} {first}".strip(), prop_addr,
                              owner_raw.title(), f"{city} {state} {zipc}".strip(), status, ""])
            sheet_rows.append([f"{last} {first}".strip(), prop_addr, last.title(),
                               first.title(), mid.title(), street, city, state, zipc, status,
                               f"{display}|{camp_addr}".strip()])
            print(f"  {display[:32]:32} -> {camp_addr[:34]:34} {status}")

    with open(CAMPAIGN_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["LastName", "FirstName", "MiddleName", "Address",
                    "City", "State", "ZipCode", "Campaign"])
        w.writerows(campaign_rows)
    with open(FULL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Input Name", "Search Name", "Property Address",
                    "Owner (from record)", "Mailing City/State/Zip", "Status", "Notes"])
        w.writerows(full_rows)
    with open(SHEET_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Search Name", "Property Address", "LastName", "FirstName",
                    "MiddleName", "Mailing Street", "City", "State", "Zip",
                    "Status", "Key"])
        w.writerows(sheet_rows)

    print(f"\nDone. {len(names)} names checked — {hits} had an address.")
    print(f"  Campaign CSV : {CAMPAIGN_CSV}  ({len(campaign_rows)} address rows)")
    print(f"  Full/review  : {FULL_CSV}")
    print(f"  Sheet copy   : {SHEET_CSV}")


if __name__ == "__main__":
    main()
