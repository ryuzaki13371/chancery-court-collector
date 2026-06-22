# Lead Collector bot — how it's deployed

The bot runs free on **Cloudflare Workers** (serverless, always-on, no VPS).

```
user taps a button / menu command → Cloudflare Worker → triggers a GitHub Action
        → the collector runs → the CSV is sent back to that chat on Telegram
```

Pressing **/start** also subscribes that chat to the **weekly auto-delivery**
(via `subscribe.yml`, which keeps `subscribers.txt`). Anyone can use the bot —
just search its username on Telegram and press Start.

---

## Files
- `worker.js` — the bot logic (this folder)
- `wrangler.toml` — Worker config (name, the `GH_REPO` variable)

## Deploy / redeploy (from this folder)
```
export CLOUDFLARE_API_TOKEN=...      # token with "Workers Scripts: Edit"
export CLOUDFLARE_ACCOUNT_ID=72919424b1ed06727230cb4fa444bd2b
npx wrangler deploy
```

## Secrets (set once, stored encrypted on the Worker)
```
echo "<telegram bot token>"   | npx wrangler secret put BOT_TOKEN
echo "<github fine-grained>"  | npx wrangler secret put GH_TOKEN      # Actions: read & write on the repo
echo "<random string>"        | npx wrangler secret put WEBHOOK_SECRET
```
`GH_REPO` is a plain var in `wrangler.toml` (not secret).

## Connect Telegram to the Worker (once)
```
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://lead-collector-bot.<account>.workers.dev" \
  -d "secret_token=<WEBHOOK_SECRET>"
```

## Bot commands (shown in the ☰ menu)
- `/start` — show the menu **and** subscribe to weekly auto-delivery
- `/dockets` — pull the latest court docket names now
- `/obituaries` — pull the latest obituary → address list now
- `/stop` — unsubscribe from the weekly auto-delivery

## GitHub secrets the Actions need
- `TELEGRAM_TOKEN` — the bot token (for sending files)
- `TELEGRAM_CHAT_ID` — fallback recipient if `subscribers.txt` is empty
