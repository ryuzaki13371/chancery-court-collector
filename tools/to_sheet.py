#!/usr/bin/env python3
"""
Append a results CSV into a Google Sheet tab (deduplicated, growing list).

- One tab per task (e.g. "Obituaries", "Chancery Court", "Tax Sale").
- Creates the tab + header row if it doesn't exist yet.
- Appends only rows whose KEY column isn't already in the sheet, so weekly
  runs grow the list without duplicating earlier leads.

Env:
  GCP_SA_KEY   service-account JSON (the whole thing) OR a path to the .json
  SHEET_ID     the spreadsheet id (from its URL)
  CSV          path to the results CSV
  TAB          tab/sheet name to write into
  KEY_COL      0-based index of the column that uniquely identifies a row
"""
import csv
import json
import os
import sys

from google.oauth2 import service_account
import google.auth.transport.requests as gtr
import requests

API = "https://sheets.googleapis.com/v4/spreadsheets"


def creds_token():
    raw = os.environ["GCP_SA_KEY"]
    info = json.load(open(raw)) if os.path.exists(raw) else json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(gtr.Request())
    return creds.token


def main():
    sheet_id = os.environ["SHEET_ID"]
    tab = os.environ["TAB"]
    key_col = int(os.environ.get("KEY_COL", "0"))
    rows = list(csv.reader(open(os.environ["CSV"], encoding="utf-8")))
    if not rows:
        print("empty CSV — nothing to write")
        return
    header, body = rows[0], rows[1:]

    tok = creds_token()
    H = {"Authorization": f"Bearer {tok}"}

    # 1) Ensure the tab exists.
    meta = requests.get(f"{API}/{sheet_id}", params={"fields": "sheets.properties.title"},
                        headers=H, timeout=30)
    meta.raise_for_status()
    tabs = [s["properties"]["title"] for s in meta.json().get("sheets", [])]
    if tab not in tabs:
        requests.post(f"{API}/{sheet_id}:batchUpdate", headers=H, timeout=30,
                      json={"requests": [{"addSheet": {"properties": {"title": tab}}}]}
                      ).raise_for_status()
        print(f"created tab '{tab}'")

    # 2) Read existing rows to find which keys are already there.
    existing = requests.get(f"{API}/{sheet_id}/values/{tab}!A1:ZZ",
                            headers=H, timeout=30).json().get("values", [])
    seen = set()
    for r in existing[1:]:                       # skip header
        if key_col < len(r):
            seen.add(r[key_col].strip())

    to_write = []
    if not existing:                             # brand-new tab -> include header
        to_write.append(header)
    new = [r for r in body if not (key_col < len(r) and r[key_col].strip() in seen)]
    to_write.extend(new)

    if not to_write or (len(to_write) == 1 and to_write[0] == header and not new):
        print(f"'{tab}': nothing new to add ({len(seen)} already there)")
        return

    # 3) Append.
    requests.post(f"{API}/{sheet_id}/values/{tab}!A1:append",
                  headers=H, timeout=30,
                  params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                  json={"values": to_write}).raise_for_status()
    print(f"'{tab}': added {len(new)} new row(s); {len(seen)} were already there")


if __name__ == "__main__":
    main()
