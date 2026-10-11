"use strict";
// One page, no dependencies. Everything is built with textContent, never innerHTML.

const main = document.getElementById("main");
let state = null;       // /api/state
let timer = null;       // polling the open campaign
let view = {};          // what the open campaign view remembers between polls

const LABELS = {
  ready: ["Ready", "ok"], sending: ["Sending…", "warn"], sent: ["Sent", "ok"], dry: ["Rehearsed, not sent", "warn"],
  failed: ["Failed", "bad"], uncertain: ["Uncertain", "bad"], invalid: ["Invalid number", "bad"],
  incomplete: ["Missing value", "warn"], unchecked: ["Not checked", "warn"],
  known: ["Already known", ""], blocked: ["Blocked", ""], texted: ["Already texted", ""],
  duplicate: ["Duplicate", ""],
};
const CAMPAIGN = { draft: "Not started", running: "Running", paused: "Paused", done: "Finished" };
const ONGOING = { draft: "Off", running: "On", paused: "Off", done: "Off" };
const stateLabel = (c) => (c.kind === "ongoing" ? ONGOING : CAMPAIGN)[c.status] || c.status;
const FILTERS = [["", "All"], ["todo", "To send"], ["sent", "Sent"], ["problem", "Problems"], ["skipped", "Skipped"]];

function put(el, ...kids) { el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false)); }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "bulksms" },
    body: JSON.stringify(body),
  };
  let res;
  try { res = await fetch("/api" + path, opt); }
  catch (e) { throw new Error("The phone is not answering. Is the service running?"); }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/login") { showLogin(); throw new Error(data.error || "Log in first."); }
  if (!res.ok) throw new Error(data.error || "Something went wrong (" + res.status + ").");
  if (typeof data.dry_run === "boolean") showMode(data.dry_run);
  return data;
}

function toast(text, bad) {
  const el = document.getElementById("toast");
  el.textContent = text; el.className = "toast" + (bad ? " bad" : ""); el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 3500);
}
const fail = (e) => toast(e.message, true);
function after(result) {
  if (result && result.lookup_problem) toast("Could not check the numbers now (" + result.lookup_problem + "). Rows already checked keep their answer; unchecked rows are not sent.", true);
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }); }
function day(ts) { return new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }); }
function field(label, input, hint) { return h("div", {}, h("label", {}, label), input, hint ? h("div", { class: "muted small" }, hint) : null); }
function statusLabel(s) {
  if (s === "known" && state && state.lookup.enabled) return "Already in " + state.lookup.label;
  return (LABELS[s] || [s])[0];
}
function badge(s) { return h("span", { class: "badge " + ((LABELS[s] || ["", ""])[1]) }, statusLabel(s)); }

// -- message length ----------------------------------------------------------
// The same counting as template.py: GSM-7 packs 160 characters into one SMS (153
// per part when split); one character outside it turns the whole message into
// Unicode, 70 (67 per part).

const GSM = new Set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà");
const GSM_EXT = new Set("^{}\\[~]|€");

function segments(text) {
  let units = 0, odd = null;
  for (const ch of text) {
    if (GSM.has(ch)) units += 1;
    else if (GSM_EXT.has(ch)) units += 2;
    else { odd = ch; break; }
  }
  if (odd === null) return { units, parts: units === 0 ? 0 : units <= 160 ? 1 : Math.ceil(units / 153), encoding: "GSM-7", odd };
  units = text.length;  // UTF-16 code units, which is what Unicode SMS counts
  return { units, parts: units <= 70 ? 1 : Math.ceil(units / 67), encoding: "Unicode", odd };
}

// A live line under a message box. [name] is counted as `name()` when one is
// known, otherwise — like every other placeholder — as ten characters.
function counter(textarea, name) {
  const line = h("div", { class: "muted small counter" });
  const update = () => {
    let guessed = false;
    const text = textarea.value.replace(/\[([^\[\]\n]{1,40})\]/g, (m, key) => {
      const known = key.trim().toLowerCase() === "name" && name && name();
      if (known) return known;
      guessed = true; return "xxxxxxxxxx";
    });
    const s = segments(text);
    const room = s.encoding === "GSM-7" ? (s.parts <= 1 ? 160 - s.units : s.parts * 153 - s.units) : (s.parts <= 1 ? 70 - s.units : s.parts * 67 - s.units);
    put(line,
      h("strong", {}, s.units + " characters, " + s.parts + " SMS"),
      s.parts ? " · " + room + " left in this SMS" : "",
      guessed ? " · placeholders counted as 10 characters" : "",
      s.odd ? h("span", { class: "tip" }, " · “" + s.odd + "” is outside the SMS alphabet, so only 70 characters fit per SMS") : null);
  };
  textarea.addEventListener("input", update);
  update();
  return line;
}

// -- login -----------------------------------------------------------------

function showLogin() {
  clearInterval(timer);
  document.getElementById("nav").hidden = true;
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try { await api("/login", { password: pw.value }); await boot(); } catch (err) { fail(err); pw.select(); }
  } }, h("h2", {}, "Log in"), field("Password", pw), h("div", { class: "actions" }, h("button", { class: "primary" }, "Log in")));
  put(main, form); pw.focus();
}

// -- campaign list ---------------------------------------------------------

function campaignForm() {
  const d = state.defaults;
  const f = {
    name: h("input", { type: "text", required: true, maxlength: 120, placeholder: "Sellers, week 42" }),
    csv: h("textarea", { class: "mono", required: true, placeholder: "name;phone\nAnna Andersson;070-123 45 67\nBo Berg;+46 73 555 66 77" }),
    template: h("textarea", { required: true, maxlength: 2000, placeholder: "Hi [name]! …" }),
    delay: h("input", { type: "text", inputmode: "decimal", value: d.delay }),
    batch: h("input", { type: "text", inputmode: "numeric", value: d.batch }),
    jitter: h("input", { type: "text", inputmode: "decimal", value: d.jitter }),
    window_start: h("input", { type: "time", value: d.window_start }),
    window_end: h("input", { type: "time", value: d.window_end }),
    start_at: h("input", { type: "datetime-local" }),
    first_name_only: h("input", { type: "checkbox" }),
    allow_repeat: h("input", { type: "checkbox" }),
  };
  const fixed = h("input", { type: "radio", name: "kind", value: "fixed", checked: true });
  const ongoing = h("input", { type: "radio", name: "kind", value: "ongoing" });
  const csvLabel = h("label", {}, "Recipients (paste CSV)");
  const submit = h("button", { class: "primary" }, "Create and check");
  const kindChanged = () => {
    f.csv.required = fixed.checked;
    csvLabel.textContent = fixed.checked ? "Recipients (paste CSV)" : "Recipients (optional — add them as they come)";
  };
  fixed.addEventListener("change", kindChanged); ongoing.addEventListener("change", kindChanged);
  return h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const body = { kind: ongoing.checked ? "ongoing" : "fixed" };
    for (const [k, el] of Object.entries(f)) body[k] = el.type === "checkbox" ? el.checked : el.value;
    try { const r = await api("/campaigns", body); after(r); location.hash = "#/c/" + r.id; } catch (err) { fail(err); }
  } },
    h("h2", {}, "New campaign"),
    field("Name of the campaign", f.name),
    h("label", {}, "Type"),
    h("div", { class: "kinds" },
      h("label", { class: "kind" }, fixed, h("span", {}, h("strong", {}, "Fixed"), h("span", { class: "muted small" }, "A list you send through once. Finishes when everyone has been texted."))),
      h("label", { class: "kind" }, ongoing, h("span", {}, h("strong", {}, "Ongoing"), h("span", { class: "muted small" }, "Stays on until you turn it off. Whoever you add is texted automatically.")))),
    h("div", {}, csvLabel, f.csv, h("div", { class: "muted small" }, "A name and a phone number per row. Comma, semicolon or tab; a header row is optional. Other columns become placeholders, e.g. [town].")),
    field("Message", f.template, "[name] is replaced for each recipient."),
    counter(f.template),
    h("div", { class: "row" },
      field("Delay (seconds)", f.delay), field("Messages per round", f.batch),
      field("Random ± seconds", f.jitter)),
    h("div", { class: "row times" },
      field("Not before", f.window_start),
      field("Not after", f.window_end),
      field("Start at (optional)", f.start_at)),
    h("label", { class: "check" }, f.first_name_only, "Use only the first name for [name]"),
    h("label", { class: "check" }, f.allow_repeat, "Allow numbers I have texted before"),
    h("div", { class: "actions" }, submit,
      h("span", { class: "muted small" }, "Nothing is sent until you start it or turn it on.")),
    h("p", { class: "muted small" }, "A round sends that many messages back to back, then waits the delay."));
}

async function showList() {
  state = await api("/state");
  const items = state.campaigns.map((c) => {
    const n = c.counts, total = Object.values(n).reduce((a, b) => a + b, 0);
    return h("li", {},
      h("a", { class: "grow", href: "#/c/" + c.id }, c.name),
      c.kind === "ongoing" ? h("span", { class: "badge" }, "Ongoing") : null,
      h("span", { class: "badge " + (c.status === "running" ? "ok" : "") }, stateLabel(c)),
      h("span", { class: "muted small" }, (n.sent || 0) + " sent of " + total + (n.dry ? " (" + n.dry + " rehearsed)" : "")));
  });
  put(main, 
    h("h1", {}, "Campaigns"),
    items.length ? h("div", { class: "card" }, h("ul", { class: "list" }, items)) : null,
    campaignForm());
}

// -- one campaign ----------------------------------------------------------

async function showCampaign(id) {
  view = { id, filter: "", page: 0, editing: null, built: false };
  put(main, h("p", { class: "muted" }, "Loading…"));
  await refreshCampaign();
  timer = setInterval(() => { refreshCampaign().catch(() => {}); }, 2000);
}

async function act(path, body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try { after(await api(path, body || {})); await refreshCampaign(); } catch (e) { fail(e); }
}

async function refreshCampaign() {
  const d = await api("/campaigns/" + view.id + "?filter=" + view.filter + "&page=" + view.page);
  if (!view.built) buildCampaign(d);
  renderLive(d);
}

function buildCampaign(d) {
  const c = d.campaign;
  const f = view.form = {
    template: h("textarea", { maxlength: 2000 }),
    delay: h("input", { type: "text", inputmode: "decimal" }),
    batch: h("input", { type: "text", inputmode: "numeric" }),
    jitter: h("input", { type: "text", inputmode: "decimal" }),
    window_start: h("input", { type: "time" }),
    window_end: h("input", { type: "time" }),
    start_at: h("input", { type: "datetime-local" }),
    first_name_only: h("input", { type: "checkbox" }),
    allow_repeat: h("input", { type: "checkbox" }),
  };
  for (const [k, el] of Object.entries(f)) { if (el.type === "checkbox") el.checked = !!c[k]; else el.value = c[k]; }
  const more = h("textarea", { class: "mono", placeholder: "name;phone" });
  view.el = {
    title: h("h1", {}, c.name), status: h("div", { class: "statusline" }), bar: h("progress", { max: 1, value: 0 }),
    chips: h("div", { class: "chips" }), controls: h("div", { class: "actions" }),
    preview: h("div", {}), tabs: h("div", { class: "chips" }), table: h("div", { class: "tablewrap" }),
    pager: h("div", { class: "pager" }), log: h("ul", { class: "log" }),
  };
  const e = view.el;
  put(main, 
    h("p", { class: "small" }, h("a", { href: "#/" }, "← All campaigns")),
    e.title,
    h("div", { class: "card" }, e.status, e.bar, e.chips, e.controls),
    h("form", { class: "card", onsubmit: async (ev) => {
      ev.preventDefault();
      const body = {};
      for (const [k, el] of Object.entries(f)) body[k] = el.type === "checkbox" ? el.checked : el.value;
      try { await api("/campaigns/" + view.id, body); toast("Saved."); await refreshCampaign(); } catch (err) { fail(err); }
    } },
      h("h2", {}, "Message and pace"),
      field("Message", f.template),
      counter(f.template, () => view.sampleName),
      e.preview,
      h("div", { class: "row" },
        field("Delay (seconds)", f.delay), field("Messages per round", f.batch), field("Random ± seconds", f.jitter)),
      h("div", { class: "row times" },
        field("Not before", f.window_start), field("Not after", f.window_end),
        field("Start at (optional)", f.start_at)),
      h("label", { class: "check" }, f.first_name_only, "Use only the first name for [name]"),
      h("label", { class: "check" }, f.allow_repeat, "Allow numbers I have texted before"),
      h("div", { class: "actions" }, h("button", {}, "Save"),
        h("span", { class: "muted small" }, "The pace can be changed while it runs; the message only while paused."))),
    h("div", { class: "card" }, h("h2", {}, "Recipients"), e.tabs, e.table, e.pager),
    h("form", { class: "card", onsubmit: async (ev) => {
      ev.preventDefault();
      // An ongoing campaign that is on sends without a Start press, so this is the last question.
      if (view.live && !confirm("This campaign is on: the people you add are texted right away. Add them?")) return;
      try { after(await api("/campaigns/" + view.id + "/add", { csv: more.value })); more.value = ""; await refreshCampaign(); } catch (err) { fail(err); }
    } }, h("h2", {}, "Add more recipients"), more, h("div", { class: "actions" }, h("button", {}, "Add and check"))),
    h("div", { class: "card" }, h("h2", {}, "Log"), e.log));
  view.built = true;
}

function renderLive(d) {
  const c = d.campaign, n = d.counts, e = view.el, base = "/campaigns/" + view.id;
  const sent = (n.sent || 0) + (n.dry || 0), left = (n.ready || 0) + (n.sending || 0);
  e.title.textContent = c.name;
  view.form.template.disabled = d.running;

  const eta = left && c.status === "running" ? "about " + Math.ceil(Math.ceil(left / Math.max(1, c.batch)) * c.delay / 60) + " min left" : "";
  const next = d.next_at && c.status === "running" ? "next in " + Math.max(0, Math.round(d.next_at - Date.now() / 1000)) + " s" : "";
  put(e.status, 
    c.kind === "ongoing" ? h("span", { class: "badge" }, "Ongoing") : null,
    h("span", { class: "badge " + (c.status === "running" ? "ok" : c.status === "paused" ? "warn" : "") }, stateLabel(c)),
    h("strong", {}, sent + (n.dry ? " rehearsed, " : " sent, ") + left + " to go"),
    h("span", { class: "muted small" }, [c.note, eta, next].filter(Boolean).join(" · ")));
  e.bar.max = Math.max(1, sent + left); e.bar.value = sent;

  put(e.chips, ...Object.keys(LABELS).filter((s) => n[s]).map((s) =>
    h("span", { class: "badge " + LABELS[s][1] }, statusLabel(s) + ": " + n[s])));

  const on = c.kind === "ongoing";
  view.live = on && d.running && !state.dry_run;
  const startLabel = on ? "Turn on" : c.status === "draft" ? "Start" : "Resume";
  const ask = state.dry_run ? null : on
    ? "Turn this campaign on? " + (n.ready || 0) + " real SMS go out now, and everyone you add later is texted automatically until you turn it off."
    : "Send " + (n.ready || 0) + " real SMS from this phone?";
  put(e.controls, 
    d.running
      ? h("button", { class: "primary", type: "button", onclick: () => act(base + "/pause") }, on ? "Turn off" : "Pause")
      : h("button", { class: "primary", type: "button", disabled: !on && !n.ready && !n.unchecked,
          onclick: () => act(base + "/start", {}, ask) }, startLabel),
    h("button", { type: "button", onclick: () => act(base + "/check") }, "Check numbers again"),
    n.failed ? h("button", { type: "button", onclick: () => act(base + "/retry-failed") }, "Retry the failed") : null,
    d.running ? null : h("button", { class: "danger", type: "button", onclick: async () => {
      if (!confirm("Delete this campaign? The history of who was texted is kept.")) return;
      try { await api(base + "/delete", {}); location.hash = "#/"; } catch (err) { fail(err); }
    } }, "Delete campaign"));

  const p = d.preview;
  const sample = c.first_name_only ? (p.to || "").trim().split(/\s+/)[0] : (p.to || "").trim();
  if (sample !== view.sampleName) { view.sampleName = sample; view.form.template.dispatchEvent(new Event("input")); }
  const draft = view.form.template.value !== c.template;
  put(e.preview, 
    h("div", { class: "muted small" }, "Preview" + (p.to ? " for " + p.to : "") + (draft ? " — the saved message; save to update" : "")),
    h("div", { class: "preview" }, p.text),
    d.unknown_placeholders.length ? h("div", { class: "tip" }, "Will be sent as written, not replaced: " + d.unknown_placeholders.map((k) => "[" + k + "]").join(", ") + ". Available: " + d.placeholders.map((k) => "[" + k + "]").join(", ")) : null,
    /stop/i.test(c.template) ? null : h("div", { class: "tip" }, "The message does not say who it is from or how to decline (e.g. “Reply STOP”). Add people who decline to the block list."));

  put(e.tabs, ...FILTERS.map(([key, label]) => h("button", { type: "button", class: "chip" + (view.filter === key ? " on" : ""),
    onclick: () => { view.filter = key; view.page = 0; view.editing = null; refreshCampaign().catch(fail); } }, label)));

  if (view.editing == null) put(e.table, recipientTable(d));
  const pages = Math.ceil(d.total / d.page_size);
  put(e.pager, ...(pages > 1 ? [
    h("button", { type: "button", class: "mini", disabled: view.page === 0, onclick: () => { view.page--; refreshCampaign().catch(fail); } }, "‹ Previous"),
    "Page " + (view.page + 1) + " of " + pages,
    h("button", { type: "button", class: "mini", disabled: view.page >= pages - 1, onclick: () => { view.page++; refreshCampaign().catch(fail); } }, "Next ›"),
  ] : [h("span", { class: "muted" }, d.total + " rows")]));

  put(e.log, ...d.events.map((ev) => h("li", {}, h("time", {}, clock(ev.at)), ev.message)));
}

function recipientTable(d) {
  if (!d.recipients.length) return h("p", { class: "muted" }, "Nothing here.");
  const rows = d.recipients.map((r) => {
    const base = "/recipients/" + r.id;
    const locked = r.status === "sent" || r.status === "dry" || r.status === "sending";
    const buttons = [];
    if (r.status === "uncertain") {
      buttons.push(h("button", { type: "button", class: "mini", onclick: () => act(base + "/resolve", { sent: true }) }, "It was sent"),
        h("button", { type: "button", class: "mini", onclick: () => act(base + "/resolve", { sent: false }) }, "It was not"));
    }
    if (!locked) buttons.push(h("button", { type: "button", class: "mini", onclick: () => editRow(r) }, "Edit"));
    if (r.phone && r.status !== "blocked" && r.status !== "sending") buttons.push(h("button", { type: "button", class: "mini", onclick: () => act(base + "/block", {}, "Never text " + r.phone + " again?") }, "Block"));
    if (!locked) buttons.push(h("button", { type: "button", class: "mini danger", onclick: () => act(base + "/delete", {}, "Remove " + (r.name || r.phone_raw) + " from this campaign?") }, "Delete"));
    return h("tr", { "data-id": r.id },
      h("td", { class: "num" }, r.position),
      h("td", {}, r.name || h("span", { class: "muted" }, "—")),
      h("td", {}, r.phone || r.phone_raw),
      h("td", {}, badge(r.status), r.detail ? h("div", { class: "muted small" }, r.detail) : null,
        r.sent_at ? h("div", { class: "muted small" }, clock(r.sent_at)) : null),
      h("td", { class: "act" }, buttons));
  });
  return h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Name"), h("th", {}, "Phone"), h("th", {}, "Status"), h("th", {}, ""))), h("tbody", {}, rows));
}

function editRow(r) {
  view.editing = r.id;
  const tr = view.el.table.querySelector('tr[data-id="' + r.id + '"]');
  const name = h("input", { type: "text", value: r.name, maxlength: 120, "aria-label": "Name" });
  const phone = h("input", { type: "text", value: r.phone_raw, maxlength: 40, inputmode: "tel", "aria-label": "Phone" });
  const done = () => { view.editing = null; refreshCampaign().catch(fail); };
  const save = async () => {
    try { after(await api("/recipients/" + r.id, { name: name.value, phone: phone.value })); done(); } catch (e) { fail(e); }
  };
  for (const el of [name, phone]) el.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); save(); } if (ev.key === "Escape") done(); });
  put(tr, h("td", { class: "num" }, r.position), h("td", {}, name), h("td", {}, phone), h("td", {}, badge(r.status)),
    h("td", { class: "act" }, h("button", { type: "button", class: "mini primary", onclick: save }, "Save"), h("button", { type: "button", class: "mini", onclick: done }, "Cancel")));
  name.focus();
}

// -- block list and history -------------------------------------------------

async function showBlock() {
  const d = await api("/blocklist");
  const phones = h("textarea", { class: "mono", placeholder: "One number per line", required: true });
  const note = h("input", { type: "text", maxlength: 200, placeholder: "Why (optional)" });
  put(main, h("h1", {}, "Block list"),
    h("p", { class: "muted" }, "Numbers here are never texted, in any campaign."),
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      try {
        const r = await api("/blocklist", { phones: phones.value, note: note.value });
        toast(r.added + " added" + (r.invalid.length ? "; not a number: " + r.invalid.join(", ") : ""), r.invalid.length > 0);
        showBlock();
      } catch (err) { fail(err); }
    } }, field("Numbers", phones), field("Note", note), h("div", { class: "actions" }, h("button", { class: "primary" }, "Block"))),
    h("div", { class: "card" }, d.blocklist.length ? h("ul", { class: "list" }, d.blocklist.map((b) => h("li", {},
      h("strong", {}, b.phone), h("span", { class: "grow muted" }, b.note), h("span", { class: "muted small" }, day(b.added_at)),
      h("button", { type: "button", class: "mini", onclick: async () => {
        if (!confirm("Allow " + b.phone + " to be texted again?")) return;
        try { await api("/blocklist/delete", { phone: b.phone }); showBlock(); } catch (err) { fail(err); }
      } }, "Remove")))) : h("p", { class: "muted" }, "Nobody is blocked.")));
}

async function showHistory(q) {
  const d = await api("/history?q=" + encodeURIComponent(q || ""));
  const search = h("input", { type: "search", value: q || "", placeholder: "Number or name" });
  put(main, h("h1", {}, "History"),
    h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); showHistory(search.value).catch(fail); } },
      field("Search everyone ever texted", search), h("div", { class: "actions" }, h("button", {}, "Search"))),
    h("div", { class: "card tablewrap" }, d.history.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Name"), h("th", {}, "Phone"), h("th", {}, "Campaign"))),
      h("tbody", {}, d.history.map((r) => h("tr", { title: r.text }, h("td", {}, day(r.sent_at)), h("td", {}, r.name), h("td", {}, r.phone), h("td", {}, r.campaign_name)))))
      : h("p", { class: "muted" }, "Nothing has been sent.")));
}

// -- routing ------------------------------------------------------------------

async function route() {
  clearInterval(timer);
  const hash = location.hash || "#/";
  try {
    const m = hash.match(/^#\/c\/(\d+)$/);
    if (m) await showCampaign(Number(m[1]));
    else if (hash === "#/block") await showBlock();
    else if (hash === "#/history") await showHistory("");
    else await showList();
  } catch (e) { if (!/Log in/.test(e.message)) { fail(e); } }
}

// The mode belongs to the server and changes when it is restarted, so the
// banner follows every answer — not only the one the page was loaded with.
function showMode(dry) {
  if (state) state.dry_run = dry;
  const banner = document.getElementById("banner");
  banner.hidden = !dry;
  banner.textContent = "Dry run: nothing is really sent. Set \"dry_run\": false in config.json and restart to send.";
}

async function boot() {
  try { state = await api("/state"); } catch (e) { return; }
  document.title = state.title;
  document.getElementById("brand").textContent = state.title;
  document.getElementById("nav").hidden = false;
  showMode(state.dry_run);
  route();
}

document.getElementById("logout").addEventListener("click", async () => { try { await api("/logout", {}); } catch (e) { /* gone anyway */ } showLogin(); });
window.addEventListener("hashchange", route);
boot();
