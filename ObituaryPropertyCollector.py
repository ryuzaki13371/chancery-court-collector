#!/usr/bin/env python3
"""
Obituary -> Property Address collector  (Task A)
================================================
1. Scrapes obituary names from chattanoogan.com/obituaries
2. Reformats each name to "Last First"
3. Searches the Hamilton County Trustee property site by last name
4. Matches the right person by first name and grabs the address
5. Writes everything to Obituary_Addresses.csv

This is the Python twin of ObituaryPropertyCollector.gs, so it can run on a
free GitHub Action (no Google account, no VPS) and deliver to Telegram.

Requires: python3 + requests.   Run:  python3 ObituaryPropertyCollector.py
(Set MAX_NAMES=3 in the environment to test on just a few names.)
"""

import os
import re
import csv
import sys
import time
import html
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
OBIT_URL    = "https://www.chattanoogan.com/obituaries/"
TRUSTEE_URL = "https://tpti.hamiltontn.gov/AppFolder/Trustee_PropertySearch.aspx?SearchType=1"
OUTPUT_CSV  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Obituary_Addresses.csv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
THROTTLE = 2.5     # seconds between county searches (politeness / avoids timeouts)
TIMEOUT  = 30
MAX_NAMES = int(os.environ.get("MAX_NAMES", "0"))   # 0 = all; >0 = quick test

# ----------------------------------------------------------------------------
# REGEXES
# ----------------------------------------------------------------------------
OBIT_RE  = re.compile(r"obit-item clickable\"\s*onclick=\"document\.location\.href='([^']+)'[\s\S]*?<h5>([^<]+)</h5>")
DATE_RE  = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})/")
HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.I)
NAME_ATTR = re.compile(r'name="([^"]*)"')
VAL_ATTR  = re.compile(r'value="([^"]*)"')
SUFFIX_RE = re.compile(r"\b(JR|SR|II|III|IV|V|VI|MD|PHD|DDS|ESQ|REV|DR)\b", re.I)
ADDR_RE   = re.compile(
    r"\b(\d{1,6}(?:\s+[A-Z0-9][A-Z0-9.\-/]*){1,5}\s+"
    r"(?:LN|RD|DR|ST|AVE|CT|BLVD|PIKE|CIR|PL|TER|WAY|HWY|TRL|LOOP|XING|CV|PT|"
    r"RUN|ROW|PKWY|SQ|GLN|TRCE|PASS|BND|CRES))\b"
)


def decode(s):
    return html.unescape(s)


# ----------------------------------------------------------------------------
# 1) OBITUARIES
# ----------------------------------------------------------------------------
def fetch_obituaries(session):
    r = session.get(OBIT_URL, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for m in OBIT_RE.finditer(r.text):
        link = m.group(1)
        if not link.startswith("http"):
            link = "https://www.chattanoogan.com" + link
        dm = DATE_RE.search(link)
        date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
        out.append({"link": link, "rawName": decode(m.group(2)).strip(), "date": date})
    return out


# ----------------------------------------------------------------------------
# 2) NAME REFORMAT  ("First Middle Last Jr." -> "Last First")
# ----------------------------------------------------------------------------
def reformat_name(raw):
    flags = []
    if re.search(r"[“”‘’\"]", raw):
        flags.append("nickname-verify")
    s = re.sub(r"[“”‘’\"][^“”‘’\"]*[“”‘’\"]", " ", raw)   # drop "nickname"
    s = re.sub(r"\([^)]*\)", " ", s)                       # drop (nee ...)
    s = re.sub(r"[.,]", " ", s)
    s = SUFFIX_RE.sub(" ", s)
    toks = [t for t in s.split() if re.search(r"[A-Za-z]", t)]
    core = [t for t in toks if len(t) > 1]                 # drop middle initials
    if len(core) < 2:
        core = toks
    if len(core) < 2:
        return None
    if len(core) >= 4:
        flags.append("compound-verify")
    return {
        "last":  core[-1].upper(),
        "first": core[0].upper(),
        "display": f"{core[-1]} {core[0]}",
        "flags": flags,
    }


# ----------------------------------------------------------------------------
# 3) TRUSTEE PROPERTY LOOKUP
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
    for tag in HIDDEN_RE.findall(page):
        nm = NAME_ATTR.search(tag)
        if nm:
            val = VAL_ATTR.search(tag)
            fields[decode(nm.group(1))] = decode(val.group(1)) if val else ""
    return fields


def lookup_address(session, last, first):
    getr = _req(session, "GET", TRUSTEE_URL)
    if not getr:
        return [], "site error (get)"
    payload = extract_hidden(getr.text)
    payload["ctl00$MainContent$txtLName"] = last
    payload["ctl00$MainContent$cmdLName_Search"] = "Search"
    postr = _req(session, "POST", TRUSTEE_URL, data=payload,
                 headers={"Referer": TRUSTEE_URL, "Origin": "https://tpti.hamiltontn.gov"})
    if not postr:
        return [], "site error (post)"
    return parse_results(postr.text, first)


def parse_results(page, first):
    idx = page.find("ctl00_MainContent_dgrResults")
    if idx < 0:
        return [], "no match"
    grid = page[idx:]
    rows = re.split(r'<tr class="DG[A-Za-z]*Item"', grid)[1:]
    initial = first[0] if first else ""
    re_first = re.compile(r"\b" + re.escape(first) + r"\b")
    strong, weak = [], []
    for part in rows:
        text = re.sub(r"\s+", " ", decode(re.sub(r"<[^>]+>", " ", part))).strip()
        am = ADDR_RE.search(text)
        if not am:
            continue
        address = re.sub(r"\s+", " ", am.group(1)).strip()
        owner = text[am.end():].strip()          # "LAST FIRST ..." (Name column)
        toks = owner.split()
        if re_first.search(owner):
            strong.append(address)
        elif len(toks) >= 2 and re.match(r"[A-Z]", toks[1]) and toks[1][0] == initial:
            weak.append(address)

    def uniq(a):
        out = []
        for x in a:
            if x not in out:
                out.append(x)
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
# MAIN
# ----------------------------------------------------------------------------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print(f"Reading obituaries: {OBIT_URL}")
    obits = fetch_obituaries(session)
    print(f"Found {len(obits)} obituaries")
    if MAX_NAMES:
        obits = obits[:MAX_NAMES]
        print(f"(test mode: only first {MAX_NAMES})")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for o in obits:
        nm = reformat_name(o["rawName"])
        if not nm:
            rows.append([today, o["date"], o["rawName"], "", "", "", "Name unclear", "", o["link"]])
            continue
        addrs, status = lookup_address(session, nm["last"], nm["first"])
        primary = addrs[0] if addrs else ""                  # one clean address per row
        others = "; ".join(addrs[1:]) if len(addrs) > 1 else ""
        rows.append([today, o["date"], o["rawName"], nm["display"],
                     primary, others, friendly_status(status), ", ".join(nm["flags"]), o["link"]])
        print(f"  {o['rawName'][:32]:32} -> {nm['display'][:22]:22} {status}")
        time.sleep(THROTTLE)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Collected On", "Obit Date", "Obituary Name", "Search Name (Last First)",
                    "Property Address", "Other Possible Addresses", "Match", "Notes", "Source Link"])
        w.writerows(rows)
    print(f"\nDone. {len(rows)} rows written to:\n  {OUTPUT_CSV}")


def friendly_status(s):
    """Turn the internal status code into plain English for the sheet."""
    if s.startswith("matched"):
        return "Matched"
    if s.startswith("multiple"):
        return "Multiple — review"
    if s.startswith("possible"):
        return "Possible — verify"
    if s.startswith("common name"):
        return "Common name — skipped"
    if s.startswith("no match"):
        return "No match"
    if s.startswith("site error"):
        return "Site error — will retry"
    return s


if __name__ == "__main__":
    main()
