# Deploy the Telegram button-bot (free, ~10 minutes)

This makes the bot interactive: the user taps **Start**, sees buttons, taps one,
and the latest list arrives in their chat. It runs free on Cloudflare Workers —
no server, no VPS.

```
user taps button → Cloudflare Worker → triggers GitHub Action → file sent to that chat
```

## You need
- A free **Cloudflare** account (https://dash.cloudflare.com/sign-up)
- A **GitHub token** for the bot to start the Actions (steps below)

---

## Step 1 — Make the GitHub token (for the bot)
1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. **Repository access:** Only select repositories → `chancery-court-collector`
3. **Permissions → Repository permissions → Actions:** set to **Read and write**
4. Generate, copy the token (starts with `github_pat_…`). This one is *minimal* —
   it can only start Actions on this one repo, nothing else.

## Step 2 — Create the Worker
1. Cloudflare dashboard → **Workers & Pages → Create → Worker**
2. Name it `lead-collector-bot` → **Deploy** (the default hello-world)
3. **Edit code** → delete everything → paste the contents of `worker.js` → **Deploy**
4. Copy the Worker URL (looks like `https://lead-collector-bot.<you>.workers.dev`)

## Step 3 — Add the variables
Worker → **Settings → Variables and Secrets** → add these, then **Deploy**:

| Name | Type | Value |
|------|------|-------|
| `BOT_TOKEN` | Secret | your Telegram bot token |
| `GH_TOKEN` | Secret | the `github_pat_…` from Step 1 |
| `GH_REPO` | Text | `ryuzaki13371/chancery-court-collector` |
| `WEBHOOK_SECRET` | Secret | any random string (e.g. mash the keyboard) |

## Step 4 — Point Telegram at the Worker
Send me the **Worker URL** and the **WEBHOOK_SECRET** and I'll connect it, or run
this yourself (fill in the three values):

```
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=<WORKER_URL>" \
  -d "secret_token=<WEBHOOK_SECRET>"
```

## Done
Open the bot, send **/start**, tap a button — the file arrives in a minute or two.
Scheduled weekly runs keep working exactly as before; this just adds on-demand taps.
