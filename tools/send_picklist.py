#!/usr/bin/env python3
"""
Show the numbered Register-of-Deeds pick-list IN the Telegram chat, so Steven can
read every document (with a title) and reply with the numbers he wants.

Reads RegisterDeeds_List.json (written by RegisterDeedsCollector.py). Also POSTs the
list to the bot's /savelist so a reply like "3 7 12" maps back to the right PDFs.

Env: BOT_TOKEN, CHAT_ID, WORKER_URL, DEEDS_KEY
"""
import os
import json
import requests

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "").strip()
WORKER = os.environ.get("WORKER_URL", "").rstrip("/")
KEY = os.environ.get("DEEDS_KEY", "")
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_SHOW = 200          # safety: don't flood the chat with thousands of lines


def send(text):
    requests.post(f"{API}/sendMessage", timeout=20, data={
        "chat_id": CHAT, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"})


def main():
    if not (TOKEN and CHAT):
        print("no token/chat — skipping"); return
    try:
        items = json.load(open("RegisterDeeds_List.json")).get("items", [])
    except Exception:
        print("no pick-list file"); return
    if not items:
        print("pick-list empty (calibration may be needed)"); return

    # 1) Let the bot remember the list (number -> document) for the reply.
    if WORKER and KEY:
        try:
            requests.post(f"{WORKER}/savelist", timeout=20,
                          headers={"X-Creds-Key": KEY, "Content-Type": "application/json"},
                          json={"chat_id": CHAT, "items": items})
        except Exception as e:
            print("savelist failed:", e)

    # 2) Show the full numbered list in the chat, split into messages under the limit.
    shown = items[:MAX_SHOW]
    header = "🏛️ <b>Recent deeds — pick the documents you want:</b>\n"
    chunk, count = header, 0
    for it in shown:
        line = f"<b>{it['n']}.</b> {it.get('title') or it.get('name')}\n"
        if len(chunk) + len(line) > 3500:
            send(chunk); chunk = ""
        chunk += line
        count += 1
    if chunk.strip():
        send(chunk)

    extra = f" (showing first {MAX_SHOW} of {len(items)})" if len(items) > MAX_SHOW else ""
    send('📎 Reply with the numbers you want — e.g. <code>3 7 12</code> — or <code>all</code>, '
         f'and I\'ll send those document PDFs.{extra}')
    print(f"sent {count} item(s) to the chat")


if __name__ == "__main__":
    main()
