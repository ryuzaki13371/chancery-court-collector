#!/usr/bin/env python3
"""
Send a results file to Telegram.

Recipients:
  - If CHAT_ID is set (an on-demand button tap) -> send only to that chat.
  - Otherwise (a scheduled weekly run) -> send to every chat in subscribers.txt.
  - If subscribers.txt is empty/missing, fall back to DEFAULT_CHAT_ID (if set).

Env:
  BOT_TOKEN        Telegram bot token            (required)
  FILE             path to the file to send      (required)
  CAPTION          caption text                  (optional)
  CHAT_ID          single recipient              (optional; on-demand runs)
  DEFAULT_CHAT_ID  fallback recipient            (optional)
"""
import os
import sys
import requests

TOKEN = os.environ["BOT_TOKEN"]
FILE = os.environ["FILE"]
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
        with open(FILE, "rb") as fh:
            r = requests.post(API, data={"chat_id": chat, "caption": CAPTION},
                              files={"document": fh}, timeout=60)
        if r.ok and r.json().get("ok"):
            sent += 1
        else:
            failed += 1
            print(f"  failed for {chat}: {r.status_code} {r.text[:120]}")
    print(f"Delivered to {sent} chat(s); {failed} failed.")


if __name__ == "__main__":
    main()
