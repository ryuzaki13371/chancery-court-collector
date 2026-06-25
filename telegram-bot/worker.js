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
  taxsale:    "taxsale.yml",      // Task C: delinquent tax sale -> owners
  owners:     "owners.yml",       // Task D: uploaded addresses -> owner + mailing address
  deeds:      "deeds.yml",        // Task E: Register of Deeds recent affidavit names+addresses
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Private endpoint: the deeds.yml GitHub Action fetches Steven's saved login
    // from here (protected by DEEDS_KEY). Returns {user, pass} JSON or 404.
    if (url.pathname === "/creds") {
      const key = request.headers.get("X-Creds-Key") || url.searchParams.get("key") || "";
      if (!env.DEEDS_KEY || key !== env.DEEDS_KEY) return new Response("forbidden", { status: 403 });
      const saved = env.CREDS ? await env.CREDS.get("deeds") : null;
      if (!saved) return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
      return new Response(saved, { headers: { "Content-Type": "application/json" } });
    }

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
      } else if (update.message && update.message.document) {
        await onDocument(update.message, env);
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
  } else if (cmd === "/help") {
    await sendMenu(env, chatId);
  } else if (cmd === "/dockets") {
    await trigger(env, "dockets", chatId);
  } else if (cmd === "/obituaries") {
    await trigger(env, "obituaries", chatId);
  } else if (cmd === "/taxsale") {
    await trigger(env, "taxsale", chatId);
  } else if (cmd === "/owners") {
    await askForAddresses(env, chatId);
  } else if (cmd === "/deeds") {
    await triggerDeeds(env, chatId);
  } else if (cmd === "/deedslogin") {
    await saveDeedsLogin(env, msg);
  } else if (cmd === "/setup" || cmd === "/login") {
    await askForDeedsLogin(env, chatId);
  } else if (cmd === "/stop") {
    await dispatch(env, "subscribe.yml", { chat_id: String(chatId), name: "", action: "remove" });
    await tg(env, "sendMessage", { chat_id: chatId, text: "🔕 You're unsubscribed from the weekly auto-send. Tap /start to rejoin." });
  } else {
    await tg(env, "sendMessage", { chat_id: chatId, text: "Send /start to see the menu." });
  }
}

async function onCallback(cq, env) {
  await tg(env, "answerCallbackQuery", { callback_query_id: cq.id }); // stop the spinner
  const chatId = cq.message.chat.id;
  if (cq.data === "owners") {
    await askForAddresses(env, chatId);               // upload flow, not a one-tap trigger
  } else if (cq.data === "deedslogin") {
    await askForDeedsLogin(env, chatId);              // 6th button: set up the deeds login
  } else if (cq.data === "deeds") {
    await triggerDeeds(env, chatId);                  // checks the saved login first
  } else {
    await trigger(env, cq.data, chatId);
  }
}

// The 🏛️ button: only run if Steven's login is saved; otherwise guide him to set it up.
async function triggerDeeds(env, chatId) {
  const saved = env.CREDS ? await env.CREDS.get("deeds") : null;
  if (!saved) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      parse_mode: "HTML",
      text: "🏛️ <b>One-time setup needed.</b> This button uses Steven's paid Register of Deeds account, so it needs his login first. Tap 🔑 <b>Set up Deeds Login</b> (or send /setup) to add it — just once.",
    });
    return;
  }
  await trigger(env, "deeds", chatId);
}

// ---- Register of Deeds login (Task E) -------------------------------------
// Steven saves his subscription login once, right here in Telegram. It's stored
// in the bot's memory (Cloudflare KV); the deeds.yml Action reads it from /creds.
function askForDeedsLogin(env, chatId) {
  return tg(env, "sendMessage", {
    chat_id: chatId,
    parse_mode: "HTML",
    text: "🔑 <b>Set up your Register of Deeds login</b>\n\nSend me ONE message in exactly this form (put your real Register of Deeds username and password):\n\n<code>/deedslogin USERNAME PASSWORD</code>\n\nExample: <code>/deedslogin steven123 myPassw0rd</code>\n\nI'll save it securely so the 🏛️ button works. (After you send it, please delete that message — it has your password in it.)",
  });
}

async function saveDeedsLogin(env, msg) {
  const chatId = msg.chat.id;
  const parts = (msg.text || "").trim().split(/\s+/);   // /deedslogin USER PASS...
  const user = parts[1] || "";
  const pass = parts.slice(2).join(" ");
  if (!user || !pass) {
    await askForDeedsLogin(env, chatId);
    return;
  }
  if (!env.CREDS) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "⚠️ The login store isn't set up yet (the bot needs its CREDS memory connected). Tell the developer." });
    return;
  }
  await env.CREDS.put("deeds", JSON.stringify({ user, pass }));
  // Best-effort: remove the message that contains the password.
  await tg(env, "deleteMessage", { chat_id: chatId, message_id: msg.message_id }).catch(() => {});
  await tg(env, "sendMessage", {
    chat_id: chatId,
    parse_mode: "HTML",
    text: "✅ <b>Login saved.</b> The 🏛️ <b>Register of Deeds</b> button works now — tap it any time.\n\n(If your message with the password is still visible above, delete it to be safe.)",
  });
}

// Task D is different from the other buttons: it needs the user to upload a file
// of addresses first. Prompt for it; the uploaded document is handled by onDocument.
function askForAddresses(env, chatId) {
  return tg(env, "sendMessage", {
    chat_id: chatId,
    parse_mode: "HTML",
    text: "📎 <b>Address → Owners</b>\nSend me your property addresses as a <b>.csv</b> or <b>.txt</b> file — one address per line (a CSV with an <i>Address</i> column works too).\n\nI'll look up each property's owner name + mailing address and send back a ready-to-use spreadsheet in your campaign format.",
  });
}

async function onDocument(msg, env) {
  const chatId = msg.chat.id;
  const doc = msg.document || {};
  const name = (doc.file_name || "").toLowerCase();
  if (!(name.endsWith(".csv") || name.endsWith(".txt"))) {
    await tg(env, "sendMessage", { chat_id: chatId, parse_mode: "HTML", text: "Please send a <b>.csv</b> or <b>.txt</b> file with one property address per line." });
    return;
  }
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: "⏳ Got your file — looking up owners + mailing addresses… this can take a few minutes (each address is looked up one by one). The spreadsheet lands here when it's done. You can close Telegram.",
  });
  const resp = await dispatch(env, WORKFLOWS.owners, {
    chat_id: String(chatId),
    file_id: doc.file_id,
    file_name: doc.file_name || "addresses.csv",
  });
  if (!resp.ok) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⚠️ Couldn't start the lookup (GitHub returned ${resp.status}). Check the bot's GH_TOKEN / GH_REPO.`,
    });
  }
}

const WELCOME = [
  "👋 <b>Welcome to Lead Collector</b>",
  "",
  "I turn Hamilton County public records into ready-to-use lead lists and send them to you here as a spreadsheet (CSV) file.",
  "",
  "<b>What you can get</b>",
  "",
  "📋 <b>Court Dockets</b>",
  "The people named in this week's Chancery Court “Motion Call” cases — foreclosures, estates, debts, divorces, and similar.",
  "",
  "🏠 <b>Obituary → Addresses</b>",
  "This week's obituary names from chattanoogan.com, each searched on the County Trustee property site to find a property address.",
  "",
  "💰 <b>Tax Sale → Owners</b>",
  "Every property on the County's Delinquent Tax Sale list — address, parcel, and minimum bid — with the current owner's name looked up from the Trustee site. (Takes a few minutes; it's ~140 lookups.)",
  "",
  "🔎 <b>Address → Owners</b>",
  "Upload your OWN list of property addresses (a .csv or .txt, one per line) and I'll return each property's owner name and MAILING address — in your mail-campaign format (LastName, FirstName, MiddleName, Address, City, State, ZipCode, Campaign).",
  "",
  "🏛️ <b>Register of Deeds → Affidavits</b>",
  "Recent affidavits (heirship/descent, loan-mod, etc.) from the Register of Deeds — party names + property address + parcel. (Uses Steven's subscription login; names/addresses only — PDFs stay manual.)",
  "",
  "🔑 <b>Set up Deeds Login</b>",
  "First time only: tap this to save your Register of Deeds username + password, so the 🏛️ button can sign in for you. After that you never do it again.",
  "",
  "<b>How to use it</b>",
  "Tap a button below 👇  Wait about 1–2 minutes. The file arrives right here in this chat. (For “Address → Owners,” tap it, then send your address file.)",
  "",
  "<b>📅 Automatic weekly delivery</b>",
  "You're now subscribed — a fresh copy is sent here automatically every week. Send /stop anytime to turn that off, /start to rejoin.",
  "",
  "<b>⚠️ Why some address rows are blank (normal)</b>",
  "The property site only lists <i>current</i> owners. Someone who recently passed often isn't the owner of record anymore (home in a spouse's name, a trust, or already sold), so there's no match. You'll get solid addresses on some names and blanks on others. Every row is labeled (matched / verify / no match) so it's clear.",
  "",
  "Tap a button to begin 👇",
].join("\n");

function sendMenu(env, chatId) {
  return tg(env, "sendMessage", {
    chat_id: chatId,
    text: WELCOME,
    parse_mode: "HTML",
    reply_markup: {
      inline_keyboard: [
        [{ text: "📋 Court Dockets",        callback_data: "dockets" }],
        [{ text: "🏠 Obituary → Addresses", callback_data: "obituaries" }],
        [{ text: "💰 Tax Sale → Owners",    callback_data: "taxsale" }],
        [{ text: "🔎 Address → Owners (upload)", callback_data: "owners" }],
        [{ text: "🏛️ Register of Deeds → Affidavits", callback_data: "deeds" }],
        [{ text: "🔑 Set up Deeds Login", callback_data: "deedslogin" }],
      ],
    },
  });
}

async function trigger(env, choice, chatId) {
  const wf = WORKFLOWS[choice];
  if (!wf) return;
  const LABELS = {
    dockets: "court docket names",
    obituaries: "obituary → property addresses",
    taxsale: "delinquent tax sale list + owners",
    deeds: "Register of Deeds recent affidavit names + addresses",
  };
  const ETA = {
    dockets: "about a minute",
    obituaries: "about a minute",
    taxsale: "about 5–7 minutes — it looks up ~140 owners one by one",
    deeds: "a minute or two",
  };
  const label = LABELS[choice] || "list";
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `⏳ Pulling the latest ${label}…\nThis takes ${ETA[choice] || "a minute or two"}, then the file lands here. You can close Telegram — it'll still arrive.`,
  });
  const resp = await dispatch(env, wf, { chat_id: String(chatId) });
  if (!resp.ok) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `⚠️ Couldn't start the job (GitHub returned ${resp.status}). Check the bot's GH_TOKEN / GH_REPO.`,
    });
  }
}
