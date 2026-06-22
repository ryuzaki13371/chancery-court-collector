/**
 * Lead Collector — Telegram button bot (Cloudflare Worker)
 * --------------------------------------------------------
 * Free, serverless, always-on (no VPS). Listens for taps/commands and triggers
 * the GitHub Actions, which scrape and send the file back to Telegram.
 *
 * Anyone can use it: searching the bot, pressing Start, and tapping a button
 * works for any Telegram user. Pressing Start also subscribes that person to
 * the weekly auto-delivery (handled by the subscribe.yml workflow).
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

// Fire a GitHub Actions workflow_dispatch (used to run collectors and to subscribe).
function dispatch(env, workflow, inputs) {
  return fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "lead-collector-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    }
  );
}

async function onMessage(msg, env) {
  const chatId = msg.chat.id;
  const cmd = (msg.text || "").trim().toLowerCase().split("@")[0];
  if (cmd === "/start" || cmd === "/menu") {
    await sendMenu(env, chatId);
    // Subscribe this chat to the weekly auto-delivery (fire and forget).
    const who = [msg.chat.first_name, msg.chat.username].filter(Boolean).join(" @");
    await dispatch(env, "subscribe.yml", { chat_id: String(chatId), name: who || "" });
  } else if (cmd === "/dockets") {
    await trigger(env, "dockets", chatId);
  } else if (cmd === "/obituaries") {
    await trigger(env, "obituaries", chatId);
  } else if (cmd === "/stop") {
    await dispatch(env, "subscribe.yml", { chat_id: String(chatId), name: "", action: "remove" });
    await tg(env, "sendMessage", { chat_id: chatId, text: "🔕 You're unsubscribed from the weekly auto-send. Tap /start to rejoin." });
  } else {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Send /start to see the menu." });
  }
}

async function onCallback(cq, env) {
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id }); // stop the spinner
  await trigger(env, cq.data, cq.message.chat.id);
}

function sendMenu(env, chatId) {
  return tg(env, "sendMessage", {
    chat_id: chatId,
    text: "👋 *Lead Collector*\n\nTap a button (or use the ☰ menu) to pull the latest list — it arrives here as a file in a minute or two. You'll also get a fresh copy automatically every week.",
    parse_mode: "Markdown",
    reply_markup: {
      inline_keyboard: [
        [{ text: "📋 Court Dockets",        callback_data: "dockets" }],
        [{ text: "🏠 Obituary → Addresses", callback_data: "obituaries" }],
      ],
    },
  });
}

async function trigger(env, choice, chatId) {
  const wf = WORKFLOWS[choice];
  if (!wf) return;
  const label = choice === "dockets" ? "court docket names" : "obituary → property addresses";
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `⏳ Pulling the latest ${label}… a minute or two, then the file lands here.`,
  });
  const resp = await dispatch(env, wf, { chat_id: String(chatId) });
  if (!resp.ok) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⚠️ Couldn't start the job (GitHub returned ${resp.status}). Check the bot's GH_TOKEN / GH_REPO.`,
    });
  }
}
