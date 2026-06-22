/**
 * Lead Collector — Telegram button bot (Cloudflare Worker)
 * --------------------------------------------------------
 * Free, serverless, always-on (no VPS). It listens for taps and triggers the
 * GitHub Actions, which do the scraping and send the file back to Telegram.
 *
 * Flow:  user taps a button  ->  this Worker  ->  GitHub Action runs  ->
 *        CSV is delivered to the chat that tapped.
 *
 * Set these in the Worker's Settings -> Variables (see DEPLOY.md):
 *   BOT_TOKEN       (secret)  Telegram bot token from @BotFather
 *   GH_TOKEN        (secret)  GitHub token with "Actions: read & write" on the repo
 *   GH_REPO         (text)    e.g. ryuzaki13371/chancery-court-collector
 *   WEBHOOK_SECRET  (secret)  any random string; must match the Telegram webhook
 */

const WORKFLOWS = {
  dockets:    "weekly.yml",       // Task B: court docket names
  obituaries: "obituaries.yml",   // Task A: obituary -> property addresses
};

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Lead Collector bot is running.");

    // Only accept calls that carry Telegram's secret header.
    if (env.WEBHOOK_SECRET &&
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }

    try {
      if (update.message && update.message.text) {
        await onMessage(update.message, env);
      } else if (update.callback_query) {
        await onCallback(update.callback_query, env);
      }
    } catch (_) { /* swallow so Telegram doesn't retry forever */ }

    return new Response("ok");
  },
};

function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function onMessage(msg, env) {
  const chatId = msg.chat.id;
  const text = (msg.text || "").trim().toLowerCase();
  if (text === "/start" || text === "/menu") {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "👋 *Lead Collector*\n\nTap a button to pull the latest list. It arrives here as a file in a minute or two.",
      parse_mode: "Markdown",
      reply_markup: {
        inline_keyboard: [
          [{ text: "📋 Court Dockets",          callback_data: "dockets" }],
          [{ text: "🏠 Obituary → Addresses",   callback_data: "obituaries" }],
        ],
      },
    });
  } else {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Send /start to see the menu." });
  }
}

async function onCallback(cq, env) {
  const chatId = cq.message.chat.id;
  const wf = WORKFLOWS[cq.data];
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id }); // stop the spinner

  if (!wf) return;
  const label = cq.data === "dockets" ? "court docket names" : "obituary → property addresses";
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `⏳ Pulling the latest ${label}… this takes a minute or two, then the file lands here.`,
  });

  // Trigger the GitHub Action, telling it which chat to deliver to.
  const resp = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${wf}/dispatches`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "lead-collector-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { chat_id: String(chatId) } }),
    }
  );

  if (!resp.ok) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⚠️ Couldn't start the job (GitHub returned ${resp.status}). Check the bot's GH_TOKEN / GH_REPO.`,
    });
  }
}
