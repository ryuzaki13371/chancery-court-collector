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

    // Private endpoint: the deeds.yml Action POSTs the numbered pick-list here after a
    // search, so a later reply like "3 7 12" can be mapped back to the right PDFs.
    // Body: { chat_id, items:[{n, image_id, name, book_page}] }  (protected by DEEDS_KEY)
    if (url.pathname === "/savelist" && request.method === "POST") {
      const key = request.headers.get("X-Creds-Key") || url.searchParams.get("key") || "";
      if (!env.DEEDS_KEY || key !== env.DEEDS_KEY) return new Response("forbidden", { status: 403 });
      if (!env.CREDS) return new Response("no store", { status: 500 });
      let body;
      try { body = await request.json(); } catch { return new Response("bad json", { status: 400 }); }
      if (body && body.chat_id) {
        await env.CREDS.put("list:" + body.chat_id, JSON.stringify(body.items || []),
                            { expirationTtl: 86400 });   // forget after a day
      }
      return new Response("ok");
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
    await registerCommands(env);        // keep the slash-command menu up to date
    await sendMenu(env, chatId);
    // Subscribe this chat to the weekly auto-delivery (fire and forget).
    const who = [msg.chat.first_name, msg.chat.username].filter(Boolean).join(" @");
    await dispatch(env, "subscribe.yml", { chat_id: String(chatId), name: who || "" });
  } else if (cmd === "/help") {
    await sendMenu(env, chatId);
  } else if (cmd === "/howto" || cmd === "/guide") {
    await sendHowTo(env, chatId);
  } else if (cmd === "/deedssteps" || cmd === "/deedshelp") {
    await sendDeedsSteps(env, chatId);
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
  } else if (await maybeHandlePdfSelection(env, msg)) {
    // handled: it was a "3 7 12" / "all" reply picking PDFs from the last deeds list
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
  } else if (cq.data === "howto") {
    await sendHowTo(env, chatId);                     // 7th button: full how-to guide
  } else if (cq.data === "deedssteps") {
    await sendDeedsSteps(env, chatId);                // 8th button: deeds step-by-step
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

// If Steven replies with numbers ("3 7 12") or "all" AND he has a recent deeds
// pick-list, fetch the PDFs for just those rows. Returns true if it handled the msg.
async function maybeHandlePdfSelection(env, msg) {
  const chatId = msg.chat.id;
  const text = (msg.text || "").trim().toLowerCase();
  if (!/^(all|\d[\d ,]*)$/.test(text)) return false;     // not a selection-looking reply
  if (!env.CREDS) return false;
  const raw = await env.CREDS.get("list:" + chatId);
  if (!raw) return false;                                // no pending list -> not for us
  let items;
  try { items = JSON.parse(raw); } catch { return false; }
  if (!Array.isArray(items) || !items.length) return false;

  let chosen;
  if (text === "all") {
    chosen = items;
  } else {
    const nums = new Set(text.split(/[ ,]+/).filter(Boolean).map(Number));
    chosen = items.filter((it) => nums.has(it.n));
  }
  if (!chosen.length) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "I couldn't match those numbers to the last list. Reply with the row numbers you want, e.g. 3 7 12 — or all." });
    return true;
  }
  const ids = chosen.map((it) => it.image_id).filter(Boolean).join(",");
  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `📎 Getting ${chosen.length} document(s)… I'll send them as a zip in a few minutes (going slow to keep the account safe).`,
  });
  const resp = await dispatch(env, "deedspdf.yml", { chat_id: String(chatId), image_ids: ids });
  if (!resp.ok) {
    await tg(env, "sendMessage", { chat_id: chatId, text: `⚠️ Couldn't start the document fetch (GitHub ${resp.status}).` });
  }
  return true;
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
  "Recent affidavits (heirship/descent, loan-mod, etc.) from the Register of Deeds — party names + property address + parcel. Uses Steven's subscription login. Can also fetch the document PDFs (capped at 25 per run, slowly, to keep the account safe).",
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

// The slash-command menu (the list shown when you type "/"). The bot re-registers
// this on every /start, so it's always current — no BotFather steps needed.
function registerCommands(env) {
  const commands = [
    { command: "start",      description: "Welcome + the button menu + weekly auto-delivery" },
    { command: "howto",      description: "How to use this bot (full guide)" },
    { command: "dockets",    description: "Get court docket names now" },
    { command: "obituaries", description: "Get obituary property addresses now" },
    { command: "taxsale",    description: "Get delinquent tax sale list + owners" },
    { command: "owners",     description: "Upload addresses → get owners + mailing addresses" },
    { command: "deeds",      description: "Register of Deeds → recent names + addresses" },
    { command: "deedssteps", description: "Register of Deeds: step-by-step guide" },
    { command: "setup",      description: "Set up your Register of Deeds login (one time)" },
    { command: "stop",       description: "Stop the weekly auto-delivery" },
  ];
  return tg(env, "setMyCommands", { commands });
}

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
        [{ text: "📖 How to use", callback_data: "howto" }],
        [{ text: "🪜 Deeds: Step by Step", callback_data: "deedssteps" }],
      ],
    },
  });
}

// Full, plain-language guide. Covers every button, the workflow, the deeds login,
// and the PDF cap (25) — so Steven knows everything.
const HOWTO = [
  "📖 <b>How to use Lead Collector</b>",
  "",
  "<b>The basic idea:</b> open the bot → tap a button → wait 1–2 minutes → a spreadsheet (CSV) drops into this chat. That's it.",
  "",
  "📊 <b>Everything also saves to your Google Sheet automatically</b> — each list on its own tab, growing with every request. No copy-paste, nothing to organize.",
  "",
  "<b>The buttons</b>",
  "📋 <b>Court Dockets</b> — this week's Chancery Court case names. ~1 min.",
  "🏠 <b>Obituary → Addresses</b> — obituary names + a property address looked up for each. ~1 min.",
  "💰 <b>Tax Sale → Owners</b> — the delinquent tax-sale list with each owner looked up. ~5–7 min (it's ~140 lookups).",
  "🔎 <b>Address → Owners</b> — tap it, then SEND a .csv/.txt file of property addresses. It searches the county's <b>free public</b> property site (no login, nothing to get suspended) and sends back each owner's name + <b>mailing address</b> in your ready-to-use campaign format. Every result is also <b>added to your Google Sheet automatically</b> on each request — so your master list keeps growing, no copy-paste.",
  "🏛️ <b>Register of Deeds</b> — recent affidavits (heirship, loan-mod…): party names + property address + parcel. Needs the one-time login below.",
  "🔑 <b>Set up Deeds Login</b> — first time only (see below).",
  "",
  "<b>🔑 Setting up the Register of Deeds login (one time)</b>",
  "1. Tap <b>Set up Deeds Login</b>.",
  "2. Send one message: <code>/deedslogin USERNAME PASSWORD</code> (your Register of Deeds account).",
  "3. It's saved. From then on, just tap 🏛️ — you never log in again.",
  "(Tip: delete your password message afterward, just to be tidy.)",
  "",
  "<b>📎 About the PDF documents</b>",
  "The 🏛️ button can also fetch the actual recorded PDF documents, sent as a zip. To stay safe on the paid account, it's <b>limited to 25 documents per run</b> and goes slowly. (Names + addresses always come; the PDFs are the optional extra.)",
  "",
  "<b>📅 Weekly auto-delivery</b>",
  "Tapping /start signs you up — fresh lists arrive here automatically every week. Send /stop to turn it off, /start to rejoin.",
  "",
  "<b>⚠️ Why some rows are blank (normal)</b>",
  "Property records only show CURRENT owners. Someone who recently passed often isn't the owner of record anymore (spouse, trust, or sold), so there's no match. Every row is labeled (matched / verify / no match) so it's clear.",
  "",
  "<b>How long things take</b>",
  "Most buttons: 1–2 minutes. Tax Sale: 5–7 minutes. Register of Deeds with PDFs: a bit longer (it goes slow on purpose). You can close Telegram — the file still arrives.",
].join("\n");

function sendHowTo(env, chatId) {
  return tg(env, "sendMessage", { chat_id: chatId, text: HOWTO, parse_mode: "HTML",
    disable_web_page_preview: true });
}

// Step-by-step JUST for the Register of Deeds feature — every step, in order.
const DEEDS_STEPS = [
  "🪜 <b>Register of Deeds — step by step</b>",
  "Follow these in order. You only do Steps 1–2 the very first time.",
  "",
  "<b>STEP 1 — Set up your login (first time only)</b>",
  "Tap 🔑 <b>Set up Deeds Login</b>.",
  "",
  "<b>STEP 2 — Send your login (first time only)</b>",
  "Send one message in this exact form, with your real Register of Deeds login:",
  "<code>/deedslogin USERNAME PASSWORD</code>",
  "You'll see ✅ “Login saved.” (Then delete that message — it has your password.)",
  "",
  "<b>STEP 3 — Get the list</b>",
  "Tap 🏛️ <b>Register of Deeds → Affidavits</b>. Wait about a minute.",
  "A spreadsheet arrives with the recent <b>names + addresses</b>, each row <b>numbered</b> (#1, #2, #3 …). It's also saved to your Google Sheet automatically.",
  "",
  "<b>STEP 4 — Pick the documents you want (optional)</b>",
  "Look at the numbered list. If you want the actual PDF documents for some rows, just reply with their numbers, like:",
  "<code>3 7 12</code>   (or reply <code>all</code>)",
  "",
  "<b>STEP 5 — Get the PDFs</b>",
  "Wait a few minutes — the documents you picked arrive here as a zip file 📎. (It goes slowly on purpose, to keep your paid account safe. Up to 25 at a time.)",
  "",
  "<b>That's it.</b> Next time, skip Steps 1–2 — just tap 🏛️ and you're at Step 3. ✅",
].join("\n");

function sendDeedsSteps(env, chatId) {
  return tg(env, "sendMessage", { chat_id: chatId, text: DEEDS_STEPS, parse_mode: "HTML",
    disable_web_page_preview: true });
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
