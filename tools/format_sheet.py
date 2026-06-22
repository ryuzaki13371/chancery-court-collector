#!/usr/bin/env python3
"""
Make a Google Sheet look professional: bold navy header, frozen header row,
auto-sized columns, alternating row stripes, and a filter. Also deletes any
empty default "Sheet1". Safe to re-run.

Env: GCP_SA_KEY (json or path), SHEET_ID
"""
import os
import json
import requests
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

API = "https://sheets.googleapis.com/v4/spreadsheets"
NAVY = {"red": 0.12, "green": 0.31, "blue": 0.47}
WHITE = {"red": 1, "green": 1, "blue": 1}
STRIPE = {"red": 0.93, "green": 0.96, "blue": 0.99}


def token():
    raw = os.environ["GCP_SA_KEY"]
    info = json.load(open(raw)) if os.path.exists(raw) else json.loads(raw)
    c = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    c.refresh(gtr.Request())
    return c.token


def main():
    sid = os.environ["SHEET_ID"].strip()
    H = {"Authorization": f"Bearer {token()}"}
    meta = requests.get(f"{API}/{sid}", params={"fields": "sheets.properties"},
                        headers=H, timeout=30).json()
    reqs = []
    for s in meta["sheets"]:
        p = s["properties"]
        gid, title = p["sheetId"], p["title"]
        cols = p.get("gridProperties", {}).get("columnCount", 8)
        rows = p.get("gridProperties", {}).get("rowCount", 1)

        # Delete an empty leftover default tab.
        if title.lower() == "sheet1":
            vals = requests.get(f"{API}/{sid}/values/{title}!A1:A2", headers=H, timeout=30
                                ).json().get("values", [])
            if not vals:
                reqs.append({"deleteSheet": {"sheetId": gid}})
                continue

        # Freeze header row.
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})
        # Header style: navy fill, white bold text.
        reqs.append({"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": NAVY,
                "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 10},
                "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}})
        # Alternating row stripes.
        reqs.append({"addBanding": {"bandedRange": {
            "range": {"sheetId": gid, "startRowIndex": 0},
            "rowProperties": {"headerColor": NAVY, "firstBandColor": WHITE,
                              "secondBandColor": STRIPE}}}})
        # Auto-size columns.
        reqs.append({"autoResizeDimensions": {"dimensions": {
            "sheetId": gid, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": cols}}})
        # Basic filter over the data.
        reqs.append({"setBasicFilter": {"filter": {"range": {
            "sheetId": gid, "startRowIndex": 0, "endColumnIndex": cols}}}})

    # addBanding fails if a banding already exists; run requests one-by-one and
    # skip the ones that are already applied, so this stays re-runnable.
    ok = 0
    for r in reqs:
        resp = requests.post(f"{API}/{sid}:batchUpdate", headers=H, timeout=30,
                             json={"requests": [r]})
        if resp.ok:
            ok += 1
        elif "already" not in resp.text.lower() and "banding" not in resp.text.lower():
            print("  skip:", list(r)[0], resp.status_code, resp.text[:90])
    print(f"formatting applied ({ok} ops)")


if __name__ == "__main__":
    main()
