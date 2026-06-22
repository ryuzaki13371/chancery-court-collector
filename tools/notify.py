#!/usr/bin/env python3
"""
Send a results file to Telegram.

Recipients:
  - If CHAT_ID is set (an on-demand button tap) -> send only to that chat.
  - Otherwise (a scheduled weekly run) -> send to every chat in subscribers.txt.
  - If subscribers.txt is empty/missing, fall back to DEFAULT_CHAT_ID (if set).

Env:
  BOT_TOKEN        Telegram bot token                       (required)
  FILE             file(s) to send, comma-separated         (required)
  CAPTION          caption text (shown on the first file)   (optional)
  CHAT_ID          single recipient                         (optional; on-demand runs)
  DEFAULT_CHAT_ID  fallback recipient                       (optional)
"""
import os
import sys
import requests

TOKEN = os.environ["BOT_TOKEN"]
FILES = [p.strip() for p in os.environ["FILE"].split(",") if p.strip()]
CAPTION = os.environ.get("CAPTION", "")
API = f"https://api.telegram.org/bot{TOKEN}/sendDocument"


def recipients():
    one = os.environ.get("CHAT_ID", "").strip()
    if one:
        return [one]
    subs = []
    if os.path.exists("subscribers.txt"):
        with open("subscribers.txt") as f:
            subs = [ln.strip() for ln in f if ln.strip()]
    if subs:
        return subs
    fallback = os.environ.get("DEFAULT_CHAT_ID", "").strip()
    return [fallback] if fallback else []


def main():
    targets = recipients()
    if not targets:
        print("No recipients (no CHAT_ID, empty subscribers.txt, no DEFAULT_CHAT_ID).")
        return
    sent, failed = 0, 0
    for chat in targets:
        ok_all = True
        for i, path in enumerate(FILES):
            if not os.path.exists(path):
                continue
            data = {"chat_id": chat}
            if i == 0 and CAPTION:
                data["caption"] = CAPTION          # caption on the first (the PDF)
            with open(path, "rb") as fh:
                r = requests.post(API, data=data, files={"document": fh}, timeout=60)
            if not (r.ok and r.json().get("ok")):
                ok_all = False
                print(f"  failed for {chat} ({path}): {r.status_code} {r.text[:120]}")
        sent += ok_all
        failed += (not ok_all)
    print(f"Delivered to {sent} chat(s); {failed} failed.")


if __name__ == "__main__":
    main()
