"use strict";
/*
 * app.js — BokYup web frontend (Layer 8).
 *
 * Vanilla JS, no build step (CLAUDE.md: the universal UI layer, pure-pip stack).
 * Talks to the FastAPI backend on the same origin. Multi-book "tabs": the list of
 * known databases is shown across the top; each is unlocked individually with its
 * own passphrase, then switched between like browser tabs.
 */

const API = "";                       // same origin
const state = { books: [], activeBook: null, section: "transactions" };

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------
async function api(method, path, body) {
  // Phone build: the Python backend runs in-process under Pyodide (no HTTP).
  if (window.__BOKYUP_NATIVE__) return nativeApi(method, path, body);

  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(API + path, opts);
  const isJson = (resp.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const detail = (data && data.detail) ? data.detail : ("HTTP " + resp.status);
    throw new ApiError(detail, resp.status);
  }
  return data;
}

// In-process transport (phone). Mirrors the fetch path's return/throw contract:
// JSON results pass through; the SIE "raw text" result unwraps to its string so
// callers that expect text keep working. Raw binary (a receipt image) is returned
// as the {raw,base64,media_type} object — receiptSrc() turns it into a data URL.
async function nativeApi(method, path, body) {
  let query = null;
  const qi = path.indexOf("?");
  if (qi >= 0) {
    query = Object.fromEntries(new URLSearchParams(path.slice(qi + 1)));
    path = path.slice(0, qi);
  }
  const out = await window.__BOKYUP_NATIVE__.call(method, path, body ?? null, query);
  if (!out.ok) throw new ApiError(out.detail || ("HTTP " + out.status), out.status);
  const r = out.result;
  if (r && r.raw && "text" in r) return r.text;   // e.g. SIE export
  return r;
}

// Source for a receipt <img>: an HTTP URL on desktop, a data: URL on the phone.
async function receiptSrc(receiptId) {
  if (window.__BOKYUP_NATIVE__) {
    const r = await api("GET", `/books/${bid()}/receipts/${receiptId}`);
    return `data:${r.media_type};base64,${r.base64}`;
  }
  return `/books/${bid()}/receipts/${receiptId}`;
}
class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

// ---------------------------------------------------------------------------
// Money helpers (UI works in kronor; API works in ören)
// ---------------------------------------------------------------------------
// Strip grouping spaces (regular + non-breaking, e.g. "1 438,40") before parsing,
// otherwise parseFloat stops at the space and saves a wrong amount (1438,40 -> 1).
const toOre = (kr) => Math.round(parseFloat(String(kr).replace(/[\s ]/g, "").replace(",", ".")) * 100);
const toKr = (ore) => (ore / 100).toLocaleString("sv-SE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// ---------------------------------------------------------------------------
// Tiny DOM helpers
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
}

// Promise-based modal with arbitrary fields. fields: [{name,label,type,value,options}]
function modal(title, fields, okLabel = "OK") {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    const body = $("#modal-body");
    body.innerHTML = "";
    const inputs = {};
    for (const f of fields) {
      body.appendChild(el("label", {}, f.label));
      let input;
      if (f.type === "select") {
        input = el("select", {});
        for (const o of f.options) input.appendChild(el("option", { value: o.value }, o.label));
        if (f.value !== undefined) input.value = f.value;
      } else if (f.type === "file") {
        input = el("input", { type: "file", accept: f.accept || "image/*" });
      } else {
        input = el("input", { type: f.type || "text", value: f.value ?? "" });
      }
      inputs[f.name] = input;
      if (f.onChange) {
        input._prev = input.value;
        input.addEventListener("change", () => f.onChange(input.value, input, inputs));
      }
      body.appendChild(input);
    }
    $("#modal-ok").textContent = okLabel;
    $("#modal-backdrop").classList.remove("hidden");
    if (fields.length) inputs[fields[0].name].focus();

    const close = (result) => {
      $("#modal-backdrop").classList.add("hidden");
      $("#modal-ok").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(result);
    };
    $("#modal-ok").onclick = () => {
      const out = {};
      for (const [k, v] of Object.entries(inputs)) out[k] = v.type === "file" ? (v.files[0] || null) : v.value;
      close(out);
    };
    $("#modal-cancel").onclick = () => close(null);
  });
}

async function guard(fn) {
  try { await fn(); }
  catch (e) { toast(e.message || String(e), true); }
}

// ---------------------------------------------------------------------------
// Book tabs (top bar)
// ---------------------------------------------------------------------------
async function loadBooks() {
  state.books = await api("GET", "/books");
  renderTabs();
}

function renderTabs() {
  const tabs = $("#book-tabs");
  tabs.innerHTML = "";
  for (const b of state.books) {
    const active = state.activeBook && state.activeBook.id === b.id;
    tabs.appendChild(el("div", {
      class: "book-tab" + (active ? " active" : ""),
      onclick: () => guard(() => openBook(b)),
    }, b.display_name));
  }
  tabs.appendChild(el("div", { class: "book-tab", onclick: () => guard(newBookFlow) }, "+ Ny bok"));
  $("#active-book").textContent = state.activeBook ? state.activeBook.display_name : "";
}

async function newBookFlow() {
  const f = await modal("Ny bok", [
    { name: "display_name", label: "Namn (t.ex. Enskild firma 2026)" },
    { name: "db_path", label: "Filsökväg (t.ex. C:\\bokforing\\firma.db)" },
    { name: "passphrase", label: "Lösenord", type: "password" },
  ], "Skapa");
  if (!f) return;
  const rec = await api("POST", "/books", f);
  await loadBooks();
  state.activeBook = rec;
  await loadBooks();
  renderWorkspace();
  toast("Bok skapad och öppnad");
}

async function removeBookFlow(b) {
  // Step 1: choose how far the removal goes. Default is the safe option.
  const f = await modal(`Ta bort "${b.display_name}"`, [
    { name: "mode", label: "Vad vill du göra?", type: "select", value: "forget",
      options: [
        { value: "forget", label: "Ta bort ur listan (behåll filerna)" },
        { value: "purge", label: "Radera alla filer permanent (kan inte ångras)" },
      ] },
  ], "Fortsätt");
  if (!f) return;

  let deleteFiles = false;
  if (f.mode === "purge") {
    // Step 2: irreversible — require typing the book's name to confirm.
    const c = await modal(
      `Radera ALLT för "${b.display_name}"? Databasen, nyckeln och alla kvitton/foton `
      + `raderas permanent och kan inte återställas.`,
      [{ name: "confirm", label: `Skriv bokens namn (${b.display_name}) för att bekräfta` }],
      "Radera permanent");
    if (!c) return;
    if ((c.confirm || "").trim() !== b.display_name) {
      toast("Namnet stämmer inte — avbröt", true);
      return;
    }
    deleteFiles = true;
  }

  const res = await api("DELETE", `/books/${b.id}${deleteFiles ? "?delete_files=true" : ""}`);
  if (state.activeBook && state.activeBook.id === b.id) state.activeBook = null;
  await loadBooks();
  renderWorkspace();
  toast(deleteFiles
    ? `"${b.display_name}" raderad (${(res.deleted_paths || []).length} filer)`
    : `"${b.display_name}" borttagen ur listan`);
}

async function openBook(b) {
  // Try a no-op call to see if it is already unlocked; otherwise prompt.
  try {
    await api("GET", `/books/${b.id}/categories`);
  } catch (e) {
    if (e.status === 423) {
      const f = await modal(`Lås upp "${b.display_name}"`, [
        { name: "passphrase", label: "Lösenord", type: "password" },
      ], "Lås upp");
      if (!f) return;
      await api("POST", `/books/${b.id}/unlock`, { passphrase: f.passphrase });
    } else { throw e; }
  }
  state.activeBook = b;
  renderTabs();
  renderWorkspace();
}

async function lockActive() {
  if (!state.activeBook) return;
  await api("POST", `/books/${state.activeBook.id}/lock`);
  const name = state.activeBook.display_name;
  state.activeBook = null;
  renderTabs();
  renderHome();
  toast(`"${name}" låst`);
}

// ---------------------------------------------------------------------------
// Home (no active book)
// ---------------------------------------------------------------------------
function renderHome() {
  const v = $("#view");
  v.innerHTML = "";
  v.appendChild(el("div", { class: "panel" },
    el("div", { style: "display:flex;align-items:center;justify-content:space-between" },
      el("h2", { style: "margin:0" }, "Dina böcker"),
      el("div", {},
        el("button", { class: "btn small ghost", onclick: () => guard(importBackupFlow) }, "Återställ säkerhetskopia"),
        el("button", { class: "btn small", style: "margin-left:8px", onclick: () => guard(newBookFlow) }, "+ Ny bok"))),
    state.books.length === 0
      ? el("p", { class: "muted" }, "Inga böcker ännu. Skapa en med “+ Ny bok” eller återställ en .buyn-säkerhetskopia.")
      : el("div", { class: "cards" }, state.books.map((b) =>
          el("div", { class: "card" },
            el("h4", {}, b.display_name),
            el("p", { class: "muted" }, b.last_opened ? ("Senast: " + b.last_opened.slice(0, 10)) : "Aldrig öppnad"),
            el("button", { class: "btn small", onclick: () => guard(() => openBook(b)) }, "Öppna"),
            el("button", { class: "btn small ghost danger", style: "margin-left:6px",
              onclick: () => guard(() => removeBookFlow(b)) }, "Ta bort"),
          ))),
  ));
  v.appendChild(updatePanel());
  maybeAutoCheckUpdates();
}

// ---------------------------------------------------------------------------
// Updates (app-level, via GitHub Releases). Never auto-installs — it only checks
// (optionally on startup) and the user must click "Uppdatera nu" to apply.
// ---------------------------------------------------------------------------
function autoUpdateOn() { return localStorage.getItem("bokyup.autoUpdate") !== "off"; }

function updatePanel() {
  const auto = el("input", { type: "checkbox" });
  auto.checked = autoUpdateOn();
  auto.onchange = () => localStorage.setItem("bokyup.autoUpdate", auto.checked ? "on" : "off");
  const box = el("div", { class: "panel", id: "update-panel", style: "margin-top:16px" },
    el("div", { style: "display:flex;align-items:center;justify-content:space-between" },
      el("h3", { style: "margin:0" }, "Uppdateringar"),
      el("button", { class: "btn small", onclick: () => guard(() => checkUpdates(true)) }, "Sök efter uppdateringar")),
    el("p", { class: "muted", style: "margin:6px 0" }, "Installerad version: " + (state.appVersion || "okänd")),
    el("label", { style: "display:flex;gap:6px;align-items:center;font-size:14px" },
      auto, "Sök automatiskt efter uppdateringar när appen startar"),
    el("div", { id: "update-result", style: "margin-top:8px" }));
  if (state.updateInfo) setTimeout(() => renderUpdateResult(state.updateInfo), 0);
  return box;
}

async function checkUpdates(manual) {
  const result = $("#update-result");
  if (result) result.textContent = "Söker…";
  const info = await api("GET", "/update-check").catch((e) => ({ error: String(e) }));
  state.updateInfo = info;
  renderUpdateResult(info, manual);
}

function renderUpdateResult(info, manual) {
  const result = $("#update-result");
  if (!result || !info) return;
  result.innerHTML = "";
  if (info.error) {
    if (manual) result.appendChild(el("p", { class: "muted" }, "Kunde inte kontrollera uppdateringar (offline?)."));
    return;
  }
  if (info.no_releases) {
    if (manual) result.appendChild(el("p", { class: "muted" },
      "Inga releaser är publicerade ännu – det finns inget att uppdatera till."));
    return;
  }
  if (!info.update_available) {
    if (manual) result.appendChild(el("p", { class: "muted" },
      "Du har den senaste versionen (" + (info.current || state.appVersion || "") + ")."));
    return;
  }
  result.appendChild(el("div", { class: "box", style: "border-left:4px solid var(--brand,#3a6ea5)" },
    el("div", { style: "font-weight:600" }, `Ny version ${info.latest} finns (du har ${info.current}).`),
    info.notes ? el("p", { class: "muted", style: "white-space:pre-wrap;max-height:120px;overflow:auto;margin:6px 0" }, info.notes) : null,
    el("div", { style: "margin-top:8px;display:flex;gap:8px;flex-wrap:wrap" },
      el("button", { class: "btn brand", onclick: () => guard(() => applyUpdate(info)) }, "Uppdatera nu"),
      info.html_url ? el("a", { class: "btn ghost", href: info.html_url, target: "_blank", rel: "noopener" }, "Visa release") : null)));
}

async function applyUpdate(info) {
  const res = await api("POST", "/update-apply", info).catch((e) => ({ applied: false, reason: String(e) }));
  if (res.applied) {
    toast("Uppdaterar… appen startar om automatiskt.");
  } else {
    toast(res.reason || "Uppdatering stöds inte i den här versionen.", true);
    if (info.html_url) window.open(info.html_url, "_blank");
  }
}

function maybeAutoCheckUpdates() {
  if (state.updateChecked || !autoUpdateOn()) return;
  state.updateChecked = true;
  checkUpdates(false);
}

// ---------------------------------------------------------------------------
// Workspace (active book)
// ---------------------------------------------------------------------------
const SECTIONS = [
  ["transactions", "Transaktioner"],
  ["record", "Bokför"],
  ["invoices", "Ordrar"],
  ["purchases", "Inköp"],
  ["articles", "Artiklar"],
  ["stock", "Lager"],
  ["customers", "Kunder"],
  ["suppliers", "Leverantörer"],
  ["categories", "BAS-konton"],
  ["rut", "RUT"],
  ["verifikat", "Verifikat"],
  ["huvudbok", "Huvudbok"],
  ["reports", "Rapporter"],
  ["arsbokslut", "Årsbokslut"],
  ["skatt", "Skatt"],
  ["bokslut", "Bokslut"],
  ["settings", "Inställningar"],
];

function renderWorkspace() {
  if (!state.activeBook) return renderHome();
  const v = $("#view");
  v.innerHTML = "";

  const nav = el("div", { class: "nav" });
  for (const [key, label] of SECTIONS) {
    nav.appendChild(el("button", {
      class: state.section === key ? "active" : "",
      onclick: () => { state.section = key; renderWorkspace(); },
    }, label));
  }
  nav.appendChild(el("div", { class: "spacer", style: "flex:1" }));
  nav.appendChild(el("button", { class: "", onclick: () => guard(lockActive) }, "🔒 Lås"));
  v.appendChild(nav);

  const panel = el("div", { class: "panel", id: "section" });
  v.appendChild(panel);
  guard(() => SECTION_RENDERERS[state.section](panel));
}

const bid = () => state.activeBook.id;

const SECTION_RENDERERS = {
  // ----- transactions -----
  async transactions(panel) {
    const [txs, cats] = await Promise.all([
      api("GET", `/books/${bid()}/transaktioner`),
      api("GET", `/books/${bid()}/categories`),
    ]);
    const catName = Object.fromEntries(cats.map((c) => [c.id, c.name]));
    panel.appendChild(el("h2", {}, "Transaktioner"));
    if (txs.length === 0) { panel.appendChild(el("p", { class: "muted" }, "Inga transaktioner ännu.")); return; }
    const rows = txs.map((t) => el("tr", {},
      el("td", {}, t.trans_date),
      el("td", {}, t.direction === "in" ? "Utgift" : "Inkomst"),
      el("td", {}, catName[t.category_id] || "—"),
      el("td", {}, el("span", { class: "pill " + t.status }, t.status === "paid" ? "Betald" : "Väntar")),
      el("td", { class: "num" }, t.verifikation_id ? ("ver " + t.verifikation_id) : ""),
      el("td", { class: "num" }, t.direction === "in"
        ? el("button", { class: "btn small ghost", onclick: () => guard(() => receiptsFlow(t.id, t.status === "pending")) }, "📎 Kvitto")
        : ""),
      el("td", { class: "num" }, t.status === "pending"
        ? el("button", { class: "btn small", onclick: () => guard(() => payFlow(t.id)) }, "Bokför betalning")
        : ""),
    ));
    panel.appendChild(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Datum"), el("th", {}, "Typ"), el("th", {}, "Kategori"),
        el("th", {}, "Status"), el("th", { class: "num" }, "Verifikat"),
        el("th", { class: "num" }, "Kvitto"), el("th", {}, ""))),
      el("tbody", {}, rows),
    ));
  },

  // ----- inköp (purchases: expenses + supplier invoices + receipts) -----
  async purchases(panel) {
    const [txs, cats, suppliers] = await Promise.all([
      api("GET", `/books/${bid()}/transaktioner`),
      api("GET", `/books/${bid()}/categories`),
      api("GET", `/books/${bid()}/suppliers`),
    ]);
    const catName = Object.fromEntries(cats.map((c) => [c.id, c.name]));
    const supName = Object.fromEntries(suppliers.map((s) => [s.id, s.name]));
    const list = txs.filter((t) => t.direction === "in");
    panel.appendChild(headerWithAdd("Inköp", "+ Nytt inköp", () => guard(() => purchaseForm(panel))));
    panel.appendChild(el("p", { class: "muted", style: "margin-top:6px" },
      "Inköp och utgifter till firman. Ange kvitto- eller fakturanummer och bifoga kvittot. "
      + "En leverantörsfaktura kan bokföras direkt och markeras som betald när den betalas."));
    if (list.length === 0) { panel.appendChild(el("p", { class: "muted" }, "Inga inköp ännu.")); return; }
    const actions = (t) => el("span", { style: "display:inline-flex;gap:4px" },
      el("button", { class: "btn small ghost", onclick: () => guard(() => receiptsFlow(t.id, t.status === "pending")) }, "📎 Kvitto"),
      t.status === "pending"
        ? el("button", { class: "btn small", onclick: () => guard(() => payFlow(t.id)) }, "Bokför betalning")
        : null);
    panel.appendChild(searchTable(
      "Sök inköp (leverantör, nr, datum, kategori)…",
      ["Datum", "Leverantör", "Kategori", "Kvitto/Faktura-nr", "Belopp", "Status", ""],
      list,
      (t, q) => [t.trans_date, supName[t.supplier_id] || "", catName[t.category_id] || "",
        t.ext_ref || ""].join(" ").toLowerCase().includes(q),
      (t) => [t.trans_date, t.supplier_id ? (supName[t.supplier_id] || "—") : "—",
        catName[t.category_id] || "—", t.ext_ref || "",
        toKr(t.amount_ore || 0) + " kr",
        el("span", { class: "pill " + (t.status === "paid" ? "paid" : "pending") },
          t.status === "paid" ? "Betald" : "Väntar"),
        actions(t)],
    ));
  },

  // ----- record income/expense -----
  async record(panel) {
    const [cats, customers, suppliers] = await Promise.all([
      api("GET", `/books/${bid()}/categories`),
      api("GET", `/books/${bid()}/customers`),
      api("GET", `/books/${bid()}/suppliers`),
    ]);
    panel.appendChild(el("h2", {}, "Bokför"));

    const kind = el("select", {},
      el("option", { value: "income" }, "Inkomst (försäljning)"),
      el("option", { value: "expense" }, "Utgift (inköp)"));
    const counter = el("select", {});
    const cat = el("select", {});
    const date = el("input", { type: "date", value: new Date().toISOString().slice(0, 10) });
    const paidNow = el("select", {}, el("option", { value: "yes" }, "Ja, betald nu"), el("option", { value: "no" }, "Nej, väntar"));
    const rut = el("input", { type: "text", value: "0,00" });

    // Multiple moms lines: a receipt can mix 6/12/25 %. Each row is rate + belopp.
    const linesEd = momsLinesEditor();
    // Receipt capture (expenses): import a file or take a photo.
    const receipt = receiptPicker();

    const rutRow = wrap("RUT-belopp (kr, endast inkomst)", rut);
    const receiptBlock = el("div", { style: "margin-top:6px" },
      el("label", {}, "Kvitto (utgift)"), receipt.element);

    const refreshFor = (k) => {
      counter.innerHTML = "";
      cat.innerHTML = "";
      if (k === "income") {
        for (const c of customers) counter.appendChild(el("option", { value: c.kundnummer },
          c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer)));
        for (const c of cats.filter((x) => x.kind === "income")) cat.appendChild(el("option", { value: c.id }, c.name));
      } else {
        counter.appendChild(el("option", { value: "" }, "— (ingen) —"));
        for (const s of suppliers) counter.appendChild(el("option", { value: s.id }, s.name));
        for (const c of cats.filter((x) => x.kind === "expense")) cat.appendChild(el("option", { value: c.id }, c.name));
      }
      rutRow.style.display = k === "income" ? "" : "none";
      receiptBlock.style.display = k === "expense" ? "" : "none";
    };
    kind.addEventListener("change", () => refreshFor(kind.value));
    refreshFor("income");

    const form = el("div", {},
      el("div", { class: "row" }, wrap("Typ", kind), wrap("Motpart", counter), wrap("Kategori", cat)),
      el("div", { class: "row" }, wrap("Datum", date), wrap("Betald?", paidNow), rutRow),
      el("div", { style: "margin-top:6px" }, el("label", {}, "Belopp & moms (en rad per momssats)"), linesEd.element),
      receiptBlock,
      el("div", { style: "margin-top:14px" }, el("button", { class: "btn brand", onclick: () => guard(submit) }, "Bokför")),
    );
    panel.appendChild(form);

    async function submit() {
      const lines = linesEd.getLines();
      if (lines.length === 0) { toast("Lägg till minst en belopps­rad", true); return; }
      const paid_date = paidNow.value === "yes" ? date.value : null;
      let warn = null;
      if (kind.value === "income") {
        const res = await api("POST", `/books/${bid()}/incomes`, {
          customer_id: parseInt(counter.value, 10), category_id: parseInt(cat.value, 10),
          lines, trans_date: date.value, rut_amount_ore: toOre(rut.value) || 0, paid_date,
        });
        const cap = res && res.rut_cap;
        if (cap && cap.over_cap) {
          warn = `Bokfört — VARNING: RUT-taket överskridet (${toKr(cap.used_ore)} av ${toKr(cap.cap_ore)} kr)`;
        } else if (cap && cap.near_cap) {
          warn = `Bokfört — OBS: nära RUT-taket (${toKr(cap.used_ore)} av ${toKr(cap.cap_ore)} kr)`;
        }
      } else {
        const res = await api("POST", `/books/${bid()}/expenses`, {
          supplier_id: counter.value ? parseInt(counter.value, 10) : null,
          category_id: parseInt(cat.value, 10), lines, trans_date: date.value, paid_date,
        });
        const staged = receipt.getStaged();
        if (staged) {
          await api("POST", `/books/${bid()}/transaktioner/${res.transaktion_id}/receipts`, {
            image_base64: staged.image_base64, mime: staged.mime, original_format: staged.original_format,
          });
        }
      }
      state.section = "transactions";
      renderWorkspace();
      toast(warn || "Bokfört", Boolean(warn));
    }
  },

  // ----- articles (reusable invoice line items) -----
  async articles(panel) {
    const [list, cats] = await Promise.all([
      api("GET", `/books/${bid()}/articles`),
      api("GET", `/books/${bid()}/categories`),
    ]);
    const incomeCats = cats.filter((c) => c.kind === "income");
    panel.appendChild(headerWithAdd("Artiklar", "+ Ny artikel",
      () => guard(() => addArticleFlow(incomeCats))));
    panel.appendChild(el("p", { class: "muted", style: "margin-top:6px" },
      "Återanvändbara artiklar för fakturarader. Artikelnummer xxxx-xxxx (du väljer de "
      + "4 första siffrorna, resten slumpas). Priset går alltid att ändra på fakturan."));
    if (list.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inga artiklar ännu."));
      return;
    }
    // Category filter: "alla" + one option per category actually used by articles.
    const usedCatIds = [...new Set(list.map((a) => a.category_id).filter((x) => x != null))];
    const catFilter = el("select", { style: "max-width:280px;margin-top:8px" },
      el("option", { value: "" }, "— Alla kategorier —"),
      el("option", { value: "none" }, "Okategoriserade"),
      ...incomeCats.filter((c) => usedCatIds.includes(c.id))
        .map((c) => el("option", { value: c.id }, `${c.prefix || "?"} · ${c.name}`)));
    catFilter.value = state.articlesCat || "";
    const tableBox = el("div", {});
    const draw = () => {
      const v = catFilter.value;
      const rows = list.filter((a) => v === "" ? true
        : v === "none" ? a.category_id == null : String(a.category_id) === v);
      tableBox.innerHTML = "";
      tableBox.appendChild(simpleTable(
        ["Artikelnr", "Beskrivning", "À-pris", "Moms", "Husavdrag", "Kategori", ""],
        rows.map((a) => [a.article_number, a.description, toKr(a.unit_price_ore) + " kr",
          rateLabel(a.rate_code), a.reduction_type ? a.reduction_type.toUpperCase() : "—",
          a.category_name || el("span", { class: "muted" }, "Okategoriserad"),
          el("span", { style: "display:inline-flex;gap:4px" },
            editBtn(() => guard(() => editArticleFlow(a, incomeCats))),
            el("button", { class: "btn small ghost danger", onclick: () => guard(async () => {
              const f = await modal(`Ta bort artikel ${a.article_number}?`, [], "Ta bort");
              if (!f) return;
              await api("DELETE", `/books/${bid()}/articles/${a.id}`);
              toast("Artikel borttagen"); renderWorkspace();
            }) }, "Ta bort"))])));
    };
    catFilter.onchange = () => { state.articlesCat = catFilter.value; draw(); };
    panel.appendChild(wrap("Filtrera på kategori", catFilter));
    panel.appendChild(tableBox);
    draw();
  },

  // ----- stock / lager -----
  async stock(panel) {
    const [stock, articles, suppliers] = await Promise.all([
      api("GET", `/books/${bid()}/stock`),
      api("GET", `/books/${bid()}/articles`),
      api("GET", `/books/${bid()}/suppliers`),
    ]);
    panel.appendChild(headerWithAdd("Lager", "+ Lägg till i lager",
      () => guard(() => addStockBatchFlow(articles, suppliers))));
    panel.appendChild(el("p", { class: "muted", style: "margin-top:6px" },
      "Varje inköp av en artikel blir en batch med sin egen kostnad. Köper du samma artikel "
      + "igen får den en ny batch (samma artikel kan ha flera kostnader). Batchnumret syns "
      + "bara här. När du väljer en batch på en fakturarad ser du den verkliga marginalen."));
    if (stock.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inget i lager ännu."));
      return;
    }
    const totalValue = stock.reduce((s, r) => s + (r.value_ore || 0), 0);
    for (const r of stock) {
      const details = el("div", { style: "display:none;padding:6px 0 12px 12px" });
      let loaded = false;
      const toggle = el("button", { class: "btn small ghost", onclick: () => guard(async () => {
        const open = details.style.display === "none";
        details.style.display = open ? "block" : "none";
        if (open && !loaded) {
          loaded = true;
          details.appendChild(await stockBatchesTable(r.article_id, suppliers));
        }
      }) }, "Batchar ▾");
      const head = el("div", {
        style: "display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)",
      },
        el("strong", { style: "min-width:90px" }, r.article_number),
        el("span", { style: "flex:1" }, r.description),
        el("span", { class: "num" }, `${(r.qty_remaining_centi / 100).toLocaleString("sv-SE")} ${r.unit || "st"}`),
        el("span", { class: "muted", style: "min-width:70px;text-align:right" }, `${r.batch_count} batch`),
        el("span", { class: "num", style: "min-width:110px;text-align:right", title: "Lagervärde (inköp)" },
          toKr(r.value_ore) + " kr"),
        toggle);
      panel.appendChild(head);
      panel.appendChild(details);
    }
    panel.appendChild(el("p", { style: "margin-top:12px;text-align:right;font-weight:600" },
      "Totalt lagervärde: " + toKr(totalValue) + " kr"));
  },

  // ----- customers -----
  async customers(panel) {
    const list = await api("GET", `/books/${bid()}/customers`);
    panel.appendChild(headerWithAdd("Kunder", "+ Ny kund", () => guard(addCustomerFlow)));
    const cname = (c) => c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim();
    const actions = (c) => el("span", { style: "display:inline-flex;gap:4px" },
      el("button", { class: "btn small ghost", title: "Skapa faktura till denna kund",
        onclick: () => newInvoiceForCustomer(c.kundnummer) }, "Ny faktura"),
      el("button", { class: "btn small ghost", title: "Gratis distanssupport – saldo & uttag",
        onclick: () => guard(() => supportFlow(c)) }, "Support"),
      editBtn(() => guard(() => editCustomerFlow(c.kundnummer))),
      c.type === "private"
        ? el("button", { class: "btn small ghost", onclick: () => guard(() => householdFlow(c)) }, "Hushåll")
        : null);
    panel.appendChild(searchTable(
      "Sök kund (namn, nr, org/pers.nr, e-post)…",
      ["Nr", "Typ", "Namn", "Org/Pers", "E-post", "Spenderat", ""],
      list,
      (c, q) => [String(c.kundnummer), cname(c), c.org_nr || "",
        c.email || "", c.phone || ""].join(" ").toLowerCase().includes(q),
      (c) => [c.kundnummer, c.type, cname(c), c.org_nr || "", c.email || "",
        el("span", { class: "num", title: "Totalt fakturerat (ej makulerat)" },
          toKr(c.invoiced_ore || 0) + " kr"), actions(c)],
    ));
  },

  // ----- suppliers -----
  async suppliers(panel) {
    const list = await api("GET", `/books/${bid()}/suppliers`);
    panel.appendChild(headerWithAdd("Leverantörer", "+ Ny leverantör", () => guard(addSupplierFlow)));
    panel.appendChild(simpleTable(
      ["Namn", "Moms", "Org.nr", ""],
      list.map((s) => [s.name, s.default_moms_rate, s.org_nr || "",
        editBtn(() => guard(() => editSupplierFlow(s)))]),
    ));
  },

  // ----- BAS-konton (categories + the system accounts the engine books to) -----
  async categories(panel) {
    const [list, accounts] = await Promise.all([
      api("GET", `/books/${bid()}/categories`),
      api("GET", `/books/${bid()}/accounts`),
    ]);
    panel.appendChild(headerWithAdd("BAS-konton", "+ Ny kategori", () => guard(addCategoryFlow)));

    // User categories (each a name + BAS-konto + default moms).
    panel.appendChild(el("h3", { style: "margin-top:8px" }, "Kategorier"));
    if (list.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inga kategorier ännu."));
    } else {
      panel.appendChild(el("p", { class: "muted" },
        "Ett BAS-konto som ännu inte använts kan tas bort. Har det bokförts måste det "
        + "vara kvar (legal spårbarhet) — inaktivera det istället."));
      panel.appendChild(simpleTable(
        ["Namn", "Typ", "BAS-konto", "Standardmoms", "Status", ""],
        list.map((c) => [c.name, c.kind === "income" ? "Inkomst" : "Utgift", c.bas_konto,
          rateLabel(c.default_rate_code),
          el("span", { class: "pill " + (c.active ? "paid" : "") }, c.active ? "Aktiv" : "Inaktiv"),
          categoryActions(c)]),
      ));
    }

    // System BAS-konton used by the booking engine (bank, moms, fordringar, …).
    panel.appendChild(el("h3", { style: "margin-top:22px" }, "Systemkonton"));
    panel.appendChild(el("p", { class: "muted" },
      "Konton som bokföringen använder automatiskt (bank, moms, kundfordringar m.m.). "
      + "Visas här för insyn."));
    const sys = accounts.filter((a) => a.is_system);
    panel.appendChild(simpleTable(
      ["BAS-konto", "Benämning", "Roll"],
      sys.map((a) => [a.bas_konto, a.name, a.system_label || "—"]),
    ));
  },

  // ----- reports -----
  async reports(panel) {
    panel.appendChild(el("h2", {}, "Rapporter"));
    const start = el("input", { type: "date", value: "2026-01-01" });
    const end = el("input", { type: "date", value: "2026-03-31" });
    const out = el("div", {});
    panel.appendChild(el("div", { class: "row" },
      wrap("Från", start), wrap("Till", end),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn", onclick: () => guard(run) }, "Momsdeklaration")),
    ));
    panel.appendChild(out);

    // ---- SIE export (with optional company/org and fiscal year for balances) ----
    panel.appendChild(el("h3", { style: "margin-top:26px" }, "SIE-export"));
    panel.appendChild(el("p", { class: "muted" },
      "Företagsnamn och org.nr skrivs i filhuvudet. Anges ett räkenskapsår tas även "
      + "ingående/utgående balanser och resultat med (#IB/#UB/#RES)."));
    const company = el("input", { type: "text", value: state.activeBook.display_name || "" });
    const orgnr = el("input", { type: "text", value: "" });
    const fyStart = el("input", { type: "date", value: "" });
    const fyEnd = el("input", { type: "date", value: "" });
    panel.appendChild(el("div", { class: "row" },
      wrap("Företagsnamn", company), wrap("Org.nr", orgnr)));
    panel.appendChild(el("div", { class: "row" },
      wrap("Räkenskapsår från (valfritt)", fyStart), wrap("Till (valfritt)", fyEnd),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn ghost", onclick: () => guard(sie) }, "Exportera SIE")),
    ));

    async function run() {
      const rep = await api("GET", `/books/${bid()}/reports/momsdeklaration?start=${start.value}&end=${end.value}`);
      out.innerHTML = "";
      const boxes = [
        ["05", "Försäljning exkl. moms"], ["10", "Utg. moms 25%"], ["11", "Utg. moms 12%"],
        ["12", "Utg. moms 6%"], ["48", "Ing. moms"], ["49", "Att betala/återfå"],
      ];
      out.appendChild(el("div", { class: "boxgrid" }, boxes.map(([k, label]) =>
        el("div", { class: "box" }, el("div", { class: "k" }, `Ruta ${k} — ${label}`),
          el("div", { class: "v" }, toKr(rep.boxes[k]) + " kr")))));
    }
    async function sie() {
      const q = new URLSearchParams();
      if (company.value) q.set("company_name", company.value);
      if (orgnr.value) q.set("org_nr", orgnr.value);
      if (fyStart.value) q.set("fiscal_year_start", fyStart.value);
      if (fyEnd.value) q.set("fiscal_year_end", fyEnd.value);
      const text = await api("GET", `/books/${bid()}/reports/sie?${q.toString()}`);
      const blob = new Blob([text], { type: "text/plain" });
      const a = el("a", { href: URL.createObjectURL(blob), download: "bokforing.se" });
      document.body.appendChild(a); a.click(); a.remove();
      toast("SIE-fil nedladdad");
    }
  },

  // ----- RUT (husavdrag) -----
  async rut(panel) {
    const [claims, customers, invoices] = await Promise.all([
      api("GET", `/books/${bid()}/rut-claims`),
      api("GET", `/books/${bid()}/customers`),
      api("GET", `/books/${bid()}/invoices`),
    ]);
    const custName = Object.fromEntries(customers.map((c) =>
      [c.kundnummer, c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer)]));
    const STATE_LABEL = {
      pending: "Väntar på kund", customer_paid: "Kund betald", skatteverket_paid: "Skatteverket betald",
    };
    panel.appendChild(el("h2", {}, "RUT-avdrag (husavdrag)"));
    panel.appendChild(el("p", { class: "muted" },
      "Livscykel: väntar → kund betald → Skatteverket betald. Bokför Skatteverkets "
      + "utbetalning när husavdraget kommit in (egen verifikation)."));

    // Worklist: invoices whose customer has paid but Skatteverket has not yet paid the
    // husavdrag (state "awaiting_rut"). Book the payout straight from here.
    const awaiting = invoices.filter((iv) => iv.state === "awaiting_rut");
    panel.appendChild(el("h3", { style: "margin-top:18px" }, "Inväntar husavdrag från Skatteverket"));
    if (awaiting.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inga fakturor väntar på husavdrag just nu."));
    } else {
      panel.appendChild(el("table", { style: "margin-bottom:10px" },
        el("thead", {}, el("tr", {},
          el("th", {}, "Faktura"), el("th", {}, "Kund"), el("th", {}, "Typ"),
          el("th", { class: "num" }, "Husavdrag"), el("th", {}, ""))),
        el("tbody", {}, awaiting.map((iv) => {
          const husavdrag = iv.rut_total_ore + iv.rot_total_ore;
          const typ = iv.rut_total_ore > 0 && iv.rot_total_ore > 0 ? "RUT/ROT"
            : iv.rot_total_ore > 0 ? "ROT" : "RUT";
          return el("tr", {},
            el("td", { class: "num" }, String(iv.invoice_number)),
            el("td", {}, custName[iv.customer_id] || ("Kund " + iv.customer_id)),
            el("td", {}, el("span", { class: "pill awaiting" }, "Inväntar " + typ)),
            el("td", { class: "num" }, toKr(husavdrag) + " kr"),
            el("td", { class: "num" }, iv.rut_claim_id
              ? el("button", { class: "btn small",
                  onclick: () => guard(() => rutSkvPayFlow(iv.rut_claim_id, husavdrag)) },
                  "Bokför husavdrag (Skatteverket)")
              : ""));
        }))));
    }

    panel.appendChild(el("h3", { style: "margin-top:22px" }, "Alla RUT/ROT-ärenden"));
    if (claims.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inga RUT-ärenden ännu."));
      return;
    }
    const rows = claims.map((c) => {
      const actions = el("td", { class: "num" });
      if (c.state === "customer_paid") {
        actions.appendChild(el("button", { class: "btn small",
          onclick: () => guard(() => rutSkvPayFlow(c.id, c.rut_amount_ore)) }, "Bokför SKV-utbetalning"));
      } else if (c.state === "skatteverket_paid") {
        actions.appendChild(el("button", { class: "btn small ghost",
          onclick: () => guard(() => rutKvittensFlow(c)) }, "📎 Kvittens"));
      }
      return el("tr", {},
        el("td", {}, String(c.id)),
        el("td", {}, custName[c.customer_id] || ("Kund " + c.customer_id)),
        el("td", {}, c.skatteverket_reference || ""),
        el("td", { class: "num" }, toKr(c.rut_amount_ore) + " kr"),
        el("td", {}, el("span", { class: "pill " + (c.state === "skatteverket_paid" ? "paid" : c.state === "customer_paid" ? "" : "pending") },
          STATE_LABEL[c.state] || c.state)),
        el("td", {}, c.customer_payment_date || "—"),
        el("td", {}, c.skatteverket_payment_date || "—"),
        actions);
    });
    panel.appendChild(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Nr"), el("th", {}, "Kund"), el("th", {}, "Begäran"),
        el("th", { class: "num" }, "Belopp"),
        el("th", {}, "Status"), el("th", {}, "Kund betald"), el("th", {}, "SKV betald"), el("th", {}, ""))),
      el("tbody", {}, rows),
    ));
  },

  // ----- verifikat (the legal ledger; reverse = rättelse) -----
  async verifikat(panel) {
    const vers = await api("GET", `/books/${bid()}/verifikationer`);
    panel.appendChild(el("h2", {}, "Verifikationer"));
    panel.appendChild(el("p", { class: "muted" },
      "Bokförda verifikationer kan inte ändras eller raderas. Fel rättas med en "
      + "rättelse (en ny verifikation som speglar den ursprungliga)."));
    if (vers.length === 0) {
      panel.appendChild(el("p", { class: "muted" }, "Inga verifikationer ännu."));
      return;
    }
    const rows = vers.map((v) => el("tr", {},
      el("td", { class: "num" }, `${v.series}${v.ver_number}`),
      el("td", {}, v.ver_date),
      el("td", {}, v.text || ""),
      el("td", {}, v.rattelse_of ? el("span", { class: "pill" }, "rättelse av ver " + v.rattelse_of) : ""),
      el("td", { class: "num" }, (v.posted && !v.rattelse_of)
        ? el("button", { class: "btn small", onclick: () => guard(() => reverseFlow(v.id, `${v.series}${v.ver_number}`)) }, "Rätta")
        : ""),
    ));
    panel.appendChild(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { class: "num" }, "Ver"), el("th", {}, "Datum"), el("th", {}, "Text"),
        el("th", {}, ""), el("th", {}, ""))),
      el("tbody", {}, rows),
    ));
  },

  // ----- huvudbok / grundbok preview + manual journal entry -----
  async huvudbok(panel) {
    const accounts = await api("GET", `/books/${bid()}/accounts`);
    panel.appendChild(el("h2", {}, "Bokföring (huvudbok / grundbok)"));
    panel.appendChild(el("p", { class: "muted" },
      "Förhandsgranska bokföringen. Grundbok = verifikationer med konteringar i "
      + "nummerordning; Huvudbok = per konto med saldo. Manuella verifikationer bokförs "
      + "direkt här (fristående från fakturor) — t.ex. för att rätta något manuellt."));

    const start = el("input", { type: "date", value: "" });
    const end = el("input", { type: "date", value: "" });
    const viewSel = el("select", {},
      el("option", { value: "grundbok" }, "Grundbok (verifikationer)"),
      el("option", { value: "huvudbok" }, "Huvudbok (per konto)"));
    const out = el("div", {});
    panel.appendChild(el("div", { class: "row" },
      wrap("Vy", viewSel), wrap("Från (valfritt)", start), wrap("Till (valfritt)", end),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn", onclick: () => guard(draw) }, "Visa")),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn brand", onclick: () => guard(() => manualVerForm(panel, accounts)) },
          "+ Ny manuell verifikation"))));
    panel.appendChild(out);

    const qs = () => {
      const p = [];
      if (start.value) p.push("start=" + start.value);
      if (end.value) p.push("end=" + end.value);
      return p.length ? "?" + p.join("&") : "";
    };
    async function draw() {
      out.innerHTML = "";
      if (viewSel.value === "huvudbok") {
        const data = await api("GET", `/books/${bid()}/huvudbok${qs()}`);
        if (!data.length) { out.appendChild(el("p", { class: "muted" }, "Inga konteringar.")); return; }
        for (const a of data) {
          out.appendChild(el("h3", { style: "margin-top:18px" },
            `${a.bas_konto} ${a.konto_namn} `,
            el("span", { class: "muted", style: "font-weight:400" },
              `— saldo ${toKr(a.saldo_ore)} kr (debet ${toKr(a.debit_ore)} / kredit ${toKr(a.credit_ore)})`)));
          out.appendChild(el("table", {},
            el("thead", {}, el("tr", {},
              el("th", {}, "Ver"), el("th", {}, "Datum"), el("th", {}, "Text"),
              el("th", { class: "num" }, "Debet"), el("th", { class: "num" }, "Kredit"),
              el("th", { class: "num" }, "Saldo"))),
            el("tbody", {}, a.lines.map((l) => el("tr", {},
              el("td", { class: "num" }, l.ver), el("td", {}, l.ver_date),
              el("td", {}, l.text || l.ver_text || ""),
              el("td", { class: "num" }, l.amount_ore > 0 ? toKr(l.amount_ore) : ""),
              el("td", { class: "num" }, l.amount_ore < 0 ? toKr(-l.amount_ore) : ""),
              el("td", { class: "num" }, toKr(l.saldo_ore)))))));
        }
      } else {
        const vers = await api("GET", `/books/${bid()}/verifikationer-full${qs()}`);
        if (!vers.length) { out.appendChild(el("p", { class: "muted" }, "Inga verifikationer.")); return; }
        for (const v of vers) {
          out.appendChild(el("h3", { style: "margin-top:18px" },
            `${v.series}${v.ver_number} `,
            el("span", { class: "muted", style: "font-weight:400" }, `${v.ver_date} — ${v.text || ""}`),
            v.rattelse_of ? el("span", { class: "pill", style: "margin-left:6px" }, "rättelse") : null));
          out.appendChild(el("table", {},
            el("thead", {}, el("tr", {},
              el("th", {}, "Konto"), el("th", {}, "Text"),
              el("th", { class: "num" }, "Debet"), el("th", { class: "num" }, "Kredit"))),
            el("tbody", {}, v.postings.map((p) => el("tr", {},
              el("td", {}, `${p.bas_konto} ${p.konto_namn}`),
              el("td", {}, p.text || ""),
              el("td", { class: "num" }, p.amount_ore > 0 ? toKr(p.amount_ore) : ""),
              el("td", { class: "num" }, p.amount_ore < 0 ? toKr(-p.amount_ore) : ""))))));
        }
      }
    }
    await draw();
  },

  // ----- Förenklat årsbokslut (SKV 2150) -----
  // Editable annual tax figures (mirrors backend CONFIG_FIELDS): [key, kind, label].
  // kind 'pct' shows/stores centi-percent as %, 'ore' shows/stores öre as kr.
  _taxFields: [
    ["prisbasbelopp_ore", "ore", "Prisbasbelopp (kr)"],
    ["kommunal_skattesats_pct_centi", "pct", "Kommunalskatt %"],
    ["begravningsavgift_pct_centi", "pct", "Begravningsavgift %"],
    ["egenavgift_pct_centi", "pct", "Egenavgifter %"],
    ["egenavgift_nedsattning_pct_centi", "pct", "Nedsättning egenavg. %"],
    ["egenavgift_nedsattning_max_ore", "ore", "Nedsättning max (kr)"],
    ["public_service_pct_centi", "pct", "Public service %"],
    ["public_service_max_ore", "ore", "Public service max (kr)"],
    ["statlig_skatt_pct_centi", "pct", "Statlig skatt %"],
    ["statlig_skiktgrans_ore", "ore", "Skiktgräns (kr)"],
    ["skattered_forvarv_max_ore", "ore", "Skattered. förvärv max (kr)"],
  ],
  // Jobbskatteavdrag formula coefficients (kind 'coef' = fraction, stored × 10000).
  _taxJsaFields: [
    ["jsa_break1_centi", "coef", "Brytpunkt 1 (× PBB)"],
    ["jsa_break2_centi", "coef", "Brytpunkt 2 (× PBB)"],
    ["jsa_break3_centi", "coef", "Brytpunkt 3 (× PBB)"],
    ["jsa_c2_centi", "coef", "Lutning intervall 2"],
    ["jsa_c3_centi", "coef", "Lutning intervall 3"],
    ["jsa_b3_base_centi", "coef", "Belopp start intervall 3 (× PBB)"],
    ["jsa_b4_level_centi", "coef", "Belopp intervall 4 (× PBB)"],
  ],

  async skatt(panel) {
    const year = new Date().getFullYear();
    const cfg = await api("GET", `/books/${bid()}/tax-config`);
    const scaleFor = (kind) => kind === "coef" ? 10000 : kind === "year" ? 1 : 100;
    const fyStart = el("input", { type: "date", value: `${year}-01-01` });
    const fyEnd = el("input", { type: "date", value: `${year}-12-31` });
    const fYear = el("input", { type: "number", step: "1", style: "width:90px",
      value: String(cfg.tax_values_year || year) });
    const fSalary = el("input", { type: "number", step: "1", style: "width:150px",
      value: String((cfg.ovrig_forvarvsinkomst_ore || 0) / 100) });
    const out = el("div", {});
    panel.appendChild(el("h2", {}, "Skatt att betala (uppskattning)"));
    panel.appendChild(el("p", { class: "muted" },
      "Enskild näringsidkare. Uppskattar vad du bör sätta undan till Skatteverket för din "
      + "firma — moms, egenavgifter och inkomstskatt. Din lön från en anställning beskattas "
      + "av arbetsgivaren, men påverkar vilken marginalskatt firmans överskott hamnar på "
      + "(grundavdrag, jobbskatteavdrag, statlig skatt) — fyll därför i din lön för rätt "
      + "beräkning. Ett hjälpmedel, inte en deklaration: stäm av mot Skatteverkets ”Räkna "
      + "ut din skatt”. Satserna sparas per bok och gäller tills du ändrar dem — du "
      + "behöver alltså inte uppdatera något varje år, men bör göra det när nya värden "
      + "kommer (Skatteverkets ”Belopp och procent” + regeringens ”Beräkningskonventioner”)."));
    panel.appendChild(el("div", { class: "row" },
      wrap("Räkenskapsår fr.o.m.", fyStart), wrap("t.o.m.", fyEnd),
      wrap("Lön / övrig förvärvsinkomst (kr/år)", fSalary),
      wrap("Satserna gäller år", fYear),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn brand", onclick: () => guard(draw) }, "Visa"))));

    // --- editable annual figures ---
    const inputs = {};
    const mkGrid = (fields) => {
      const grid = el("div", { class: "row" });
      for (const [key, kind, label] of fields) {
        const scale = scaleFor(kind);
        const inp = el("input", { type: "number", step: kind === "coef" ? "0.0001" : kind === "pct" ? "0.01" : "1",
          value: String((cfg[key] || 0) / scale), style: "width:150px" });
        inp._scale = scale;
        inputs[key] = inp;
        grid.appendChild(wrap(label, inp));
      }
      return grid;
    };
    panel.appendChild(el("details", { style: "margin:14px 0" },
      el("summary", { style: "cursor:pointer;font-weight:600" }, "Skattesatser & belopp (redigerbara, uppdatera årligen)"),
      el("p", { class: "muted" }, "Ange procent (t.ex. 30,55) och kronor. Värdena gäller tills "
        + "du ändrar dem."),
      mkGrid(SECTION_RENDERERS._taxFields),
      el("div", { style: "margin-top:8px" },
        el("button", { class: "btn", onclick: () => guard(save) }, "Spara satser"))));
    panel.appendChild(el("details", { style: "margin:14px 0" },
      el("summary", { style: "cursor:pointer;font-weight:600" }, "Jobbskatteavdrag – formelkoefficienter (avancerat)"),
      el("p", { class: "muted" }, "Koefficienterna i Beräkningskonventionernas Tabell 2.10 "
        + "(uttryckta i prisbasbelopp). Ändra bara när en ny årsformel publiceras."),
      mkGrid(SECTION_RENDERERS._taxJsaFields),
      el("div", { style: "margin-top:8px" },
        el("button", { class: "btn", onclick: () => guard(save) }, "Spara koefficienter"))));
    panel.appendChild(out);

    function collectConfig() {
      const num = (inp) => parseFloat((inp.value || "0").replace(",", "."));
      const body = { ovrig_forvarvsinkomst_ore: Math.round(num(fSalary) * 100),
                     tax_values_year: Math.round(num(fYear)) };
      for (const key of Object.keys(inputs)) body[key] = Math.round(num(inputs[key]) * inputs[key]._scale);
      return body;
    }
    async function save() { await api("PUT", `/books/${bid()}/tax-config`, collectConfig()); toast("Skattesatser sparade"); await draw(); }

    const kv = (k, ore, big) => el("div", {},
      el("div", { class: "k" }, k),
      el("div", { class: "v", style: big ? "font-weight:700;font-size:18px" : "" }, toKr(ore) + " kr"));
    const row = (label, ore, note, bold) => el("tr", { style: bold ? "border-top:2px solid var(--border,#ccc)" : "" },
      el("td", { style: bold ? "font-weight:700" : "" }, label),
      el("td", { class: "num", style: "font-variant-numeric:tabular-nums" + (bold ? ";font-weight:700" : "") }, toKr(ore) + " kr"),
      el("td", { class: "muted", style: "font-size:12px" }, note || ""));

    async function draw() {
      out.innerHTML = "";
      // persist the salary/rates the user typed, then compute
      await api("PUT", `/books/${bid()}/tax-config`, collectConfig());
      const r = await api("GET", `/books/${bid()}/reports/tax?start=${fyStart.value}&end=${fyEnd.value}`);
      // Firma: what to set aside
      out.appendChild(el("div", { class: "box", style: "margin-top:14px" },
        el("div", { class: "row" },
          kv("Firmans överskott", r.overskott_ore),
          kv("Att sätta undan (firman)", r.firma_total_ore, true))));
      out.appendChild(el("table", { style: "width:100%;margin-top:14px" },
        el("thead", {}, el("tr", {}, el("th", {}, "Firmans skatt"), el("th", { class: "num" }, "Belopp"), el("th", {}, ""))),
        el("tbody", {}, r.lines.map((l) => row(l.label, l.amount_ore, l.note)),
          row("Att sätta undan totalt (firman)", r.firma_total_ore, "moms + egenavgifter + firmans inkomstskatt", true))));
      // Overview (when a salary is entered). Income tax only — moms is shown separately
      // below, since moms is not a förvärvsinkomstskatt.
      const ov = r.overview;
      if (r.ovrig_forvarvsinkomst_ore) {
        const momsOre = Math.max(0, r.moms_ore);
        out.appendChild(el("h3", { style: "margin-top:24px" }, "Överblick (firma + lön)"));
        out.appendChild(el("p", { class: "muted" },
          "Inkomstskatt på firma + lön. Din arbetsgivare drar skatten på lönen; firmans del "
          + "betalar du själv via F-skatt/slutskatt. Momsen är ingen inkomstskatt utan visas "
          + "separat nedan."));
        out.appendChild(el("div", { class: "box" }, el("div", { class: "row" },
          kv("Total förvärvsinkomst", ov.forvarvsinkomst_ore),
          kv("Inkomstskatt (firma + lön)", ov.total_skatt_ore),
          kv("Varav arbetsgivaren drar (lön)", ov.salary_skatt_ore),
          kv("Varav firman (du betalar)", r.firma_tax_ore, true))));
        // Firma: skatt + moms separated, tying back to "att sätta undan totalt"
        out.appendChild(el("div", { class: "box", style: "margin-top:10px" }, el("div", { class: "row" },
          kv("Firmans skatt (inkomstskatt + egenavgifter)", r.firma_tax_ore),
          kv("+ Moms (redovisas separat per momsperiod)", momsOre),
          kv("= Firman att sätta undan totalt", r.firma_total_ore, true))));
      }
    }
    draw();
  },

  async arsbokslut(panel) {
    const company = await api("GET", `/books/${bid()}/company`).catch(() => ({}));
    const year = new Date().getFullYear();
    const fyStart = el("input", { type: "date", value: `${year}-01-01` });
    const fyEnd = el("input", { type: "date", value: `${year}-12-31` });
    const out = el("div", {});
    panel.appendChild(el("h2", {}, "Förenklat årsbokslut (SKV 2150)"));
    panel.appendChild(el("p", { class: "muted" },
      "Enskilda näringsidkare. Värdena hämtas ur din bokföring och placeras i "
      + "blankettens rutor (B1–B16, R1–R11). Detta är ett hjälpmedel — kontrollera mot "
      + "din kontoplan. Årsbokslutet lämnas inte in, men sparas i 7 år. Håll muspekaren "
      + "över en ruta för att se vilka konton som ingår."));
    panel.appendChild(el("div", { class: "row" },
      wrap("Räkenskapsår fr.o.m.", fyStart), wrap("t.o.m.", fyEnd),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn brand", onclick: () => guard(draw) }, "Visa årsbokslut"))));
    panel.appendChild(out);

    // A single box row: number, label, amount (right-aligned); title lists the konton.
    const boxRow = (b, opts = {}) => {
      const accs = (b.accounts || []).filter((a) => a.amount_ore)
        .map((a) => `${a.bas_konto != null ? a.bas_konto + " " : ""}${a.name}: ${toKr(a.amount_ore)}`).join("\n");
      return el("tr", { title: accs || "Inga konton" },
        el("td", { class: "num", style: "width:44px;color:var(--muted,#888)" }, b.box),
        el("td", {}, b.label),
        el("td", { class: "num", style: "width:120px;font-variant-numeric:tabular-nums" + (opts.bold ? ";font-weight:700" : "") },
          b.value_ore || opts.showZero ? toKr(b.value_ore) : ""));
    };
    const sumRow = (label, ore) => el("tr", { style: "border-top:2px solid var(--border,#ccc)" },
      el("td", {}, ""), el("td", { style: "font-weight:700" }, label),
      el("td", { class: "num", style: "font-weight:700;font-variant-numeric:tabular-nums" }, toKr(ore) + " ="));
    const groupHead = (t) => el("tr", {}, el("td", { colspan: "3", style: "padding-top:10px;font-weight:600;color:var(--muted,#666)" }, t));
    const mkTable = (...rows) => el("table", { style: "width:100%" }, el("tbody", {}, rows.flat()));

    async function draw() {
      out.innerHTML = "";
      const r = await api("GET", `/books/${bid()}/reports/arsbokslut`
        + `?start=${fyStart.value}&end=${fyEnd.value}`);
      const B = r.balans, R = r.resultat, U = r.upplysningar;

      // Header block (name / org / räkenskapsår)
      out.appendChild(el("div", { class: "box", style: "margin-top:14px" },
        el("div", { class: "row" },
          el("div", {}, el("div", { class: "k" }, "Namn"), el("div", { class: "v" }, company.name || "—")),
          el("div", {}, el("div", { class: "k" }, "Person-/organisationsnummer"), el("div", { class: "v" }, company.org_nr || "—")),
          el("div", {}, el("div", { class: "k" }, "Räkenskapsår"), el("div", { class: "v" }, `${r.fiscal_year_start} – ${r.fiscal_year_end}`)))));

      const balanserar = el("div", { class: "box", style: "margin-top:12px;font-weight:600;color:" + (r.balanserar ? "var(--ok,#2a7)" : "var(--danger,#c33)") },
        r.balanserar ? "✓ Balansräkningen balanserar (summa tillgångar = summa eget kapital och skulder)."
          : `⚠ Balansräkningen balanserar inte — differens ${toKr(r.diff_ore)} kr.`);

      // Two columns: Balansräkning | Resultaträkning
      const balans = el("div", { style: "flex:1;min-width:320px" },
        el("h3", {}, "Balansräkning"),
        mkTable(
          groupHead("Anläggningstillgångar"),
          ["B1", "B2", "B3", "B4", "B5"].map((k) => boxRow(B[k])),
          groupHead("Omsättningstillgångar"),
          ["B6", "B7", "B8", "B9"].map((k) => boxRow(B[k])),
          sumRow("Summa tillgångar", r.summa_tillgangar_ore),
          groupHead("Eget kapital"), boxRow(B.B10),
          groupHead("Obeskattade reserver"), boxRow(B.B11),
          groupHead("Skulder"),
          ["B13", "B14", "B15", "B16"].map((k) => boxRow(B[k])),
          sumRow("Summa eget kapital och skulder", r.summa_ek_skulder_ore)));

      const resultat = el("div", { style: "flex:1;min-width:320px" },
        el("h3", {}, "Resultaträkning"),
        mkTable(
          groupHead("Intäkter"),
          ["R1", "R2", "R3", "R4"].map((k) => boxRow(R[k])),
          groupHead("Kostnader"),
          ["R5", "R6", "R7", "R8"].map((k) => boxRow(R[k])),
          groupHead("Avskrivningar"),
          ["R9", "R10"].map((k) => boxRow(R[k])),
          groupHead("Årets resultat"), boxRow(R.R11, { bold: true, showZero: true })),
        el("h3", { style: "margin-top:18px" }, "Upplysningar"),
        mkTable(["U1", "U2", "U3", "U4"].map((k) =>
          el("tr", { }, el("td", { class: "num", style: "width:44px;color:var(--muted,#888)" }, k),
            el("td", {}, U[k].label),
            el("td", { class: "num", style: "width:120px" }, U[k].value_ore ? toKr(U[k].value_ore) : "")))));

      out.appendChild(balanserar);
      out.appendChild(el("div", { class: "row", style: "gap:28px;align-items:flex-start;margin-top:10px" }, balans, resultat));
    }
    await draw();
  },

  // ----- bokslut (period locking + year-end accruals) -----
  async bokslut(panel) {
    panel.appendChild(el("h2", {}, "Bokslut & perioder"));

    // Period locking
    panel.appendChild(el("h3", {}, "Lås period"));
    panel.appendChild(el("p", { class: "muted" },
      "När en momsdeklaration är inlämnad: lås perioden så inget kan bakdateras in i den."));
    const lockStart = el("input", { type: "date", value: "2026-01-01" });
    const lockEnd = el("input", { type: "date", value: "2026-03-31" });
    panel.appendChild(el("div", { class: "row" },
      wrap("Från", lockStart), wrap("Till", lockEnd),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn", onclick: () => guard(lockPeriod) }, "Lås period")),
    ));

    // Year-end accruals
    panel.appendChild(el("h3", { style: "margin-top:26px" }, "Årsbokslut — periodisera obetalda fakturor"));
    panel.appendChild(el("p", { class: "muted" },
      "Även med kontantmetod måste obetalda fakturor bokföras vid bokslut. Detta "
      + "periodiserar dem på årets sista dag och återför automatiskt på nyårsdagen "
      + "(vändning) så inget dubbelräknas när betalningen sedan kommer."));
    const fyEnd = el("input", { type: "date", value: "2026-12-31" });
    panel.appendChild(el("div", { class: "row" },
      wrap("Räkenskapsårets sista dag", fyEnd),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn brand", onclick: () => guard(yearEnd) }, "Bokför periodiseringar")),
    ));

    async function lockPeriod() {
      await api("POST", `/books/${bid()}/period-locks`, {
        period_start: lockStart.value, period_end: lockEnd.value, kind: "moms",
      });
      toast(`Period ${lockStart.value} – ${lockEnd.value} låst`);
    }
    async function yearEnd() {
      const res = await api("POST", `/books/${bid()}/year-end-accruals`, { fiscal_year_end: fyEnd.value });
      toast(`${res.count} faktura(or) periodiserade`);
      state.section = "verifikat";
      renderWorkspace();
    }
  },

  // ----- settings: security (passphrase / recovery key) + backup -----
  async settings(panel) {
    panel.appendChild(el("h2", {}, "Inställningar"));

    // --- Company / seller profile (for invoices) ---
    const [company, methods, acct] = await Promise.all([
      api("GET", `/books/${bid()}/company`),
      api("GET", `/books/${bid()}/payment-methods`),
      api("GET", `/books/${bid()}/accounting-method`),
    ]);

    // --- Bookkeeping method ---
    panel.appendChild(el("h3", {}, "Bokföringsmetod"));
    panel.appendChild(el("p", { class: "muted" },
      "Kontantmetod: fakturan bokförs när den betalas (+ periodisering vid bokslut). "
      + "Fakturametoden: fakturan bokförs direkt (kundfordran + moms) och betalningen "
      + "bokförs separat. Påverkar endast nya fakturor."));
    const acctSel = el("select", {},
      el("option", { value: "kontantmetod" }, "Kontantmetod"),
      el("option", { value: "fakturametod" }, "Fakturametoden"));
    acctSel.value = acct.method;
    acctSel.addEventListener("change", () => guard(async () => {
      await api("PUT", `/books/${bid()}/accounting-method`, { method: acctSel.value });
      toast("Bokföringsmetod: " + (acctSel.value === "fakturametod" ? "Fakturametoden" : "Kontantmetod"));
    }));
    panel.appendChild(el("div", { class: "row", style: "margin-bottom:22px" },
      wrap("Metod", acctSel)));
    panel.appendChild(el("h3", {}, "Företagsuppgifter (säljare)"));
    panel.appendChild(el("p", { class: "muted" }, "Visas på fakturor."));
    const cName = el("input", { type: "text", value: company.name || "" });
    const cOrg = el("input", { type: "text", value: company.org_nr || "" });
    const cVat = el("input", { type: "text", value: company.vat_nr || "" });
    const cAddr = el("input", { type: "text", value: company.address || "" });
    const cEmail = el("input", { type: "text", value: company.email || "" });
    const cPhone = el("input", { type: "text", value: company.phone || "" });
    const cFskatt = el("select", {}, el("option", { value: "1" }, "Ja"), el("option", { value: "0" }, "Nej"));
    cFskatt.value = company.f_skatt ? "1" : "0";
    panel.appendChild(el("div", { class: "row" },
      wrap("Företagsnamn", cName), wrap("Org.nr", cOrg), wrap("Momsreg.nr", cVat)));
    panel.appendChild(el("div", { class: "row" },
      wrap("Adress", cAddr), wrap("E-post", cEmail), wrap("Telefon", cPhone),
      wrap("Godkänd för F-skatt", cFskatt)));
    panel.appendChild(el("div", { style: "margin:6px 0 22px" },
      el("button", { class: "btn", onclick: () => guard(saveCompany) }, "Spara företagsuppgifter")));

    // --- Logo (used on every document) ---
    panel.appendChild(el("h3", {}, "Logotyp"));
    panel.appendChild(el("p", { class: "muted" },
      "Visas på alla dokument (fakturor m.m.). PNG, JPG, WEBP — kan bytas när som helst."));
    const logoImg = el("img", { class: "receipt-thumb", style: "max-height:70px;display:none" });
    const logoFile = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif,image/*" });
    const removeLogo = el("button", { class: "btn small ghost", onclick: () => guard(async () => {
      await api("DELETE", `/books/${bid()}/logo`); toast("Logotyp borttagen"); showLogo(false);
    }) }, "Ta bort logotyp");
    logoFile.addEventListener("change", () => guard(async () => {
      const f = logoFile.files && logoFile.files[0];
      if (!f) return;
      await api("PUT", `/books/${bid()}/logo`, { image_base64: await blobToBase64(f) });
      toast("Logotyp sparad"); showLogo(true);
    }));
    panel.appendChild(el("div", { class: "row", style: "align-items:center" },
      logoImg, logoFile, removeLogo));
    panel.appendChild(el("div", { style: "margin-bottom:22px" }));
    showLogo(company.has_logo);

    async function showLogo(has) {
      removeLogo.style.display = has ? "" : "none";
      if (!has) { logoImg.style.display = "none"; logoImg.removeAttribute("src"); return; }
      if (window.__BOKYUP_NATIVE__) {
        const r = await api("GET", `/books/${bid()}/logo`);
        logoImg.src = `data:${r.media_type};base64,${r.base64}`;
      } else {
        logoImg.src = `/books/${bid()}/logo?t=${Date.now()}`;
      }
      logoImg.style.display = "block";
    }

    // --- Payment methods ---
    panel.appendChild(el("h3", {}, "Betalsätt"));
    panel.appendChild(el("p", { class: "muted" }, "T.ex. Swish, Bankgiro, IBAN — namn + nummer/länk. "
      + "Redigering påverkar inte redan skapade fakturor (de har egna kopior)."));
    const pmActions = (m) => el("span", { style: "display:inline-flex;gap:4px" },
      editBtn(() => guard(() => editPaymentMethod(m))),
      el("button", { class: "btn small ghost", onclick: () => guard(() => togglePaymentMethod(m)) },
        m.active ? "Inaktivera" : "Aktivera"),
      el("button", { class: "btn small ghost danger", onclick: () => guard(() => deletePaymentMethod(m)) }, "Ta bort"));
    panel.appendChild(simpleTable(["Betalsätt", "Nummer/länk", "Aktiv", ""],
      methods.map((m) => [m.label, m.value,
        el("span", { class: "pill " + (m.active ? "paid" : "") }, m.active ? "Ja" : "Nej"),
        pmActions(m)])));
    panel.appendChild(el("div", { style: "margin:6px 0 22px" },
      el("button", { class: "btn small", onclick: () => guard(addPaymentMethod) }, "+ Nytt betalsätt")));

    async function saveCompany() {
      await api("PUT", `/books/${bid()}/company`, {
        name: cName.value || null, org_nr: cOrg.value || null, vat_nr: cVat.value || null,
        address: cAddr.value || null, email: cEmail.value || null, phone: cPhone.value || null,
        f_skatt: parseInt(cFskatt.value, 10),
      });
      toast("Företagsuppgifter sparade");
    }
    async function addPaymentMethod() {
      const f = await modal("Nytt betalsätt", [
        { name: "label", label: "Namn (t.ex. Swish)" },
        { name: "value", label: "Nummer/länk" },
      ], "Spara");
      if (!f || !f.label || !f.value) return;
      await api("POST", `/books/${bid()}/payment-methods`, { label: f.label, value: f.value });
      toast("Betalsätt sparat");
      renderWorkspace();
    }
    async function editPaymentMethod(m) {
      const f = await modal("Ändra betalsätt", [
        { name: "label", label: "Namn (t.ex. Swish, Bankgiro)", value: m.label },
        { name: "value", label: "Nummer/länk", value: m.value },
      ], "Spara");
      if (!f || !f.label || !f.value) return;
      await api("PATCH", `/books/${bid()}/payment-methods/${m.id}`, { label: f.label, value: f.value });
      toast("Betalsätt uppdaterat");
      renderWorkspace();
    }
    async function togglePaymentMethod(m) {
      await api("PATCH", `/books/${bid()}/payment-methods/${m.id}`, { active: m.active ? 0 : 1 });
      toast(m.active ? "Betalsätt inaktiverat" : "Betalsätt aktiverat");
      renderWorkspace();
    }
    async function deletePaymentMethod(m) {
      const f = await modal(`Ta bort betalsättet "${m.label}"?`, [], "Ta bort");
      if (!f) return;
      await api("DELETE", `/books/${bid()}/payment-methods/${m.id}`);
      toast("Betalsätt borttaget");
      renderWorkspace();
    }

    // --- Change passphrase ---
    panel.appendChild(el("h3", {}, "Byt lösenord"));
    panel.appendChild(el("p", { class: "muted" },
      "Byter lösenord utan att kryptera om data (samma nyckel pakas om). En "
      + "återställningsnyckel påverkas inte."));
    const oldP = el("input", { type: "password" });
    const newP = el("input", { type: "password" });
    const newP2 = el("input", { type: "password" });
    panel.appendChild(el("div", { class: "row" },
      wrap("Nuvarande lösenord", oldP), wrap("Nytt lösenord", newP), wrap("Bekräfta nytt", newP2)));
    panel.appendChild(el("div", { style: "margin:6px 0 22px" },
      el("button", { class: "btn", onclick: () => guard(changePass) }, "Byt lösenord")));

    // --- Recovery key ---
    panel.appendChild(el("h3", {}, "Återställningsnyckel"));
    panel.appendChild(el("p", { class: "muted" },
      "En offline-nyckel som kan låsa upp boken om lösenordet glöms bort — viktigt "
      + "för ett 7-årigt arkiv. Skriv ut den och förvara säkert. Den visas bara nu."));
    const rkStatus = el("div", { class: "muted" }, "Kontrollerar…");
    const rkBox = el("div", {});
    panel.appendChild(rkStatus);
    panel.appendChild(el("div", { style: "margin:6px 0 22px" },
      el("button", { class: "btn", onclick: () => guard(genRecovery) }, "Skapa/ersätt återställningsnyckel")));
    panel.appendChild(rkBox);
    api("GET", `/books/${bid()}/recovery-key`).then((s) => {
      rkStatus.textContent = s.has_recovery_key
        ? "✓ En återställningsnyckel finns redan."
        : "Ingen återställningsnyckel ännu.";
    });

    // --- Backup / restore (.buyn) ---
    panel.appendChild(el("h3", {}, "Säkerhetskopia (.buyn)"));
    panel.appendChild(el("p", { class: "muted" },
      "Exporterar hela boken (krypterad DB + kvitton) till en .buyn-fil. Samma fil "
      + "importeras på en annan enhet (PC eller telefon) med samma lösenord — välj "
      + "“Återställ säkerhetskopia” på startsidan."));
    panel.appendChild(el("div", { style: "margin:6px 0" },
      el("button", { class: "btn", onclick: () => guard(() =>
        exportBackupFlow(bid(), state.activeBook.display_name)) }, "Exportera säkerhetskopia")));

    async function changePass() {
      if (!oldP.value || !newP.value) { toast("Fyll i lösenorden", true); return; }
      if (newP.value !== newP2.value) { toast("Nya lösenorden matchar inte", true); return; }
      await api("POST", `/books/${bid()}/change-passphrase`,
                { old_passphrase: oldP.value, new_passphrase: newP.value });
      oldP.value = newP.value = newP2.value = "";
      toast("Lösenord bytt");
    }
    async function genRecovery() {
      const f = await modal("Bekräfta lösenord", [
        { name: "passphrase", label: "Nuvarande lösenord", type: "password" },
      ], "Skapa nyckel");
      if (!f || !f.passphrase) return;
      const res = await api("POST", `/books/${bid()}/recovery-key`, { passphrase: f.passphrase });
      rkBox.innerHTML = "";
      rkBox.appendChild(el("div", { class: "box" },
        el("div", { class: "k" }, "Återställningsnyckel — spara nu, visas inte igen"),
        el("div", { class: "v", style: "font-family:monospace;user-select:all" }, res.recovery_key)));
      rkStatus.textContent = "✓ En återställningsnyckel finns redan.";
      toast("Återställningsnyckel skapad — spara den säkert");
    }
  },

  // ----- invoices (faktura): list + create with line items & RUT recipients -----
  async invoices(panel) {
    // Arriving from the "Ny faktura" button in the Kunder tab: open the form straight
    // away with that customer preselected.
    if (state.pendingInvoiceCustomer != null) {
      const kid = state.pendingInvoiceCustomer;
      state.pendingInvoiceCustomer = null;
      await invoiceForm(panel, { payload: { customer_id: kid } });
      return;
    }
    const [list, drafts, customers, offerter] = await Promise.all([
      api("GET", `/books/${bid()}/invoices`),
      api("GET", `/books/${bid()}/invoice-drafts`),
      api("GET", `/books/${bid()}/customers`),
      api("GET", `/books/${bid()}/offerter`),
    ]);
    const custName = Object.fromEntries(customers.map((c) =>
      [c.kundnummer, c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim()]));
    panel.appendChild(headerWithAdd("Ordrar", "+ Ny faktura", () => guard(() => invoiceForm(panel))));

    // Sub-tabs (Fakturor / Offerter / Utkast) + a per-customer filter. Default: all,
    // sequential order. State persists on `state` so an action that re-renders keeps the
    // active sub-tab and filter.
    if (!state.ordersTab) state.ordersTab = "fakturor";
    if (state.ordersCustomer == null) state.ordersCustomer = "";
    const cfilter = (arr) => {
      const cid = state.ordersCustomer ? parseInt(state.ordersCustomer, 10) : null;
      return cid ? arr.filter((x) => x.customer_id === cid) : arr;
    };
    const subTabs = [["fakturor", "Fakturor", list.length],
                     ["offerter", "Offerter", offerter.length],
                     ["utkast", "Utkast", drafts.length]];
    const subnav = el("div", { class: "nav", style: "margin-top:12px" });
    for (const [key, label, count] of subTabs) {
      subnav.appendChild(el("button", { class: state.ordersTab === key ? "active" : "",
        onclick: () => { state.ordersTab = key; renderContent(); } }, `${label} (${count})`));
    }
    const custFilter = el("select", { style: "max-width:280px" },
      el("option", { value: "" }, "— Alla kunder —"),
      ...customers.map((c) => el("option", { value: c.kundnummer },
        c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer))));
    custFilter.value = state.ordersCustomer;
    custFilter.onchange = () => { state.ordersCustomer = custFilter.value; renderContent(); };
    panel.appendChild(subnav);
    panel.appendChild(el("div", { class: "row", style: "margin-top:8px" }, wrap("Filtrera på kund", custFilter)));
    const content = el("div", {});
    panel.appendChild(content);

    // ----- Utkast -----
    function drawUtkast() {
      const rows = cfilter(drafts);
      if (rows.length === 0) {
        content.appendChild(el("p", { class: "muted", style: "margin-top:14px" }, "Inga utkast.")); return;
      }
      content.appendChild(simpleTable(
        ["Sparat", "Kund", "Rader", "Summa", ""],
        rows.map((d) => [d.updated_at ? d.updated_at.slice(0, 16).replace("T", " ") : "",
          custName[d.customer_id] || (d.customer_id ? "Kund " + d.customer_id : "—"),
          d.line_count, toKr(d.total_ore || 0) + " kr",
          el("span", { style: "display:inline-flex;gap:4px" },
            el("button", { class: "btn small", onclick: () => guard(async () => {
              const full = await api("GET", `/books/${bid()}/invoice-drafts/${d.id}`);
              invoiceForm(panel, full);
            }) }, "Fortsätt"),
            el("button", { class: "btn small ghost", title: "Skapa en offert från utkastet (utkastet behålls)",
              onclick: () => guard(async () => {
                const o = await api("POST", `/books/${bid()}/offerter`, { draft_id: d.id });
                toast(`Offert ${o.offert_number} skapad`);
                showPdf(`/books/${bid()}/offerter/${o.offert_id}/pdf`, `Offert ${o.offert_number}`);
                renderWorkspace();
              }) }, "Skapa offert"),
            el("button", { class: "btn small ghost danger", onclick: () => guard(async () => {
              await api("DELETE", `/books/${bid()}/invoice-drafts/${d.id}`);
              toast("Utkast borttaget"); renderWorkspace();
            }) }, "Ta bort"))]),
      ));
    }

    // ----- Offerter -----
    function drawOfferter() {
      const rows = cfilter(offerter);
      if (rows.length === 0) {
        content.appendChild(el("p", { class: "muted", style: "margin-top:14px" }, "Inga offerter.")); return;
      }
      content.appendChild(simpleTable(
        ["Offertnr", "Kund", "Datum", "Giltig till", "Summa", "Status", ""],
        rows.map((o) => [String(o.offert_number), custName[o.customer_id] || ("Kund " + o.customer_id),
          o.offert_date, o.valid_until || "—", toKr(o.inc_moms_ore || 0) + " kr",
          o.invoice_id
            ? el("span", { class: "pill paid" }, "Faktura " + (o.invoice_number || o.invoice_id))
            : el("span", { class: "pill" }, "Offert"),
          el("span", { style: "display:inline-flex;gap:4px" },
            el("button", { class: "btn small ghost",
              onclick: () => guard(() => showPdf(`/books/${bid()}/offerter/${o.id}/pdf`, `Offert ${o.offert_number}`)) }, "PDF"),
            o.invoice_id ? null : el("button", { class: "btn small",
              title: "Skapa en riktig faktura utifrån offerten",
              onclick: () => guard(() => offertToInvoiceFlow(o)) }, "Skapa faktura"))]),
      ));
    }

    function renderContent() {
      content.innerHTML = "";
      [...subnav.children].forEach((btn, i) => {
        btn.className = state.ordersTab === subTabs[i][0] ? "active" : "";
      });
      if (state.ordersTab === "offerter") return drawOfferter();
      if (state.ordersTab === "utkast") return drawUtkast();
      return drawFakturor();
    }

    // ----- Fakturor -----
    const STATE = {
      paid: ["paid", "Betald"], pending: ["pending", "Obetald"],
      partial: ["pending", "Delbetald"],
      awaiting_rut: ["awaiting", "Inväntar RUT/ROT"],
      cancelled: ["", "Makulerad"], credited: ["", "Krediterad"],
    };
    const act = (label, cls, fn) => el("button",
      { class: "btn small " + cls, style: "margin-left:4px", onclick: () => guard(fn) }, label);
    const rowFor = (iv) => {
      let [cls, label] = STATE[iv.state] || STATE.pending;
      // RUT and ROT invoices both carry a husavdrag receivable -> full Skatteverket flow.
      const isRut = (iv.rut_total_ore > 0) || (iv.rot_total_ore > 0);
      // Customer paid, but Skatteverket hasn't paid the husavdrag yet -> "Inväntar RUT/ROT".
      if (iv.state === "awaiting_rut") {
        const hasRut = iv.rut_total_ore > 0, hasRot = iv.rot_total_ore > 0;
        label = "Inväntar " + (hasRut && hasRot ? "RUT/ROT" : hasRot ? "ROT" : "RUT");
      }
      const owed = iv.outstanding_ore < 0 ? -iv.outstanding_ore : 0;   // we owe the customer
      const actions = el("td", { class: "num" },
        el("button", { class: "btn small ghost", onclick: () => guard(() => invoicePdf(iv.id, iv.invoice_number)) }, "PDF"));
      for (const cn of iv.credit_notes || []) {
        actions.appendChild(el("button", { class: "btn small ghost", style: "margin-left:4px",
          title: `Kreditfaktura nr ${cn.credit_note_number}`,
          onclick: () => guard(() => creditNotePdf(iv.id, cn.id, cn.credit_note_number)) },
          `Kreditnota ${cn.credit_note_number}`));
      }
      if (iv.husavdrag_shortfall_ore > 0) {
        // Husavdrag follow-up: the customer owes the part Skatteverket didn't pay.
        // Settles 1510 directly (no moms) — only a plain payment applies.
        actions.appendChild(el("span", { class: "pill", style: "margin-left:4px",
          title: iv.relation_note || "" }, "Husavdrag (uppföljning)"));
        if (iv.state === "pending" || iv.state === "partial") {
          actions.appendChild(act("Betala", "", () => payInvoiceFlow(iv)));
        }
      } else if (isRut) {
        // RUT/ROT invoices: first book the customer payment, then (once that's done)
        // book the Skatteverket husavdrag payout — the button stays here until it lands.
        if (iv.state === "pending") {
          actions.appendChild(act("Bokför betalning", "", () => payFlow(iv.transaktion_id)));
          actions.appendChild(act("Makulera", "ghost danger", () => makuleraInvoiceFlow(iv)));
        } else if (iv.rut_claim_state === "customer_paid" && iv.rut_claim_id) {
          actions.appendChild(act("Bokför husavdrag (Skatteverket)", "",
            () => rutSkvPayFlow(iv.rut_claim_id, iv.rut_total_ore + iv.rot_total_ore,
              `Avser delbetalning av faktura ${iv.invoice_number} — RUT/ROT-avdrag ej fullt utnyttjat av Skatteverket.`)));
        } else if (iv.rut_claim_state === "skatteverket_paid") {
          actions.appendChild(el("span", { class: "pill paid", style: "margin-left:4px" },
            "Husavdrag betalt"));
        }
      } else {
        if (iv.state === "pending" || iv.state === "partial") {
          actions.appendChild(act("Betala", "", () => payInvoiceFlow(iv)));
          actions.appendChild(act("Kreditera", "ghost", () => kreditInvoiceFlow(iv)));
        }
        if (iv.state === "paid") {
          actions.appendChild(act("Kreditera", "ghost", () => kreditInvoiceFlow(iv)));
        }
        if (iv.paid_ore > 0 || owed) {
          actions.appendChild(act(owed ? "Återbetala" : "Återbetala", "ghost", () => refundInvoiceFlow(iv)));
        }
        if (iv.state === "pending" && iv.paid_ore === 0 && iv.credited_ore === 0) {
          actions.appendChild(act("Makulera", "ghost danger", () => makuleraInvoiceFlow(iv)));
        }
      }
      const kvar = owed ? ("−" + toKr(owed) + " kr") : (toKr(iv.outstanding_ore) + " kr");
      // Real margin from picked lager-batches (revenue ex moms − inköpskostnad). Only
      // shown when at least one line carried a cost; otherwise unknown ("—").
      const marginCell = iv.has_cost
        ? el("td", { class: "num", title: `Inköpskostnad ${toKr(iv.cost_ore)} kr` },
            toKr(iv.margin_ore) + " kr")
        : el("td", { class: "num muted", title: "Ingen lagerbatch vald" }, "—");
      return el("tr", {},
        el("td", { class: "num" }, String(iv.invoice_number)),
        el("td", {}, custName[iv.customer_id] || ("Kund " + iv.customer_id)),
        el("td", {}, iv.invoice_date),
        el("td", {}, iv.due_date),
        el("td", { class: "num" }, toKr(iv.inc_moms_ore) + " kr"),
        marginCell,
        el("td", { class: "num", title: owed ? "Att återbetala till kund" : "Kvar att betala" }, kvar),
        el("td", {}, el("span", { class: "pill " + cls }, label),
          owed ? el("span", { class: "pill", style: "margin-left:4px" }, "återbet.") : null),
        actions);
    };
    function drawFakturor() {
      const rows = cfilter(list);
      if (rows.length === 0) {
        content.appendChild(el("p", { class: "muted", style: "margin-top:14px" },
          state.ordersCustomer ? "Inga fakturor för vald kund."
            : "Inga fakturor ännu. Ställ in företagsuppgifter och betalsätt under Inställningar."));
        return;
      }
      const ivMatch = (iv, q) => [String(iv.invoice_number), custName[iv.customer_id] || "",
        iv.invoice_date || "", iv.due_date || ""].join(" ").toLowerCase().includes(q);
      const search = el("input", { type: "search", placeholder: "Sök faktura (nr, kund, datum)…",
        style: "margin-top:12px;max-width:340px" });
      const tbody = el("tbody", {});
      const drawRows = () => {
        const q = search.value.trim().toLowerCase();
        const shown = q ? rows.filter((iv) => ivMatch(iv, q)) : rows;
        tbody.innerHTML = "";
        if (q && shown.length === 0) {
          tbody.appendChild(el("tr", {}, el("td", { colspan: "9", class: "muted" }, "Inga träffar.")));
        } else {
          for (const iv of shown) tbody.appendChild(rowFor(iv));
        }
      };
      search.oninput = drawRows;
      drawRows();
      content.appendChild(search);
      content.appendChild(el("table", { style: "margin-top:14px" },
        el("thead", {}, el("tr", {},
          el("th", { class: "num" }, "Nr"), el("th", {}, "Kund"), el("th", {}, "Datum"),
          el("th", {}, "Förfaller"), el("th", { class: "num" }, "Summa"),
          el("th", { class: "num" }, "Marginal"),
          el("th", { class: "num" }, "Kvar"), el("th", {}, "Status"), el("th", {}, ""))),
        tbody));
    }
    renderContent();
  },
};

// ---------------------------------------------------------------------------
// Moms-lines editor — one row per momssats (a receipt can mix 6/12/25 %)
// ---------------------------------------------------------------------------
const RATE_OPTIONS = ["25", "12", "6", "0", "momsfri", "ej_avdragsgill"];
const RATE_PCT = { "25": 0.25, "12": 0.12, "6": 0.06 };   // 0/momsfri/ej_avdragsgill -> 0
function rateLabel(r) {
  if (!r) return "—";
  return (r === "momsfri" || r === "ej_avdragsgill") ? r : r + "%";
}

// RUT/ROT pots (in ören) from the article lines: skattereduktion = labour cost INCL
// moms × the reduction percentage. Whole eligible line counts as labour.
function potsFromLines(lines, rutPct, rotPct) {
  let rut = 0, rot = 0;
  for (const ln of lines) {
    if (!ln.reduction_type) continue;
    const gross = Math.round((ln.quantity_centi || 0) * (ln.unit_price_ore || 0) / 100);
    const ex = gross - Math.round(gross * (ln.discount_pct_centi || 0) / 10000);
    const inc = ex + Math.round(ex * (RATE_PCT[ln.rate_code] || 0));
    const red = Math.round(inc * (ln.reduction_type === "rut" ? rutPct : rotPct) / 100);
    if (ln.reduction_type === "rut") rut += red; else rot += red;
  }
  return { rut, rot };
}

// Live invoice totals (ören) from the article lines — a preview while building. The final
// invoice also applies öresavrundning on the customer part; here we show whole-krona
// "Att betala (ca)" to match closely.
function invoiceTotals(lines, rutPct, rotPct) {
  let ex = 0, moms = 0, rabatt = 0;
  for (const ln of lines) {
    const gross = Math.round((ln.quantity_centi || 0) * (ln.unit_price_ore || 0) / 100);
    const disc = Math.round(gross * (ln.discount_pct_centi || 0) / 10000);
    const lineEx = gross - disc;
    ex += lineEx; rabatt += disc;
    moms += Math.round(lineEx * (RATE_PCT[ln.rate_code] || 0));
  }
  const inc = ex + moms;
  const { rut, rot } = potsFromLines(lines, rutPct, rotPct);
  const husavdrag = rut + rot;
  const attExakt = inc - husavdrag;                         // customer part before öresavrundning
  const attBetala = Math.round(attExakt / 100) * 100;       // avrundningslagen (helt krontal)
  return { ex, moms, inc, rabatt, rut, rot, husavdrag, attBetala };
}
function momsLinesEditor() {
  const rowsBox = el("div", {});
  const element = el("div", {});

  function addRow(value = "0,00", rate = "25") {
    const amount = el("input", { type: "text", value });
    const rateSel = el("select", {},
      ...RATE_OPTIONS.map((r) => el("option", { value: r },
        r === "momsfri" || r === "ej_avdragsgill" ? r : r + "%")));
    rateSel.value = rate;
    const remove = el("button", { class: "btn small ghost", type: "button",
      onclick: () => { if (rowsBox.children.length > 1) row.remove(); } }, "✕");
    const row = el("div", { class: "row line-row" },
      wrap("Belopp (kr, inkl. moms)", amount), wrap("Moms", rateSel),
      el("div", { style: "flex:0 0 auto;align-self:flex-end" }, remove));
    row._read = () => ({ rate_code: rateSel.value, amount_ore: toOre(amount.value), inclusive: true });
    rowsBox.appendChild(row);
  }
  addRow();

  element.appendChild(rowsBox);
  element.appendChild(el("button", { class: "btn small ghost", type: "button",
    onclick: () => addRow() }, "+ Lägg till rad"));

  return {
    element,
    getLines() {
      return Array.from(rowsBox.children)
        .map((r) => r._read())
        .filter((l) => l.amount_ore > 0);
    },
  };
}

// ---------------------------------------------------------------------------
// Receipt picker — import a file or take a photo; stages one image for upload
// ---------------------------------------------------------------------------
function receiptPicker() {
  let staged = null;           // { image_base64, mime }
  const preview = el("div", { class: "receipt-preview" });
  const fmt = el("select", {},
    el("option", { value: "paper" }, "Papperskvitto (foto = originalet)"),
    el("option", { value: "digital" }, "Digitalt kvitto (foto ersätter EJ originalet)"));

  function showPreview() {
    preview.innerHTML = "";
    if (!staged) return;
    preview.appendChild(el("img", { src: `data:${staged.mime};base64,${staged.image_base64}`, class: "receipt-thumb" }));
    preview.appendChild(el("button", { class: "btn small ghost", type: "button",
      onclick: () => { staged = null; showPreview(); } }, "Ta bort"));
  }

  const file = el("input", { type: "file", accept: "image/*", capture: "environment" });
  file.addEventListener("change", () => guard(async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    staged = { image_base64: await blobToBase64(f), mime: f.type || "image/jpeg" };
    file.value = "";
    showPreview();
  }));

  const controls = el("div", { class: "row" },
    el("div", { style: "flex:0 0 auto" }, file));
  if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    controls.appendChild(el("div", { style: "flex:0 0 auto;align-self:flex-end" },
      el("button", { class: "btn small", type: "button", onclick: () => guard(takePhoto) }, "📷 Ta foto")));
  }

  async function takePhoto() {
    const blob = await cameraCaptureModal();
    if (!blob) return;
    staged = { image_base64: await blobToBase64(blob), mime: blob.type || "image/jpeg" };
    showPreview();
  }

  const element = el("div", {}, controls, wrap("Kvittots originalformat", fmt), preview);
  return {
    element,
    getStaged() { return staged ? { ...staged, original_format: fmt.value } : null; },
  };
}

// Read a Blob/File into base64 (without the data: prefix).
function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

// A full-screen overlay (used by the camera and the receipt viewer).
function overlay(...children) {
  const box = el("div", { class: "overlay-box" }, ...children);
  const back = el("div", { class: "overlay" }, box);
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  return { close, box };
}

// Inline "create category" — a stacking overlay dialog so it works even on top of the
// shared modal (e.g. the Ny artikel form). Categories created here are income/product
// categories (an article's sell-side category, which also gives its number prefix).
// Resolves to the new category object {id,name,kind,prefix,default_rate_code,bas_konto}
// or null if cancelled.
const NEW_CAT = "__newcat__";
function catLabel(c) { return `${c.prefix || "?"} · ${c.name}`; }

function newCategoryDialog() {
  return new Promise((resolve) => {
    const name = el("input", { type: "text", placeholder: "Namn (t.ex. Nätverk)" });
    const bas = el("input", { type: "text", placeholder: "BAS-konto (t.ex. 3001)" });
    const prefix = el("input", { type: "text", placeholder: "4 siffror" });
    const rate = el("select", {}, ...RATE_OPTIONS.map((r) => el("option", { value: r }, rateLabel(r))));
    rate.value = "25";
    // Suggest the lowest unused prefix (editable; a taken one is rejected on save).
    api("GET", `/books/${bid()}/categories/next-prefix`)
      .then((r) => { if (!prefix.value) prefix.value = r.prefix; }).catch(() => {});
    let done = false;
    const finish = (v) => { if (done) return; done = true; ui.close(); resolve(v); };
    const ok = el("button", { class: "btn brand", onclick: () => guard(async () => {
      if (!name.value.trim() || !bas.value.trim()) { toast("Fyll i namn och BAS-konto", true); return; }
      const res = await api("POST", `/books/${bid()}/categories`, {
        name: name.value.trim(), kind: "income", bas_konto: parseInt(bas.value, 10),
        prefix: (prefix.value || "").trim() || null, default_rate_code: rate.value });
      const cat = { id: res.id, name: name.value.trim(), kind: "income", prefix: res.prefix,
        default_rate_code: rate.value, bas_konto: parseInt(bas.value, 10) };
      toast(`Kategori ${cat.name} skapad (prefix ${cat.prefix})`);
      finish(cat);
    }) }, "Skapa");
    const cancel = el("button", { class: "btn ghost", onclick: () => finish(null) }, "Avbryt");
    const ui = overlay(el("div", { class: "panel", style: "min-width:280px;max-width:420px" },
      el("h3", { style: "margin-top:0" }, "Ny kategori (inkomst/produkt)"),
      wrap("Namn", name), wrap("BAS-konto", bas), wrap("Artikelnr-prefix", prefix),
      wrap("Standardmoms", rate),
      el("div", { class: "modal-actions", style: "margin-top:12px" }, cancel, ok)));
    name.focus();
  });
}

// Wire a category <select> whose first entries include a "➕ Ny kategori…" option: when
// picked it opens newCategoryDialog, inserts + selects the created category, pushes it
// onto `cats`, and calls onCreated(cat). Cancelling reverts to the previous selection.
function wireCategoryCreateSelect(select, cats, onCreated) {
  let prev = select.value;
  select.addEventListener("change", () => {
    if (select.value !== NEW_CAT) { prev = select.value; return; }
    guard(async () => {
      const cat = await newCategoryDialog();
      if (cat) {
        cats.push(cat);
        const anchor = select.querySelector(`option[value="${NEW_CAT}"]`);
        select.insertBefore(el("option", { value: String(cat.id) }, cat.name),
          anchor ? anchor.nextSibling : null);
        select.value = String(cat.id);
        prev = select.value;
        if (onCreated) onCreated(cat);
      } else {
        select.value = prev;
      }
    });
  });
}

// Live-camera capture: returns a Promise<Blob|null>.
function cameraCaptureModal() {
  return new Promise((resolve) => {
    const video = el("video", { autoplay: "", playsinline: "", class: "cam-video" });
    let stream = null;
    let done = false;
    const finish = (blob) => { if (done) return; done = true; if (stream) stream.getTracks().forEach((t) => t.stop()); ui.close(); resolve(blob); };

    const snap = el("button", { class: "btn brand", onclick: () => {
      const c = el("canvas", {});
      c.width = video.videoWidth; c.height = video.videoHeight;
      c.getContext("2d").drawImage(video, 0, 0);
      c.toBlob((b) => finish(b), "image/jpeg", 0.92);
    } }, "Fånga");
    const cancel = el("button", { class: "btn ghost", onclick: () => finish(null) }, "Avbryt");

    const ui = overlay(video, el("div", { class: "modal-actions" }, cancel, snap));
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } })
      .then((s) => { stream = s; video.srcObject = s; })
      .catch((e) => { toast("Kunde inte öppna kameran: " + e.message, true); finish(null); });
  });
}

// ---------------------------------------------------------------------------
// Flows used by sections
// ---------------------------------------------------------------------------
async function receiptsFlow(txId, isPending) {
  const list = await api("GET", `/books/${bid()}/transaktioner/${txId}/receipts`);
  const body = el("div", {});
  if (list.length === 0) {
    body.appendChild(el("p", { class: "muted" }, "Inga kvitton för denna transaktion."));
  } else {
    for (const rc of list) {
      const img = el("img", { class: "receipt-view" });
      receiptSrc(rc.id).then((src) => { img.src = src; });
      const meta = el("div", { class: "muted" },
        `${rc.mime} · ${Math.round(rc.byte_size / 1024)} kB`
        + (rc.original_format ? " · " + rc.original_format : ""));
      const actions = el("div", { class: "modal-actions" });
      if (isPending) {
        actions.appendChild(el("button", { class: "btn small danger", onclick: () => guard(async () => {
          await api("DELETE", `/books/${bid()}/receipts/${rc.id}`);
          toast("Kvitto borttaget");
          ui.close();
          renderWorkspace();
        }) }, "Ta bort"));
      }
      body.appendChild(el("div", { class: "receipt-card" }, img, meta, actions));
    }
  }
  const ui = overlay(el("h3", {}, "Kvitton"), body,
    el("div", { class: "modal-actions" }, el("button", { class: "btn", onclick: () => ui.close() }, "Stäng")));
}

async function payFlow(txId) {
  const f = await modal("Bokför betalning", [
    { name: "payment_date", label: "Betaldatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Bokför");
  if (!f) return;
  await api("POST", `/books/${bid()}/transaktioner/${txId}/pay`, { payment_date: f.payment_date });
  toast("Betalning bokförd");
  renderWorkspace();
}

// Upload Skatteverket's kvittens (image or PDF) for a RUT/ROT payout, encrypted.
async function uploadKvittens(claimId, file) {
  if (!file) return;
  try {
    await api("POST", `/books/${bid()}/rut/${claimId}/receipt`,
      { image_base64: await blobToBase64(file), mime: file.type || "application/octet-stream" });
  } catch (e) {
    toast("Kvittensen kunde inte laddas upp: " + (e.message || e), true);
  }
}

// View (or add) the Skatteverket kvittens stored for a RUT/ROT payout.
async function rutKvittensFlow(claim) {
  const list = await api("GET", `/books/${bid()}/rut/${claim.id}/receipts`);
  async function addOne() {
    const f = await modal("Ladda upp kvittens från Skatteverket",
      [{ name: "kvittens", label: "Bild eller PDF", type: "file", accept: "image/*,application/pdf" }],
      "Ladda upp");
    if (!f || !f.kvittens) return;
    await uploadKvittens(claim.id, f.kvittens);
    toast("Kvittens sparad"); renderWorkspace();
  }
  if (list.length === 0) { await addOne(); return; }
  const body = el("div", {});
  for (const rc of list) {
    body.appendChild(el("div", { class: "modal-actions", style: "justify-content:space-between;align-items:center" },
      el("span", { class: "muted" }, `${rc.mime} · ${Math.round(rc.byte_size / 1024)} kB`),
      el("button", { class: "btn small", onclick: () =>
        showPdf(`/books/${bid()}/receipts/${rc.id}`, `Kvittens RUT ${claim.id}`) }, "Öppna")));
  }
  body.appendChild(el("div", { style: "margin-top:8px" },
    el("button", { class: "btn small ghost", onclick: () => guard(addOne) }, "+ Ladda upp fler")));
  const ov = overlay(el("h3", {}, "Kvittens från Skatteverket"), body,
    el("div", { class: "modal-actions" }, el("button", { class: "btn", onclick: () => ov.close() }, "Stäng")));
}

async function rutSkvPayFlow(claimId, claimedOre, defaultNote) {
  // Step 1: enter the date + the amount Skatteverket actually paid (defaults to the
  // claimed husavdrag), a reference (the RUT/ROT begäran name, e.g. "RUT1"), and the
  // kvittens from Skatteverket (stored encrypted).
  // Suggest the next RUT/ROT reference (own sequence, last + 1) — editable.
  const suggestedRef = await api("GET", `/books/${bid()}/rut-next-reference`)
    .then((r) => r.reference).catch(() => "");
  const f = await modal("Bokför Skatteverkets utbetalning", [
    { name: "payment_date", label: "Utbetalningsdatum", type: "date", value: new Date().toISOString().slice(0, 10) },
    { name: "received", label: "Mottaget belopp (kr)",
      value: claimedOre != null ? toKr(claimedOre) : "" },
    { name: "reference", label: "RUT/ROT-begäran (namn, t.ex. RUT1)", value: suggestedRef },
    { name: "kvittens", label: "Kvittens från Skatteverket (bild/PDF, valfri)",
      type: "file", accept: "image/*,application/pdf" },
  ], "Fortsätt");
  if (!f) return;
  const received_ore = f.received === "" ? null : toOre(f.received);
  const reference = f.reference && f.reference.trim() ? f.reference.trim() : null;

  // Step 2: ask the backend how it reads the amount (rounding vs partial vs overpaid).
  let interp = "rounding";
  if (received_ore != null) {
    const prev = await api("POST", `/books/${bid()}/rut/${claimId}/skatteverket-preview`,
      { received_ore });
    interp = prev.interpretation;
    if (interp === "overpaid") {
      toast(`Skatteverket betalade mer än begärt (${toKr(-prev.difference_ore)} kr över). `
        + "Kontrollera beloppet.", true);
      return;
    }
    if (interp === "partial") {
      // A quota/cap-driven shortfall: the remainder becomes a receivable on the
      // customer, documented as a linked follow-up invoice. Confirm + let the user
      // edit the reference text (blank = standard text referencing the original).
      const shortfall = prev.difference_ore;
      const c = await modal(
        `Skatteverket betalade ${toKr(prev.received_ore)} kr av begärda ${toKr(prev.claimed_ore)} kr `
        + `(${toKr(shortfall)} kr mindre). Detta ser ut som en delbetalning (utnyttjat tak/kvot). `
        + "En uppföljningsfaktura skapas till kunden på mellanskillnaden.",
        [{ name: "relation_note", label: "Text på uppföljningsfakturan (valfri)",
           value: defaultNote || "" }],
        "Skapa uppföljningsfaktura");
      if (!c) return;
      const res = await api("POST", `/books/${bid()}/rut/${claimId}/skatteverket-payment`,
        { payment_date: f.payment_date, received_ore, mode: "partial",
          relation_note: c.relation_note || null, reference });
      await uploadKvittens(claimId, f.kvittens);
      toast(`Husavdrag delbetalt. Uppföljningsfaktura skapad (${toKr(shortfall)} kr till kunden).`);
      renderWorkspace();
      if (res.shortfall_invoice_id) invoicePdf(res.shortfall_invoice_id);
      return;
    }
  }
  // exact or within öresavrundning -> book straight (diff, if any, lands on 3740).
  await api("POST", `/books/${bid()}/rut/${claimId}/skatteverket-payment`,
    { payment_date: f.payment_date, received_ore, reference });
  await uploadKvittens(claimId, f.kvittens);
  toast(interp === "rounding" && received_ore != null
    ? "Husavdrag bokfört (öresavrundning mot 3740)." : "Husavdrag bokfört.");
  renderWorkspace();
}

// Manual journal entry (manuell verifikation): a balanced, hand-entered verifikation
// independent of invoices — for fixing something manually. Takes over the section panel.
async function manualVerForm(panel, accounts) {
  const known = new Map((accounts || []).map((a) => [String(a.bas_konto), a.name]));
  panel.innerHTML = "";
  panel.appendChild(el("h2", {}, "Ny manuell verifikation"));
  panel.appendChild(el("p", { class: "muted" },
    "En manuell verifikation får nästa lediga verifikationsnummer, måste balansera "
    + "(debet = kredit) och kan inte ändras efteråt (rätta med en rättelse). Belopp i kronor."));

  const dList = el("datalist", { id: "manual-konto-list" },
    ...(accounts || []).map((a) => el("option", { value: String(a.bas_konto) }, `${a.bas_konto} ${a.name}`)));
  panel.appendChild(dList);

  const today = new Date().toISOString().slice(0, 10);
  const verDate = el("input", { type: "date", value: today });
  const text = el("input", { type: "text", placeholder: "T.ex. Omföring materialkostnad", style: "min-width:280px" });
  panel.appendChild(el("div", { class: "row" },
    wrap("Verifikationsdatum", verDate), wrap("Verifikationstext", text)));

  const rowsBox = el("div", {});
  const balance = el("div", { class: "muted", style: "margin:8px 0;font-weight:600" });
  function recompute() {
    let dsum = 0, ksum = 0;
    for (const r of rowsBox.children) {
      dsum += r._debit(); ksum += r._credit();
    }
    const diff = dsum - ksum;
    balance.textContent = `Debet ${toKr(dsum)} kr · Kredit ${toKr(ksum)} kr · `
      + (diff === 0 ? "balanserar ✓" : `differens ${toKr(diff)} kr`);
    balance.style.color = diff === 0 ? "" : "var(--danger)";
  }
  function addRow(v) {
    v = v || {};
    const konto = el("input", { type: "text", list: "manual-konto-list", placeholder: "konto",
      style: "width:90px", value: v.konto || "", oninput: recompute });
    const debit = el("input", { type: "text", placeholder: "0,00", style: "width:96px", oninput: recompute });
    const credit = el("input", { type: "text", placeholder: "0,00", style: "width:96px", oninput: recompute });
    const ptext = el("input", { type: "text", placeholder: "radtext (valfri)", style: "width:180px" });
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end" },
      wrap("Konto", konto), wrap("Debet", debit), wrap("Kredit", credit), wrap("Text", ptext),
      el("button", { class: "btn small ghost", onclick: (e) => { e.target.closest(".row").remove(); recompute(); } }, "✕"));
    row._konto = konto; row._ptext = ptext;
    row._debit = () => (debit.value.trim() ? toOre(debit.value) : 0);
    row._credit = () => (credit.value.trim() ? toOre(credit.value) : 0);
    rowsBox.appendChild(row);
  }
  addRow(); addRow();
  panel.appendChild(rowsBox);
  panel.appendChild(el("div", {},
    el("button", { class: "btn small ghost", onclick: () => { addRow(); recompute(); } }, "+ Rad")));
  panel.appendChild(balance);
  recompute();

  panel.appendChild(el("div", { style: "margin-top:14px" },
    el("button", { class: "btn brand", onclick: () => guard(submit) }, "Bokför verifikation"),
    el("button", { class: "btn ghost", style: "margin-left:8px",
      onclick: () => { state.section = "huvudbok"; renderWorkspace(); } }, "Avbryt")));

  async function submit() {
    if (!text.value.trim()) { toast("Ange en verifikationstext", true); return; }
    const postings = [];
    for (const r of rowsBox.children) {
      const k = r._konto.value.trim();
      const d = r._debit(), c = r._credit();
      if (!k && !d && !c) continue;                 // skip blank rows
      if (!k) { toast("Fyll i konto på alla rader med belopp", true); return; }
      if (d && c) { toast(`Konto ${k}: ange antingen debet eller kredit, inte båda`, true); return; }
      if (!d && !c) { toast(`Konto ${k}: ange ett belopp`, true); return; }
      const p = { bas_konto: parseInt(k, 10), debit_ore: d, credit_ore: c, text: r._ptext.value.trim() || null };
      if (!known.has(k)) {                          // new konto -> name it
        const nf = await modal(`Nytt konto ${k} — ange kontonamn`,
          [{ name: "name", label: "Kontonamn", value: "" }], "OK");
        if (nf && nf.name.trim()) p.account_name = nf.name.trim();
      }
      postings.push(p);
    }
    if (postings.length < 2) { toast("En verifikation behöver minst två rader", true); return; }
    const dsum = postings.reduce((s, p) => s + p.debit_ore, 0);
    const ksum = postings.reduce((s, p) => s + p.credit_ore, 0);
    if (dsum !== ksum) { toast(`Balanserar inte (differens ${toKr(dsum - ksum)} kr)`, true); return; }
    const res = await api("POST", `/books/${bid()}/verifikationer/manual`,
      { ver_date: verDate.value, text: text.value.trim(), postings });
    toast(`Verifikation ${res.ver_number} bokförd`);
    state.section = "huvudbok";
    renderWorkspace();
  }
}

async function reverseFlow(verId, verLabel) {
  const f = await modal(`Rätta verifikation ${verLabel}`, [
    { name: "reason", label: "Orsak till rättelse" },
    { name: "reg_date", label: "Bokföringsdatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Skapa rättelse");
  if (!f || !f.reason) { if (f) toast("Ange en orsak", true); return; }
  const res = await api("POST", `/books/${bid()}/verifikationer/${verId}/reverse`,
                        { reason: f.reason, reg_date: f.reg_date });
  toast(`Rättelse skapad (ver ${res.ver_number})`);
  renderWorkspace();
}

async function addCustomerFlow() {
  const f = await modal("Ny kund", [
    { name: "type", label: "Typ", type: "select", value: "private",
      options: [{ value: "private", label: "Privat" }, { value: "business", label: "Företag" }] },
    { name: "first_name", label: "Förnamn (privat)" },
    { name: "last_name", label: "Efternamn (privat)" },
    { name: "personnummer", label: "Personnummer (privat)" },
    { name: "company_name", label: "Företagsnamn (företag)" },
    { name: "org_nr", label: "Org.nr (företag)" },
    { name: "vat_nr", label: "Momsreg.nr (företag/EU)" },
    { name: "street", label: "Gatuadress" },
    { name: "zip_code", label: "Postnummer" },
    { name: "city", label: "Ort" },
    { name: "country", label: "Land", value: "Sverige" },
    { name: "shipping_address", label: "Leveransadress (om annan)" },
    { name: "email", label: "E-post" },
    { name: "phone", label: "Telefon" },
  ], "Spara");
  if (!f) return;
  const body = { type: f.type };
  for (const k of ["first_name", "last_name", "personnummer", "company_name", "org_nr",
                   "vat_nr", "street", "zip_code", "city", "country", "shipping_address",
                   "email", "phone"]) {
    if (f[k]) body[k] = f[k];
  }
  await api("POST", `/books/${bid()}/customers`, body);
  toast("Kund sparad");
  renderWorkspace();
}

async function addSupplierFlow() {
  const f = await modal("Ny leverantör", [
    { name: "name", label: "Namn" },
    { name: "default_moms_rate", label: "Standardmoms", type: "select", value: "25",
      options: ["25", "12", "6", "0", "momsfri", "ej_avdragsgill"].map((r) => ({ value: r, label: r })) },
    { name: "org_nr", label: "Org.nr (valfritt)" },
  ], "Spara");
  if (!f || !f.name) return;
  await api("POST", `/books/${bid()}/suppliers`, {
    name: f.name, default_moms_rate: f.default_moms_rate, org_nr: f.org_nr || null,
  });
  toast("Leverantör sparad");
  renderWorkspace();
}

function articleFields(a, incomeCats) {
  a = a || {};
  return [
    { name: "description", label: "Beskrivning", value: a.description || "" },
    { name: "unit_price_kr", label: "À-pris (kr, ex moms)",
      value: a.unit_price_ore != null ? toKr(a.unit_price_ore) : "0,00" },
    { name: "unit", label: "Enhet", value: a.unit || "st" },
    { name: "rate_code", label: "Moms", type: "select", value: a.rate_code || "25",
      options: RATE_OPTIONS.map((r) => ({ value: r, label: rateLabel(r) })) },
    { name: "reduction_type", label: "Husavdrag", type: "select", value: a.reduction_type || "",
      options: [{ value: "", label: "—" }, { value: "rut", label: "RUT" }, { value: "rot", label: "ROT" }] },
    { name: "category_id", label: "Kategori (BAS, valfri)", type: "select",
      value: a.category_id ? String(a.category_id) : "",
      options: [{ value: "", label: "Okategoriserad" },
        { value: NEW_CAT, label: "➕ Ny kategori…" },
        ...incomeCats.map((c) => ({ value: String(c.id), label: c.name }))],
      onChange: handleModalCatCreate },
  ];
}

// Category <select> inside a shared modal: open the stacking category dialog when the
// "➕ Ny kategori…" option is picked, then insert + select the created category.
function handleModalCatCreate(val, input) {
  if (val !== NEW_CAT) { input._prev = val; return; }
  guard(async () => {
    const cat = await newCategoryDialog();
    if (cat) {
      const anchor = input.querySelector(`option[value="${NEW_CAT}"]`);
      input.insertBefore(el("option", { value: String(cat.id) }, cat.name),
        anchor ? anchor.nextSibling : null);
      input.value = String(cat.id);
    } else {
      input.value = input._prev || "";
    }
    input._prev = input.value;
  });
}

async function addArticleFlow(incomeCats) {
  // No manual prefix — the article number's prefix comes from the chosen category (or
  // the provisional "NY-" bucket when uncategorised; assign a category later to reissue).
  const f = await modal("Ny artikel", articleFields(null, incomeCats), "Skapa");
  if (!f || !f.description) return;
  const res = await api("POST", `/books/${bid()}/articles`, {
    description: f.description,
    unit_price_ore: toOre(f.unit_price_kr), unit: f.unit || null,
    rate_code: f.rate_code, reduction_type: f.reduction_type || null,
    category_id: f.category_id ? parseInt(f.category_id, 10) : null,
  });
  toast(`Artikel ${res.article_number} skapad`);
  renderWorkspace();
}

async function editArticleFlow(a, incomeCats) {
  const f = await modal(`Ändra artikel ${a.article_number}`, [
    { name: "article_number", label: "Artikelnummer", value: a.article_number },
    ...articleFields(a, incomeCats),
  ], "Spara");
  if (!f || !f.description) return;
  const newCat = f.category_id ? parseInt(f.category_id, 10) : null;
  const body = {
    description: f.description,
    unit_price_ore: toOre(f.unit_price_kr), unit: f.unit || null,
    rate_code: f.rate_code, reduction_type: f.reduction_type || null,
    category_id: newCat,
  };
  // Only send an explicit article_number if the user actually changed it. Leaving it out
  // lets the backend re-issue the number to the new category's prefix (when the category
  // changed and the article has never been invoiced).
  if ((f.article_number || "") !== a.article_number) body.article_number = f.article_number || null;
  await api("PATCH", `/books/${bid()}/articles/${a.id}`, body);
  toast("Artikel uppdaterad");
  renderWorkspace();
}

async function addStockBatchFlow(articles, suppliers) {
  if (!articles || articles.length === 0) {
    toast("Skapa en artikel först (fliken Artiklar)", true);
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  const f = await modal("Lägg till i lager (inköp)", [
    { name: "article_id", label: "Artikel", type: "select",
      options: articles.map((a) => ({ value: String(a.id),
        label: `${a.article_number} — ${a.description}` })) },
    { name: "qty", label: "Antal (st)", value: "1" },
    { name: "unit_cost_kr", label: "Inköpspris per st (kr, ex moms)", value: "0,00" },
    { name: "supplier_id", label: "Leverantör (valfri)", type: "select", value: "",
      options: [{ value: "", label: "—" },
        ...(suppliers || []).map((s) => ({ value: String(s.id), label: s.name }))] },
    { name: "received_date", label: "Inköpsdatum", type: "date", value: today },
    { name: "note", label: "Notering (valfri)" },
  ], "Lägg till");
  if (!f) return;
  const qty = Math.round(parseFloat(String(f.qty).replace(",", ".")) * 100);
  if (!(qty > 0)) { toast("Antalet måste vara större än 0", true); return; }
  const res = await api("POST", `/books/${bid()}/stock`, {
    article_id: parseInt(f.article_id, 10), qty_centi: qty,
    unit_cost_ore: toOre(f.unit_cost_kr),
    supplier_id: f.supplier_id ? parseInt(f.supplier_id, 10) : null,
    received_date: f.received_date || null, note: f.note || null,
  });
  toast(`Batch ${res.batch_number} tillagd i lager`);
  renderWorkspace();
}

async function stockBatchesTable(articleId, suppliers) {
  const batches = await api("GET", `/books/${bid()}/articles/${articleId}/batches`);
  return simpleTable(
    ["Batch-ID", "Kvar", "Inköpt", "À-kostnad", "Datum", "Leverantör", "Notering", ""],
    batches.map((b) => [
      el("strong", { title: `Batch ${b.batch_number} av ${b.article_number}` },
        b.full_batch_id || (b.article_number + "-" + b.batch_number)),
      (b.qty_remaining_centi / 100).toLocaleString("sv-SE"),
      (b.qty_in_centi / 100).toLocaleString("sv-SE"),
      toKr(b.unit_cost_ore) + " kr",
      b.received_date || "—",
      b.supplier_name || el("span", { class: "muted" }, "—"),
      b.note || "",
      b.qty_remaining_centi === b.qty_in_centi
        ? el("button", { class: "btn small ghost danger", title: "Ta bort (oanvänd batch)",
            onclick: () => guard(async () => {
              const c = await modal(`Ta bort batch #${b.batch_number}?`, [], "Ta bort");
              if (!c) return;
              await api("DELETE", `/books/${bid()}/stock/${b.id}`);
              toast("Batch borttagen"); renderWorkspace();
            }) }, "Ta bort")
        : el("span", { class: "muted", title: "Delvis förbrukad – kan inte tas bort" }, "förbrukad"),
    ]),
  );
}

async function addCategoryFlow() {
  // Suggest the lowest unused 4-digit prefix; the user may type another (a taken one is
  // rejected by the backend with a clear message).
  let suggested = "";
  try { suggested = (await api("GET", `/books/${bid()}/categories/next-prefix`)).prefix; }
  catch (e) { /* offline / none — leave blank, backend auto-assigns */ }
  const f = await modal("Ny kategori", [
    { name: "name", label: "Namn (t.ex. Försäljning IT-tjänster)" },
    { name: "kind", label: "Typ", type: "select", value: "expense",
      options: [{ value: "income", label: "Inkomst" }, { value: "expense", label: "Utgift" }] },
    { name: "bas_konto", label: "BAS-konto (t.ex. 3001 eller 5460)" },
    { name: "prefix", label: "Artikelnr-prefix (4 siffror, 0000–9999)", value: suggested },
    { name: "default_rate_code", label: "Standardmoms", type: "select", value: "25",
      options: RATE_OPTIONS.map((r) => ({ value: r, label: rateLabel(r) })) },
  ], "Spara");
  if (!f || !f.name || !f.bas_konto) return;
  await api("POST", `/books/${bid()}/categories`, {
    name: f.name, kind: f.kind, bas_konto: parseInt(f.bas_konto, 10),
    prefix: (f.prefix || "").trim() || null,
    default_rate_code: f.default_rate_code || null,
  });
  toast(`Kategori sparad (prefix ${(f.prefix || "").trim() || "auto"})`);
  renderWorkspace();
}

// ----- edit flows (reference data is freely editable; issued invoices keep their
// snapshot, so editing the live row never rewrites history) -----
// Manage a customer's household links (related customers for RUT/ROT recipients).
async function householdFlow(c) {
  const name = `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer);
  const [related, all] = await Promise.all([
    api("GET", `/books/${bid()}/customers/${c.kundnummer}/relations`),
    api("GET", `/books/${bid()}/customers`),
  ]);
  const body = $("#modal-body");
  $("#modal-title").textContent = `Hushåll – ${name}`;
  body.innerHTML = "";
  body.appendChild(el("p", { class: "muted" }, "Kopplade hushållsmedlemmar:"));
  const listBox = el("div", {});
  body.appendChild(listBox);
  const render = (rels) => {
    listBox.innerHTML = "";
    if (rels.length === 0) listBox.appendChild(el("p", { class: "muted" }, "Inga kopplingar ännu."));
    for (const r of rels) {
      const rn = `${r.first_name || ""} ${r.last_name || ""}`.trim() || r.company_name || ("Kund " + r.kundnummer);
      listBox.appendChild(el("div", { class: "row", style: "align-items:center;gap:8px" },
        el("span", {}, rn),
        el("button", { class: "btn small ghost danger", onclick: () => guard(async () => {
          await api("DELETE", `/books/${bid()}/customers/${c.kundnummer}/relations/${r.kundnummer}`);
          render(await api("GET", `/books/${bid()}/customers/${c.kundnummer}/relations`));
        }) }, "Ta bort")));
    }
  };
  render(related);
  // add an existing customer as a household member
  const candidates = all.filter((x) => x.kundnummer !== c.kundnummer);
  const pick = el("select", {}, el("option", { value: "" }, "— välj kund —"),
    ...candidates.map((x) => el("option", { value: x.kundnummer },
      x.company_name || `${x.first_name || ""} ${x.last_name || ""}`.trim() || ("Kund " + x.kundnummer))));
  body.appendChild(el("div", { class: "row", style: "margin-top:12px;align-items:flex-end;gap:8px" },
    wrap("Koppla befintlig kund", pick),
    el("button", { class: "btn small", onclick: () => guard(async () => {
      if (!pick.value) return;
      await api("POST", `/books/${bid()}/customers/${c.kundnummer}/relations`,
                { other_kundnummer: parseInt(pick.value, 10) });
      render(await api("GET", `/books/${bid()}/customers/${c.kundnummer}/relations`));
    }) }, "Koppla")));
  $("#modal-ok").textContent = "Stäng";
  $("#modal-backdrop").classList.remove("hidden");
  await new Promise((resolve) => {
    $("#modal-ok").onclick = () => { $("#modal-backdrop").classList.add("hidden"); resolve(); };
    $("#modal-cancel").onclick = () => { $("#modal-backdrop").classList.add("hidden"); resolve(); };
  });
}

// Gratis distanssupport: a customer's support-time balance, quick deductions,
// manual additions, and the full history — the customer's "support profile".
async function supportFlow(c) {
  const name = c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer);
  const body = $("#modal-body");
  $("#modal-title").textContent = `Distanssupport – ${name}`;
  const fmt = (m) => `${m} min` + (Math.abs(m) >= 60 ? ` (${(m / 60).toFixed(1).replace(".", ",")} h)` : "");

  async function render() {
    const s = await api("GET", `/books/${bid()}/customers/${c.kundnummer}/support`);
    body.innerHTML = "";
    body.appendChild(el("div", { class: "box", style: "text-align:center;margin-bottom:10px" },
      el("div", { class: "muted" }, "Kvarvarande supporttid"),
      el("div", { style: "font-size:28px;font-weight:800" }, fmt(s.remaining_minutes)),
      el("div", { class: "muted", style: "font-size:12px" },
        `Intjänat (giltigt): ${fmt(s.earned_active_minutes)} · Använt netto: ${fmt(s.used_minutes)}`)));

    async function entry(minutes, kind, note) {
      await api("POST", `/books/${bid()}/customers/${c.kundnummer}/support`, { minutes, kind, note: note || null });
      toast(kind === "deduction" ? `Drog av ${minutes} min` : `Lade till ${minutes} min`);
      render();
    }

    // quick-deduct buttons
    body.appendChild(el("div", { class: "muted", style: "margin:4px 0" }, "Dra av tid:"));
    body.appendChild(el("div", { class: "row", style: "gap:6px" },
      ...[15, 30, 60].map((mn) => el("button", { class: "btn",
        onclick: () => guard(() => entry(mn, "deduction")) }, `− ${mn} min`))));

    // manual add (free value) + optional note
    const addMin = el("input", { type: "text", placeholder: "minuter", style: "width:90px" });
    const addNote = el("input", { type: "text", placeholder: "anteckning (valfri)", style: "min-width:160px" });
    body.appendChild(el("div", { class: "row", style: "gap:6px;margin-top:10px;align-items:flex-end" },
      wrap("Lägg till tid", addMin), wrap("Notering", addNote),
      el("button", { class: "btn brand", onclick: () => guard(async () => {
        const m = parseInt((addMin.value || "").replace(/\D/g, ""), 10);
        if (!m || m <= 0) { toast("Ange ett antal minuter", true); return; }
        await entry(m, "addition", addNote.value.trim());
      }) }, "+ Lägg till")));

    // history
    body.appendChild(el("h3", { style: "margin:14px 0 4px" }, "Historik"));
    if (!s.ledger.length) {
      body.appendChild(el("p", { class: "muted" }, "Inga uttag eller tillägg ännu."));
    } else {
      body.appendChild(el("table", { style: "width:100%" },
        el("thead", {}, el("tr", {},
          el("th", {}, "Tidpunkt"), el("th", {}, "Typ"),
          el("th", { class: "num" }, "Minuter"), el("th", {}, "Notering"))),
        el("tbody", {}, s.ledger.map((l) => el("tr", {},
          el("td", {}, (l.created_at || "").slice(0, 16).replace("T", " ")),
          el("td", {}, l.kind === "deduction" ? "Uttag" : "Tillägg"),
          el("td", { class: "num", style: "color:" + (l.kind === "deduction" ? "var(--danger,#c33)" : "var(--ok,#2a7)") },
            (l.kind === "deduction" ? "−" : "+") + l.minutes),
          el("td", {}, l.note || ""))))));
    }

    // per-invoice breakdown (transparency)
    if (s.active_invoices.length) {
      body.appendChild(el("h3", { style: "margin:14px 0 4px" }, "Intjänat per faktura (giltiga)"));
      body.appendChild(el("table", { style: "width:100%" },
        el("thead", {}, el("tr", {},
          el("th", {}, "Faktura"), el("th", {}, "Datum"), el("th", { class: "num" }, "Belopp"),
          el("th", { class: "num" }, "Minuter"), el("th", {}, "Giltig t.o.m."))),
        el("tbody", {}, s.active_invoices.map((iv) => el("tr", {},
          el("td", {}, String(iv.invoice_number)), el("td", {}, iv.invoice_date),
          el("td", { class: "num" }, toKr(iv.inc_moms_ore) + " kr"),
          el("td", { class: "num" }, String(iv.support_minutes_earned)),
          el("td", {}, iv.support_expiry_date || ""))))));
    }
  }
  await render();
  $("#modal-ok").textContent = "Stäng";
  $("#modal-cancel").style.display = "none";
  $("#modal-backdrop").classList.remove("hidden");
  await new Promise((resolve) => {
    const close = () => { $("#modal-backdrop").classList.add("hidden"); $("#modal-cancel").style.display = ""; resolve(); };
    $("#modal-ok").onclick = close;
    $("#modal-cancel").onclick = close;
  });
}

async function editCustomerFlow(kundnummer) {
  const c = await api("GET", `/books/${bid()}/customers/${kundnummer}`);
  const isPrivate = c.type === "private";
  const fields = isPrivate
    ? [{ name: "first_name", label: "Förnamn", value: c.first_name || "" },
       { name: "last_name", label: "Efternamn", value: c.last_name || "" },
       { name: "personnummer", label: "Personnummer", value: c.personnummer || "" }]
    : [{ name: "company_name", label: "Företagsnamn", value: c.company_name || "" },
       { name: "org_nr", label: "Org.nr", value: c.org_nr || "" },
       { name: "vat_nr", label: "Momsreg.nr", value: c.vat_nr || "" }];
  fields.push({ name: "street", label: "Gatuadress", value: c.street || "" });
  fields.push({ name: "zip_code", label: "Postnummer", value: c.zip_code || "" });
  fields.push({ name: "city", label: "Ort", value: c.city || "" });
  fields.push({ name: "country", label: "Land", value: c.country || "Sverige" });
  fields.push({ name: "shipping_address", label: "Leveransadress (om annan)", value: c.shipping_address || "" });
  fields.push({ name: "email", label: "E-post", value: c.email || "" });
  fields.push({ name: "phone", label: "Telefon", value: c.phone || "" });
  const f = await modal(`Ändra kund ${kundnummer}`, fields, "Spara");
  if (!f) return;
  await api("PATCH", `/books/${bid()}/customers/${kundnummer}`, _nonEmpty(f));
  toast("Kund uppdaterad");
  renderWorkspace();
}

async function editSupplierFlow(s) {
  const f = await modal(`Ändra leverantör`, [
    { name: "name", label: "Namn", value: s.name },
    { name: "default_moms_rate", label: "Standardmoms", type: "select", value: String(s.default_moms_rate),
      options: ["25", "12", "6", "0", "momsfri", "ej_avdragsgill"].map((r) => ({ value: r, label: r })) },
    { name: "org_nr", label: "Org.nr", value: s.org_nr || "" },
  ], "Spara");
  if (!f) return;
  await api("PATCH", `/books/${bid()}/suppliers/${s.id}`, _nonEmpty(f));
  toast("Leverantör uppdaterad");
  renderWorkspace();
}

// Row actions for a category: edit, activate/inactivate, and delete-if-unused.
function categoryActions(c) {
  const box = el("span", { style: "display:inline-flex;gap:4px;flex-wrap:wrap" },
    editBtn(() => guard(() => editCategoryFlow(c))),
    el("button", { class: "btn small ghost",
      onclick: () => guard(() => toggleCategoryActive(c)) },
      c.active ? "Inaktivera" : "Aktivera"));
  if (c.used) {
    box.appendChild(el("span", { class: "muted", style: "align-self:center;font-size:12px",
      title: "Kontot har bokförts och kan inte tas bort" }, "Använd"));
  } else {
    box.appendChild(el("button", { class: "btn small ghost danger",
      onclick: () => guard(() => deleteCategoryFlow(c)) }, "Ta bort"));
  }
  return box;
}

async function editCategoryFlow(c) {
  // A used BAS-konto must not have its number changed (it would retroactively remap
  // already-booked entries in the reports); only name + default moms stay editable.
  const fields = [{ name: "name", label: "Namn", value: c.name }];
  if (!c.used) fields.push({ name: "bas_konto", label: "BAS-konto", value: c.bas_konto });
  fields.push({ name: "default_rate_code", label: "Standardmoms", type: "select",
    value: c.default_rate_code || "25",
    options: RATE_OPTIONS.map((r) => ({ value: r, label: rateLabel(r) })) });
  const f = await modal(`Ändra kategori`, fields, "Spara");
  if (!f || !f.name) return;
  const body = { name: f.name, default_rate_code: f.default_rate_code || null };
  if (!c.used && f.bas_konto) body.bas_konto = parseInt(f.bas_konto, 10);
  await api("PATCH", `/books/${bid()}/categories/${c.id}`, body);
  toast("Kategori uppdaterad");
  renderWorkspace();
}

async function toggleCategoryActive(c) {
  await api("PATCH", `/books/${bid()}/categories/${c.id}`, { active: !c.active });
  toast(c.active ? "Kategori inaktiverad" : "Kategori aktiverad");
  renderWorkspace();
}

async function deleteCategoryFlow(c) {
  const f = await modal(
    `Ta bort BAS-konto "${c.name}" (${c.bas_konto})? Det har inte använts i bokföringen.`,
    [], "Ta bort");
  if (!f) return;
  await api("DELETE", `/books/${bid()}/categories/${c.id}`);
  toast(`"${c.name}" borttaget`);
  renderWorkspace();
}

// Drop empty strings so a blank field doesn't overwrite with "" (PATCH = partial).
function _nonEmpty(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) if (v !== "" && v != null) out[k] = v;
  return out;
}

// ---------------------------------------------------------------------------
// Backup / restore (.buyn) — one seam, two transports:
//   desktop  -> filesystem paths typed by the user (the FastAPI server reads/writes them)
//   phone    -> window.__BOKYUP_FILES__ (Capacitor Filesystem/Share); see phone/native-bridge.js
// ---------------------------------------------------------------------------
async function exportBackupFlow(bookId, displayName) {
  const safe = (displayName || "bok").replace(/\W+/g, "_");
  if (window.__BOKYUP_FILES__) {
    // Phone: write the bundle into the app FS, then hand bytes to the OS share sheet.
    const fsPath = `/bokyup-data/${safe}.buyn`;
    await api("POST", `/books/${bookId}/export`, { out_path: fsPath });
    await window.__BOKYUP_FILES__.shareFile(fsPath);
    toast("Säkerhetskopia delad");
    return;
  }
  const f = await modal("Exportera säkerhetskopia", [
    { name: "out_path", label: "Sökväg (t.ex. C:\\backup\\" + safe + ".buyn)" },
  ], "Exportera");
  if (!f || !f.out_path) return;
  const res = await api("POST", `/books/${bookId}/export`, { out_path: f.out_path });
  toast("Säkerhetskopia sparad: " + res.out_path);
}

// When a book with the same name already exists, let the user choose to overwrite it
// (replace that book's file — a backup of the old one is auto-made) or create a new book.
// Returns { dest_db_path, overwrite, display_name } or null (cancelled).
async function resolveImportConflict(name, proposedDest) {
  const existing = state.books.find((b) => (b.display_name || "").trim() === (name || "").trim());
  if (!name || !existing) return { dest_db_path: proposedDest, overwrite: false, display_name: name };
  const f = await modal(`En bok med namnet "${name}" finns redan`, [
    { name: "action", label: "Vad vill du göra?", type: "select", options: [
      { value: "new", label: "Skapa en ny bok (behåll båda)" },
      { value: "overwrite", label: "Skriv över den befintliga boken" },
    ] },
  ], "Fortsätt");
  if (!f) return null;
  if (f.action === "overwrite") {
    return { dest_db_path: existing.db_path, overwrite: true, display_name: name, overwrote: true };
  }
  return { dest_db_path: proposedDest, overwrite: false, display_name: name };
}

async function importBackupFlow() {
  if (window.__BOKYUP_FILES__) {
    // Phone: pick a .buyn (copied into the app FS), then import it into IndexedDB.
    const picked = await window.__BOKYUP_FILES__.pickBuynIntoFs();
    if (!picked) return;
    const f = await modal("Återställ säkerhetskopia", [
      { name: "display_name", label: "Namn på boken", value: picked.name || "Återställd bok" },
    ], "Återställ");
    if (!f) return;
    const name = f.display_name || "Återställd bok";
    const proposed = `/bokyup-data/${name.replace(/\W+/g, "_")}_${Date.now()}.db`;
    const target = await resolveImportConflict(name, proposed);
    if (!target) return;
    const rec = await api("POST", "/books/import", { bundle_path: picked.fsPath,
      dest_db_path: target.dest_db_path, display_name: name, overwrite: target.overwrite });
    await loadBooks();
    renderHome();
    toast(target.overwrote ? `Skrev över "${name}" (säkerhetskopia av den gamla gjordes) — lås upp med dess lösenord`
                           : `Återställd: ${rec.display_name} — lås upp med dess lösenord`);
    return;
  }
  const f = await modal("Återställ från säkerhetskopia", [
    { name: "bundle_path", label: "Sökväg till .buyn-fil" },
    { name: "dest_db_path", label: "Spara databasen som (t.ex. C:\\bokforing\\restored.db)" },
    { name: "display_name", label: "Namn på boken (valfritt)" },
  ], "Återställ");
  if (!f || !f.bundle_path || !f.dest_db_path) return;
  const name = f.display_name || null;
  const target = await resolveImportConflict(name, f.dest_db_path);
  if (!target) return;
  const rec = await api("POST", "/books/import", {
    bundle_path: f.bundle_path, dest_db_path: target.dest_db_path,
    display_name: name, overwrite: target.overwrite,
  });
  await loadBooks();
  renderHome();
  toast(target.overwrote ? `Skrev över "${name}" (säkerhetskopia av den gamla gjordes) — lås upp med dess lösenord`
                         : `Återställd: ${rec.display_name} — lås upp med dess lösenord`);
}

// ---------------------------------------------------------------------------
// Invoices (faktura)
// ---------------------------------------------------------------------------

// From the Kunder tab: jump to Fakturor and open a fresh invoice form with this
// customer preselected. The invoices renderer picks up state.pendingInvoiceCustomer.
function newInvoiceForCustomer(kundnummer) {
  state.pendingInvoiceCustomer = kundnummer;
  state.section = "invoices";
  renderWorkspace();
}

async function offertToInvoiceFlow(o) {
  const today = new Date().toISOString().slice(0, 10);
  const due = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
  const f = await modal(`Skapa faktura från offert ${o.offert_number}?`, [
    { name: "invoice_date", label: "Fakturadatum", type: "date", value: today },
    { name: "due_date", label: "Förfallodatum", type: "date", value: due },
  ], "Skapa faktura");
  if (!f) return;
  const res = await api("POST", `/books/${bid()}/offerter/${o.id}/create-invoice`,
    { invoice_date: f.invoice_date, due_date: f.due_date });
  toast(`Faktura ${res.invoice_number} skapad från offert ${o.offert_number}`);
  showPdf(`/books/${bid()}/invoices/${res.invoice_id}/pdf`, `Faktura ${res.invoice_number}`);
  renderWorkspace();
}

// Inköp line-item editor: each row is an article (name + product category) bought at a
// qty × à-cost (ex moms) + moms rate. A named row becomes a stock batch on booking; a
// blank-name row is a pure cost line (still booked, no stock). Picking an existing
// article prefills the row (so buying it again just adds a batch to the same article).
function purchaseItemsEditor(incomeCats, articles, onChange) {
  const rowsBox = el("div", {});
  const cats = incomeCats || [];
  const arts = articles || [];
  const fire = () => { if (onChange) onChange(); };
  function addRow(v) {
    v = v || {};
    const pick = el("select", { style: "min-width:150px" },
      el("option", { value: "" }, "— ny artikel —"),
      ...arts.map((a) => el("option", { value: a.id }, `${a.article_number} ${a.description}`)));
    const name = el("input", { type: "text", placeholder: "Artikelnamn (tomt = ren kostnad)",
      value: v.description || "", oninput: fire });
    const cat = el("select", {}, el("option", { value: "" }, "(ingen)"),
      el("option", { value: NEW_CAT }, "➕ Ny kategori…"),
      ...cats.map((c) => el("option", { value: c.id }, `${c.prefix || "?"} · ${c.name}`)));
    if (v.category_id) cat.value = String(v.category_id);
    wireCategoryCreateSelect(cat, cats, (nc) => {
      // keep the label format (prefix · name) consistent with the other options
      const o = cat.querySelector(`option[value="${nc.id}"]`);
      if (o) o.textContent = catLabel(nc);
    });
    const qty = el("input", { type: "text", value: v.qty || "1", style: "width:60px", oninput: fire });
    const cost = el("input", { type: "text", value: v.cost || "0,00", style: "width:96px", oninput: fire });
    const rate = el("select", { onchange: fire }, ...RATE_OPTIONS.map((r) => el("option", { value: r }, rateLabel(r))));
    if (v.rate_code) rate.value = v.rate_code;
    pick.onchange = () => {
      const a = arts.find((x) => String(x.id) === pick.value);
      if (!a) return;
      name.value = a.description || "";
      cat.value = a.category_id ? String(a.category_id) : "";
      if (a.rate_code) rate.value = a.rate_code;
      fire();
    };
    name.addEventListener("input", () => { pick.value = ""; });
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end;flex-wrap:wrap" },
      wrap("Befintlig", pick), wrap("Artikelnamn", name), wrap("Produktkategori", cat),
      wrap("Antal", qty), wrap("À-pris ex moms", cost), wrap("Moms", rate),
      el("button", { class: "btn small ghost", onclick: (e) => { e.target.closest(".row").remove(); fire(); } }, "✕"));
    row._get = () => ({
      description: name.value.trim() || null,
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      quantity_centi: Math.round(parseFloat((qty.value || "0").replace(",", ".")) * 100),
      unit_cost_ore: toOre(cost.value), rate_code: rate.value, to_stock: true,
    });
    rowsBox.appendChild(row);
  }
  addRow();
  const element = el("div", {}, rowsBox,
    el("button", { class: "btn small ghost", onclick: () => addRow() }, "+ Rad"));
  return { element, get: () => [...rowsBox.children].map((r) => r._get()).filter((l) => l.quantity_centi > 0) };
}

async function purchaseForm(panel) {
  const [cats, suppliers, articles] = await Promise.all([
    api("GET", `/books/${bid()}/categories`),
    api("GET", `/books/${bid()}/suppliers`),
    api("GET", `/books/${bid()}/articles`),
  ]);
  const expenseCats = cats.filter((c) => c.kind === "expense");
  const incomeCats = cats.filter((c) => c.kind === "income");
  if (expenseCats.length === 0) {
    toast("Lägg till minst en utgiftskategori (BAS-konto) först", true);
    return;
  }
  panel.innerHTML = "";
  panel.appendChild(el("h2", {}, "Nytt inköp"));
  const supplier = el("select", {}, el("option", { value: "" }, "— (ingen leverantör) —"),
    ...suppliers.map((s) => el("option", { value: s.id }, s.name)));
  const cat = el("select", {}, ...expenseCats.map((c) => el("option", { value: c.id }, c.name)));
  const today = new Date().toISOString().slice(0, 10);
  const date = el("input", { type: "date", value: today });
  const extRef = el("input", { type: "text", placeholder: "t.ex. kvitto 1234 / faktura FAKT-99" });
  const paidNow = el("select", {},
    el("option", { value: "yes" }, "Ja, betald nu"),
    el("option", { value: "no" }, "Nej, leverantörsfaktura (betalas senare)"));
  const payDate = el("input", { type: "date", value: today });
  const payDateWrap = wrap("Betaldatum", payDate);
  paidNow.onchange = () => { payDateWrap.style.display = paidNow.value === "yes" ? "" : "none"; };
  const totalsBox = el("p", { class: "muted", style: "margin-top:6px" });
  const updateTotals = () => {
    let ex = 0, moms = 0;
    for (const it of items.get()) {
      const lineEx = Math.round(it.quantity_centi * it.unit_cost_ore / 100);
      ex += lineEx; moms += Math.round(lineEx * (RATE_PCT[it.rate_code] || 0));
    }
    totalsBox.textContent = `Summa: ${toKr(ex)} kr ex moms + ${toKr(moms)} kr moms = ${toKr(ex + moms)} kr`;
  };
  const items = purchaseItemsEditor(incomeCats, articles, updateTotals);
  const receipt = receiptPicker();

  panel.appendChild(el("div", { class: "row" },
    wrap("Leverantör", supplier), wrap("Bokförs på (kostnadskonto)", cat),
    wrap("Kvitto-/fakturanummer", extRef)));
  panel.appendChild(el("div", { class: "row" },
    wrap("Inköpsdatum", date), wrap("Betald?", paidNow), payDateWrap));
  panel.appendChild(el("div", { style: "margin-top:6px" },
    el("label", {}, "Artiklar (namnge en rad → den läggs i lager som en batch)"), items.element));
  panel.appendChild(totalsBox);
  panel.appendChild(el("div", { style: "margin-top:6px" },
    el("label", {}, "Kvitto/faktura (bild eller PDF, valfritt)"), receipt.element));
  panel.appendChild(el("div", { style: "margin-top:14px" },
    el("button", { class: "btn brand", onclick: () => guard(submit) }, "Bokför inköp"),
    el("button", { class: "btn ghost", style: "margin-left:8px",
      onclick: () => { state.section = "purchases"; renderWorkspace(); } }, "Avbryt")));
  updateTotals();

  async function submit() {
    const rows = items.get();
    if (rows.length === 0) { toast("Lägg till minst en rad med belopp", true); return; }
    const paid_date = paidNow.value === "yes" ? payDate.value : null;
    const res = await api("POST", `/books/${bid()}/expenses`, {
      supplier_id: supplier.value ? parseInt(supplier.value, 10) : null,
      category_id: parseInt(cat.value, 10), items: rows, trans_date: date.value,
      ext_ref: extRef.value || null, paid_date,
    });
    const staged = receipt.getStaged();
    if (staged) {
      await api("POST", `/books/${bid()}/transaktioner/${res.transaktion_id}/receipts`, {
        image_base64: staged.image_base64, mime: staged.mime, original_format: staged.original_format,
      });
    }
    state.section = "purchases";
    renderWorkspace();
    const n = (res.batches || []).length;
    toast((paid_date ? "Inköp bokfört (betalt)" : "Leverantörsfaktura bokförd (väntar på betalning)")
      + (n ? ` — ${n} artikel${n > 1 ? "/artiklar" : ""} lagd i lager` : ""));
  }
}

async function invoiceForm(panel, draft) {
  const dp = (draft && draft.payload) || {};   // prefill from a saved draft
  let draftId = draft ? draft.id : null;
  const [customers, cats, redCfg, articles] = await Promise.all([
    api("GET", `/books/${bid()}/customers`),
    api("GET", `/books/${bid()}/categories`),
    api("GET", `/books/${bid()}/reduction-config`),
    api("GET", `/books/${bid()}/articles`),
  ]);
  const incomeCats = cats.filter((c) => c.kind === "income");
  if (customers.length === 0 || incomeCats.length === 0) {
    toast("Lägg till minst en kund och en inkomstkategori först", true);
    return;
  }
  panel.innerHTML = "";
  panel.appendChild(el("h2", {}, draftId ? `Utkast (forts.)` : "Ny faktura"));

  const custSel = searchableSelect(
    customers.map((c) => ({ value: c.kundnummer,
      label: c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer) })),
    dp.customer_id, "Sök kund…");
  const customer = custSel.select;
  const cat = el("select", {},
    el("option", { value: "" }, "— välj per rad —"),
    ...incomeCats.map((c) => el("option", { value: c.id }, c.name)));
  if (dp.category_id) cat.value = String(dp.category_id);
  const today = new Date().toISOString().slice(0, 10);
  const due = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
  const invDate = el("input", { type: "date", value: dp.invoice_date || today });
  const dueDate = el("input", { type: "date", value: dp.due_date || due });
  const delivery = el("input", { type: "date", value: dp.delivery_date || "" });
  const terms = el("input", { type: "text", value: dp.payment_terms || "30 dagar netto" });
  const yourRef = el("input", { type: "text", value: dp.your_reference || "" });
  const note = el("input", { type: "text", value: dp.note || "" });
  const recips = recipientsEditor({
    rutPct: redCfg.rut_pct, rotPct: redCfg.rot_pct,
    getLines: () => lines.get(), getInvoiceCustomerId: () => parseInt(customer.value, 10),
    getYear: () => parseInt((invDate.value || "").slice(0, 4), 10) || new Date().getFullYear(),
    initialRecipients: dp.recipients || [],
  });
  // Live totals box, updated on every line change (below).
  const totalsBox = el("div", { class: "box", style: "margin-top:14px" });
  const kvRow = (k, ore, opts = {}) => el("div", { style: "display:flex;justify-content:space-between;"
    + "padding:2px 0;" + (opts.bold ? "font-weight:700;font-size:16px;border-top:1px solid var(--border,#ccc);margin-top:4px;padding-top:6px;" : "")
    + (opts.red ? "color:var(--danger,#c33);" : "") },
    el("span", {}, k), el("span", { style: "font-variant-numeric:tabular-nums" }, toKr(ore) + " kr"));
  // Lazy per-article stock-batch loader (open batches only), cached across rows.
  const batchCache = new Map();
  async function loadBatches(articleId) {
    if (!batchCache.has(articleId)) {
      batchCache.set(articleId, await api("GET",
        `/books/${bid()}/articles/${articleId}/batches?open_only=1`));
    }
    return batchCache.get(articleId);
  }
  function updateTotals() {
    const t = invoiceTotals(lines.get(), redCfg.rut_pct, redCfg.rot_pct);
    totalsBox.innerHTML = "";
    totalsBox.appendChild(el("div", { style: "font-weight:600;margin-bottom:4px" }, "Summering (preliminär)"));
    if (t.rabatt) totalsBox.appendChild(kvRow("Total rabatt", -t.rabatt, { red: true }));
    totalsBox.appendChild(kvRow("Summa exkl. moms", t.ex));
    totalsBox.appendChild(kvRow("Moms", t.moms));
    if (t.husavdrag) {
      totalsBox.appendChild(kvRow("Summa inkl. moms", t.inc));
      if (t.rut) totalsBox.appendChild(kvRow("− RUT-avdrag", -t.rut, { red: true }));
      if (t.rot) totalsBox.appendChild(kvRow("− ROT-avdrag", -t.rot, { red: true }));
    }
    totalsBox.appendChild(kvRow(t.husavdrag ? "Att betala för kund (ca)" : "Att betala (ca)", t.attBetala, { bold: true }));
    // Real margin from picked lager-batches (revenue − inköpskostnad for those lines).
    const m = lines.getMargin();
    if (m.hasCost) {
      totalsBox.appendChild(el("div", { style: "border-top:1px solid var(--border,#ccc);margin-top:6px;padding-top:6px" }));
      totalsBox.appendChild(kvRow("Inköpskostnad (lagerförda rader)", m.cost));
      const pct = m.revenue ? Math.round(m.margin / m.revenue * 100) : 0;
      totalsBox.appendChild(kvRow(`Marginal (${pct} %)`, m.margin, { bold: true }));
    }
  }
  const lines = lineItemsEditor(incomeCats, () => { recips.recompute(); updateTotals(); }, dp.lines || [], articles, loadBatches);
  customer.onchange = () => recips.reloadPeople();
  invDate.onchange = () => recips.refreshCaps();

  panel.appendChild(el("div", { class: "row" },
    wrap("Kund", custSel.element), wrap("Standardkategori (BAS, valfri)", cat)));
  panel.appendChild(el("div", { class: "row" },
    wrap("Fakturadatum", invDate), wrap("Förfallodatum", dueDate),
    wrap("Leveransdatum (valfritt)", delivery)));
  panel.appendChild(el("h3", { style: "margin-top:18px" }, "Artikelrader"));
  panel.appendChild(el("p", { class: "muted" },
    "Varje rad bokförs på sin egen kategori (BAS-konto). Markera RUT eller ROT på de "
    + "arbetsrader som är husavdragsberättigade (hela radens belopp räknas som arbete)."));
  panel.appendChild(lines.element);
  panel.appendChild(el("h3", { style: "margin-top:18px" }, "RUT/ROT-mottagare (hushåll)"));
  panel.appendChild(el("p", { class: "muted" },
    `Skattereduktionen (RUT ${redCfg.rut_pct} %, ROT ${redCfg.rot_pct} % på arbete inkl. moms) `
    + "bildar en pott som mottagarna delar på. Välj fakturakunden eller en hushållsmedlem, "
    + "ange personnummer och andel (%). Beloppen räknas ut nedan."));
  panel.appendChild(recips.element);
  panel.appendChild(el("h3", { style: "margin-top:18px" }, "Summa"));
  panel.appendChild(totalsBox);
  panel.appendChild(el("div", { class: "row", style: "margin-top:14px" },
    wrap("Betalningsvillkor", terms), wrap("Er referens", yourRef), wrap("Notering", note)));
  panel.appendChild(el("div", { style: "margin-top:16px" },
    el("button", { class: "btn brand", onclick: () => guard(submit) }, "Skapa faktura"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => guard(saveDraft) }, "Spara utkast"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => guard(createOffert) }, "Skapa offert"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => { state.section = "invoices"; renderWorkspace(); } }, "Avbryt")));

  await recips.reloadPeople();
  updateTotals();

  // Collect the current form state (may be incomplete — that's fine for a draft).
  function collectBody() {
    return {
      customer_id: parseInt(customer.value, 10) || null,
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      invoice_date: invDate.value, due_date: dueDate.value,
      delivery_date: delivery.value || null, payment_terms: terms.value || null,
      your_reference: yourRef.value || null, note: note.value || null,
      lines: lines.get(), recipients: recips.get(),
    };
  }

  async function createOffert() {
    const payload = collectBody();
    if (!payload.customer_id) { toast("Välj en kund först", true); return; }
    if (!payload.lines || payload.lines.length === 0) { toast("Lägg till minst en rad", true); return; }
    const o = await api("POST", `/books/${bid()}/offerter`, { payload });
    toast(`Offert ${o.offert_number} skapad`);
    showPdf(`/books/${bid()}/offerter/${o.offert_id}/pdf`, `Offert ${o.offert_number}`);
  }

  async function saveDraft() {
    const payload = collectBody();
    if (draftId) {
      await api("PUT", `/books/${bid()}/invoice-drafts/${draftId}`, { payload });
    } else {
      const res = await api("POST", `/books/${bid()}/invoice-drafts`, { payload });
      draftId = res.id;
    }
    toast("Utkast sparat");
  }

  async function submit() {
    const body = collectBody();
    if ((body.lines || []).length === 0) { toast("Lägg till minst en artikelrad", true); return; }
    const res = await api("POST", `/books/${bid()}/invoices`, body);
    if (draftId) { try { await api("DELETE", `/books/${bid()}/invoice-drafts/${draftId}`); } catch (e) { /* ignore */ } }
    toast(`Faktura ${res.invoice_number} skapad`);
    for (const w of res.cap_warnings || []) {
      if (w.over_cap || w.near_cap) {
        toast(`OBS: ${w.name} ${w.over_cap ? "har överskridit" : "närmar sig"} `
          + `husavdragstaket i år (${toKr(w.used_ore)}/${toKr(w.cap_ore)} kr totalt)`, true);
      }
      if (w.rot_over_cap || w.rot_near_cap) {
        toast(`OBS: ${w.name} ${w.rot_over_cap ? "har överskridit" : "närmar sig"} `
          + `ROT-taket i år (${toKr(w.rot_used_ore)}/${toKr(w.rot_cap_ore)} kr)`, true);
      }
    }
    state.section = "invoices";
    renderWorkspace();
    invoicePdf(res.invoice_id, res.invoice_number);
  }
}

function lineItemsEditor(incomeCats, onChange, initialLines, articles, loadBatches) {
  const rowsBox = el("div", {});
  const cats = incomeCats || [];
  const arts = articles || [];
  const fire = () => { if (onChange) onChange(); };
  function addRow(v) {                                  // v = optional prefill (draft)
    v = v || {};
    // Article picker: choose an existing article (fills the row; price stays editable).
    const pick = el("select", {},
      el("option", { value: "" }, "— välj artikel —"),
      ...arts.map((a) => el("option", { value: a.id }, `${a.article_number} ${a.description}`)));
    let articleId = v.article_id || null;
    // Stock-batch picker: choose which lager-batch this line is sold from → real margin
    // (revenue − batch cost). Only populated once an article with stock is picked.
    const batchSel = el("select", { style: "min-width:150px" },
      el("option", { value: "" }, "— ingen batch —"));
    let batchCost = null;                 // unit_cost_ore of the selected batch (for margin)
    let batchCache = null;                // this row's loaded batches
    async function refreshBatches(selectId) {
      batchSel.innerHTML = "";
      batchSel.appendChild(el("option", { value: "" }, "— ingen batch —"));
      batchCost = null;
      if (!articleId || !loadBatches) return;
      batchCache = await loadBatches(articleId);
      for (const b of batchCache) {
        const label = b.full_batch_id || ("#" + b.batch_number);
        batchSel.appendChild(el("option", { value: b.id },
          `${label} · ${(b.qty_remaining_centi / 100).toLocaleString("sv-SE")} kvar · ${toKr(b.unit_cost_ore)} kr/st`));
      }
      if (selectId != null) batchSel.value = String(selectId);
      const sel = batchCache.find((b) => String(b.id) === batchSel.value);
      batchCost = sel ? sel.unit_cost_ore : null;
    }
    batchSel.onchange = () => {
      const sel = (batchCache || []).find((b) => String(b.id) === batchSel.value);
      batchCost = sel ? sel.unit_cost_ore : null;
      fire();
    };
    const desc = el("input", { type: "text", placeholder: "Beskrivning", value: v.description || "" });
    const cat = el("select", {},
      el("option", { value: "" }, "(standard)"),
      el("option", { value: NEW_CAT }, "➕ Ny kategori…"),
      ...cats.map((c) => el("option", { value: c.id }, c.name)));
    if (v.category_id) cat.value = String(v.category_id);
    // Allow creating a category inline; on create pre-fill the row's moms from it.
    wireCategoryCreateSelect(cat, cats, (nc) => {
      if (nc.default_rate_code) rate.value = nc.default_rate_code;
      fire();
    });
    const qtyVal = v.quantity_centi != null ? String(v.quantity_centi / 100).replace(".", ",") : "1";
    const qty = el("input", { type: "text", value: qtyVal, style: "width:64px", oninput: fire });
    const unit = el("input", { type: "text", value: v.unit || "st", style: "width:54px" });
    const priceVal = v.unit_price_ore != null ? toKr(v.unit_price_ore) : "0,00";
    const price = el("input", { type: "text", value: priceVal, style: "width:96px", oninput: fire });
    const discVal = v.discount_pct_centi ? String(v.discount_pct_centi / 100).replace(".", ",") : "";
    const disc = el("input", { type: "text", value: discVal, placeholder: "0", style: "width:54px", oninput: fire });
    const rate = el("select", { onchange: fire }, ...RATE_OPTIONS.map((r) => el("option", { value: r }, rateLabel(r))));
    if (v.rate_code) rate.value = v.rate_code;
    const red = el("select", { onchange: fire },
      el("option", { value: "" }, "—"),
      el("option", { value: "rut" }, "RUT"),
      el("option", { value: "rot" }, "ROT"));
    if (v.reduction_type) red.value = v.reduction_type;
    cat.onchange = () => {
      const c = cats.find((x) => String(x.id) === cat.value);
      if (c && c.default_rate_code) rate.value = c.default_rate_code;
    };
    // Picking an article prefills everything (price included — still editable).
    pick.onchange = () => {
      const a = arts.find((x) => String(x.id) === pick.value);
      if (!a) { articleId = null; guard(() => refreshBatches()); return; }
      articleId = a.id;
      desc.value = a.description || "";
      price.value = a.unit_price_ore != null ? toKr(a.unit_price_ore) : price.value;
      if (a.unit) unit.value = a.unit;
      if (a.rate_code) rate.value = a.rate_code;
      red.value = a.reduction_type || "";
      cat.value = a.category_id ? String(a.category_id) : "";
      guard(() => refreshBatches());
      fire();
    };
    // Typing a fresh description detaches the row from a picked article.
    desc.oninput = () => { articleId = null; pick.value = ""; guard(() => refreshBatches()); };
    // Save this row's current values to the catalog as a new article.
    const saveBtn = el("button", { class: "btn small ghost", type: "button", title: "Spara som artikel",
      onclick: () => guard(() => saveRowAsArticle(row)) }, "★");
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end;flex-wrap:wrap" },
      wrap("Artikel", pick), wrap("Beskrivning", desc), wrap("Kategori (BAS)", cat),
      wrap("Antal", qty), wrap("Enhet", unit), wrap("À-pris ex moms", price),
      wrap("% rabatt", disc), wrap("Moms", rate), wrap("Husavdrag", red),
      wrap("Lagerbatch", batchSel), saveBtn,
      el("button", { class: "btn small ghost", onclick: (e) => { e.target.closest(".row").remove(); fire(); } }, "✕"));
    row._desc = desc; row._cat = cat; row._unit = unit; row._price = price; row._disc = disc; row._rate = rate; row._red = red;
    const qtyCenti = () => Math.round(parseFloat((qty.value || "0").replace(",", ".")) * 100);
    row._get = () => ({
      description: desc.value.trim(),
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      quantity_centi: qtyCenti(),
      unit: unit.value || null, unit_price_ore: toOre(price.value),
      discount_pct_centi: Math.round(parseFloat((disc.value || "0").replace(",", ".")) * 100) || 0,
      rate_code: rate.value, reduction_type: red.value || null, article_id: articleId,
      stock_batch_id: batchSel.value ? parseInt(batchSel.value, 10) : null,
    });
    // Margin preview (ören): the frozen inköpskostnad of this line if a batch is picked,
    // else null (unknown cost). Revenue side comes from _get() ex moms.
    row._costPreview = () => (batchSel.value && batchCost != null ? Math.round(qtyCenti() * batchCost / 100) : null);
    rowsBox.appendChild(row);
    if (v.article_id) guard(() => refreshBatches(v.stock_batch_id));
  }
  async function saveRowAsArticle(row) {
    if (!row._desc.value.trim()) { toast("Fyll i beskrivning först", true); return; }
    const f = await modal("Spara som artikel", [
      { name: "prefix", label: "Artikelnr-prefix (4 siffror)", value: "1000" },
    ], "Spara");
    if (!f || !(f.prefix || "").trim()) return;
    const a = await api("POST", `/books/${bid()}/articles`, {
      prefix: f.prefix.trim(), description: row._desc.value.trim(),
      unit_price_ore: toOre(row._price.value), unit: row._unit.value || null,
      rate_code: row._rate.value, reduction_type: row._red.value || null,
      category_id: row._cat.value ? parseInt(row._cat.value, 10) : null,
    });
    arts.push({ id: a.id, article_number: a.article_number, description: row._desc.value.trim(),
      unit_price_ore: toOre(row._price.value), unit: row._unit.value || null,
      rate_code: row._rate.value, reduction_type: row._red.value || null,
      category_id: row._cat.value ? parseInt(row._cat.value, 10) : null });
    toast(`Artikel ${a.article_number} sparad`);
  }
  if (initialLines && initialLines.length) initialLines.forEach(addRow); else addRow();
  const element = el("div", {}, rowsBox,
    el("button", { class: "btn small ghost", onclick: () => addRow() }, "+ Rad"));
  // Aggregate real margin from rows that picked a stock batch: revenue ex moms (after
  // rabatt) of those rows − their frozen cost. `hasCost` says whether any row is costed.
  const getMargin = () => {
    let cost = 0, revenue = 0, hasCost = false;
    for (const r of rowsBox.children) {
      const c = r._costPreview();
      if (c == null) continue;
      hasCost = true;
      cost += c;
      const g = r._get();
      const gross = Math.round((g.quantity_centi || 0) * (g.unit_price_ore || 0) / 100);
      revenue += gross - Math.round(gross * (g.discount_pct_centi || 0) / 10000);
    }
    return { cost, revenue, margin: revenue - cost, hasCost };
  };
  return {
    element, getMargin,
    get: () => [...rowsBox.children].map((r) => r._get()).filter((l) => l.description),
  };
}

// Recipient editor: pick the invoice customer or a linked household member (or add a
// new person, which creates a customer + household link), enter personnummer + share %,
// and see each person's RUT/ROT kronor computed live from the article pots.
function recipientsEditor(opts) {
  const { rutPct, rotPct, getLines, getInvoiceCustomerId, getYear } = opts;
  const initialRecipients = opts.initialRecipients || [];
  const rowsBox = el("div", {});
  const potInfo = el("p", { class: "muted" }, "Pott: —");
  let people = [];                 // [{kundnummer, name}]
  let seeded = false;              // initial (draft) recipients added once
  const capCache = new Map();      // `${cid}:${year}` -> cap status
  const custCache = new Map();     // kundnummer -> customer (for personnummer prefill)

  // A filterable person picker (searchableSelect): the "— välj person —" placeholder
  // plus the invoice customer + their linked household members. Returns {element,
  // select} so callers keep the native select API (.value / .onchange / rebuild).
  function peopleOptions(selected) {
    return searchableSelect(
      [{ value: "", label: "— välj person —" },
        ...people.map((p) => ({ value: p.kundnummer, label: p.name }))],
      selected, "Sök person…");
  }

  // When a customer is picked, prefill the personnummer from their kundkort if it is
  // already stored (so it does not need re-entering) and the field is still empty.
  async function prefillPnr(row) {
    const cid = row._who.value;
    if (!cid || row._pnr.value.trim()) return;
    let cust = custCache.get(cid);
    if (!cust) {
      try { cust = await api("GET", `/books/${bid()}/customers/${cid}`); custCache.set(cid, cust); }
      catch (e) { return; }
    }
    if (cust && cust.personnummer) row._pnr.value = cust.personnummer;
  }

  function onWhoChange(row) {
    return () => { recompute(); refreshCaps(); prefillPnr(row); };
  }

  function addRow(v) {
    v = v || {};
    const whoSel = peopleOptions(v.customer_id);
    const who = whoSel.select;
    const pnr = el("input", { type: "text", placeholder: "ÅÅMMDD-XXXX", style: "width:120px",
      value: v.personnummer || "" });
    const rutShare = el("input", { type: "text", value: v.rut_share_pct != null ? String(v.rut_share_pct) : "100",
      style: "width:56px", oninput: recompute });
    const rotShare = el("input", { type: "text", value: v.rot_share_pct != null ? String(v.rot_share_pct) : "100",
      style: "width:56px", oninput: recompute });
    const out = el("span", { class: "muted", style: "align-self:center" }, "—");
    const cap = el("span", { class: "muted", style: "align-self:center;font-size:12px" }, "");
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end" },
      wrap("Person", whoSel.element), wrap("Personnummer", pnr),
      wrap("RUT %", rutShare), wrap("ROT %", rotShare), wrap("Belopp", out), wrap("Tak", cap),
      el("button", { class: "btn small ghost", onclick: (e) => { e.target.closest(".row").remove(); recompute(); } }, "✕"));
    row._who = who; row._whoWrap = whoSel.element;
    row._pnr = pnr; row._rut = rutShare; row._rot = rotShare; row._out = out; row._cap = cap;
    who.onchange = onWhoChange(row);
    rowsBox.appendChild(row);
    if (v.customer_id) prefillPnr(row);
    recompute(); refreshCaps();
  }

  function rowAmount(row, pots) {
    const rp = parseFloat((row._rut.value || "0").replace(",", ".")) || 0;
    const op = parseFloat((row._rot.value || "0").replace(",", ".")) || 0;
    return { rut: Math.round(pots.rut * rp / 100), rot: Math.round(pots.rot * op / 100) };
  }

  function recompute() {
    const pots = potsFromLines(getLines ? getLines() : [], rutPct, rotPct);
    potInfo.textContent = `Pott — RUT: ${toKr(pots.rut)} kr, ROT: ${toKr(pots.rot)} kr`;
    for (const row of rowsBox.children) {
      const a = rowAmount(row, pots);
      const parts = [];
      if (pots.rut) parts.push(`RUT ${toKr(a.rut)}`);
      if (pots.rot) parts.push(`ROT ${toKr(a.rot)}`);
      row._out.textContent = parts.length ? parts.join(" · ") + " kr" : "—";
      updateCapNote(row, a);
    }
  }

  // Per-recipient annual cap note vs BOTH caps: the combined RUT+ROT cap and the
  // ROT-only sub-cap. Red if this invoice would push either over.
  function updateCapNote(row, amt) {
    const cid = row._who.value;
    const year = getYear ? getYear() : new Date().getFullYear();
    const st = capCache.get(`${cid}:${year}`);
    if (!cid || !st) { row._cap.textContent = ""; return; }
    const overCombined = (amt.rut + amt.rot) > st.remaining_ore || st.over_cap;
    const overRot = amt.rot > st.rot_remaining_ore || st.rot_over_cap;
    const note = `kvar ${toKr(st.remaining_ore)}`
      + (st.rot_cap_ore ? ` · ROT ${toKr(st.rot_remaining_ore)}` : "") + " kr";
    const over = overCombined || overRot;
    row._cap.textContent = note;
    row._cap.style.color = over ? "var(--danger)" : "";
    row._cap.title = `Använt i år: ${toKr(st.used_ore)} / ${toKr(st.cap_ore)} kr (totalt)`
      + `\nROT: ${toKr(st.rot_used_ore)} / ${toKr(st.rot_cap_ore)} kr`;
  }

  async function refreshCaps() {
    const year = getYear ? getYear() : new Date().getFullYear();
    const ids = [...new Set([...rowsBox.children].map((r) => r._who.value).filter(Boolean))];
    for (const cid of ids) {
      const key = `${cid}:${year}`;
      if (!capCache.has(key)) {
        try { capCache.set(key, await api("GET", `/books/${bid()}/customers/${cid}/husavdrag-cap/${year}`)); }
        catch (e) { /* ignore */ }
      }
    }
    recompute();
  }

  async function reloadPeople() {
    const cid = getInvoiceCustomerId();
    people = [];
    capCache.clear();
    if (cid) {
      try {
        const me = await api("GET", `/books/${bid()}/customers/${cid}`);
        const nm = `${me.first_name || ""} ${me.last_name || ""}`.trim() || me.company_name || ("Kund " + cid);
        people.push({ kundnummer: cid, name: nm + " (fakturakund)" });
        const rel = await api("GET", `/books/${bid()}/customers/${cid}/relations`);
        for (const r of rel) {
          people.push({ kundnummer: r.kundnummer,
            name: `${r.first_name || ""} ${r.last_name || ""}`.trim() || r.company_name || ("Kund " + r.kundnummer) });
        }
      } catch (e) { /* ignore */ }
    }
    // Seed draft recipients once, after the people list is first available.
    if (!seeded && initialRecipients.length) {
      seeded = true;
      initialRecipients.forEach(addRow);
    }
    // repopulate existing rows' selects, keeping selection if still present
    for (const row of rowsBox.children) {
      const prev = row._who.value;
      const fresh = peopleOptions(prev);
      fresh.select.onchange = onWhoChange(row);
      row._whoWrap.replaceWith(fresh.element);
      row._who = fresh.select;
      row._whoWrap = fresh.element;
    }
    refreshCaps();
  }

  async function addNewPerson() {
    const cid = getInvoiceCustomerId();
    const f = await modal("Ny hushållsmedlem (skapas som kund och kopplas)", [
      { name: "first_name", label: "Förnamn" },
      { name: "last_name", label: "Efternamn" },
      { name: "personnummer", label: "Personnummer" },
    ], "Skapa & koppla");
    if (!f || !f.first_name || !f.last_name) return;
    const created = await api("POST", `/books/${bid()}/customers`, {
      type: "private", first_name: f.first_name, last_name: f.last_name,
      personnummer: f.personnummer || null,
    });
    if (cid && created.kundnummer !== cid) {
      await api("POST", `/books/${bid()}/customers/${cid}/relations`, { other_kundnummer: created.kundnummer });
    }
    await reloadPeople();
    addRow(created.kundnummer);
  }

  const element = el("div", {}, potInfo, rowsBox,
    el("button", { class: "btn small ghost", onclick: () => addRow() }, "+ Mottagare"),
    el("button", { class: "btn small ghost", style: "margin-left:6px",
      onclick: () => guard(addNewPerson) }, "+ Ny person"));

  return {
    element, reloadPeople, recompute, refreshCaps,
    get: () => [...rowsBox.children].map((row) => ({
      customer_id: row._who.value ? parseInt(row._who.value, 10) : null,
      personnummer: row._pnr.value.trim() || null,
      rut_share_pct: parseFloat((row._rut.value || "0").replace(",", ".")) || 0,
      rot_share_pct: parseFloat((row._rot.value || "0").replace(",", ".")) || 0,
    })).filter((r) => r.customer_id || r.personnummer),
  };
}

// Show a PDF in an in-app viewer. This avoids relying on the native window's
// download/new-window handling (pywebview blocks <a download target=_blank> by
// default): the PDF renders inline in an <iframe>. Desktop points the iframe at the
// same-origin endpoint (WebView2/browsers render PDFs inline); the phone build (no
// HTTP) builds a blob from the base64 the native bridge returns.
async function showPdf(path, fname) {
  let src, revoke = null;
  if (window.__BOKYUP_NATIVE__) {
    const r = await api("GET", path);                 // { raw, base64, media_type }
    const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
    src = URL.createObjectURL(new Blob([bytes], { type: r.media_type || "application/pdf" }));
    revoke = src;
  } else {
    src = API + path;                                 // same-origin; rendered inline
  }
  $("#modal-title").textContent = fname;
  const body = $("#modal-body");
  body.innerHTML = "";
  body.appendChild(el("iframe", { src, title: fname,
    style: "width:100%;height:68vh;border:1px solid var(--border,#ccc);border-radius:6px;background:#fff" }));
  body.appendChild(el("div", { style: "margin-top:8px" },
    el("a", { href: src, download: fname, class: "btn small ghost" }, "Ladda ner")));
  // Widen the modal for the document and restore afterwards.
  const dialog = $("#modal-backdrop .modal");
  const prevWidth = dialog ? dialog.style.width : "";
  if (dialog) dialog.style.width = "min(900px, 94vw)";
  $("#modal-cancel").style.display = "none";
  $("#modal-ok").textContent = "Stäng";
  $("#modal-backdrop").classList.remove("hidden");
  await new Promise((resolve) => {
    const close = () => {
      $("#modal-backdrop").classList.add("hidden");
      $("#modal-cancel").style.display = "";
      if (dialog) dialog.style.width = prevWidth;
      if (revoke) URL.revokeObjectURL(revoke);
      $("#modal-ok").onclick = null; $("#modal-cancel").onclick = null;
      resolve();
    };
    $("#modal-ok").onclick = close;
    $("#modal-cancel").onclick = close;
  });
}

async function invoicePdf(invoiceId, number) {
  await showPdf(`/books/${bid()}/invoices/${invoiceId}/pdf`, `faktura-${number || invoiceId}.pdf`);
}

async function creditNotePdf(invoiceId, eventId, number) {
  await showPdf(`/books/${bid()}/invoices/${invoiceId}/credit-notes/${eventId}/pdf`,
                `kreditfaktura-${number || eventId}.pdf`);
}

// Makulera (void) an unbooked invoice — keeps the number, nothing was booked.
async function makuleraInvoiceFlow(iv) {
  const f = await modal(
    `Makulera faktura ${iv.invoice_number}? Fakturan blir ogiltig men numret behålls (obruten serie).`,
    [], "Makulera");
  if (!f) return;
  await api("POST", `/books/${bid()}/invoices/${iv.id}/cancel`);
  toast(`Faktura ${iv.invoice_number} makulerad`);
  renderWorkspace();
}

// Register a customer payment (partial or full) against an invoice.
async function payInvoiceFlow(iv) {
  const f = await modal(`Betalning faktura ${iv.invoice_number}`, [
    { name: "amount", label: "Belopp (kr) — kvar att betala", value: toKr(iv.outstanding_ore) },
    { name: "date", label: "Betaldatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Bokför betalning");
  if (!f) return;
  const body = { date: f.date || null };
  if (f.amount) body.amount_ore = toOre(f.amount);
  const res = await api("POST", `/books/${bid()}/invoices/${iv.id}/pay`, body);
  toast(res.outstanding_ore > 0
    ? `Delbetalning bokförd — ${toKr(res.outstanding_ore)} kr kvar`
    : `Faktura ${iv.invoice_number} fullt betald`);
  renderWorkspace();
}

// Pay money back to the customer (partial or full).
async function refundInvoiceFlow(iv) {
  const owed = iv.outstanding_ore < 0 ? -iv.outstanding_ore : iv.paid_ore;
  const f = await modal(`Återbetalning faktura ${iv.invoice_number}`, [
    { name: "amount", label: "Belopp att återbetala (kr)", value: toKr(owed) },
    { name: "date", label: "Datum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Återbetala");
  if (!f || !f.amount) return;
  await api("POST", `/books/${bid()}/invoices/${iv.id}/refund`,
            { amount_ore: toOre(f.amount), date: f.date || null });
  toast(`Återbetalning bokförd`);
  renderWorkspace();
}

// Kreditera (partial or full) — reverses the income/moms slice against the receivable.
async function kreditInvoiceFlow(iv) {
  const billable = iv.customer_total_ore - iv.credited_ore;
  const f = await modal(`Kreditera faktura ${iv.invoice_number}`, [
    { name: "amount", label: "Belopp att kreditera (kr)", value: toKr(billable) },
    { name: "reason", label: "Orsak" },
    { name: "date", label: "Bokföringsdatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Kreditera");
  if (!f) return;
  const body = { reason: f.reason || null, date: f.date || null };
  if (f.amount) body.amount_ore = toOre(f.amount);
  const res = await api("POST", `/books/${bid()}/invoices/${iv.id}/credit`, body);
  toast(`Faktura ${iv.invoice_number} krediterad (ver ${res.ver_number})`);
  renderWorkspace();
}

// ---------------------------------------------------------------------------
// Small render helpers
// ---------------------------------------------------------------------------
function editBtn(onClick) {
  return el("button", { class: "btn small ghost", onclick: onClick }, "Ändra");
}
function wrap(label, input) { return el("div", {}, el("label", {}, label), input); }

// A <select> with a free-text filter above it (for long customer/article lists).
// options: [{value, label}]. Returns {element, select}; the select keeps the native
// API so callers can read .value / set .onchange as before.
function searchableSelect(options, selectedValue, placeholder) {
  const sel = el("select", {}, ...options.map((o) => el("option", { value: o.value }, o.label)));
  if (selectedValue != null) sel.value = String(selectedValue);
  const filter = el("input", { type: "search", placeholder: placeholder || "Sök…",
    style: "margin-bottom:4px" });
  filter.oninput = () => {
    const q = filter.value.trim().toLowerCase();
    let firstVisible = null;
    for (const opt of sel.options) {
      const match = !q || opt.textContent.toLowerCase().includes(q);
      opt.hidden = !match;
      if (match && firstVisible === null) firstVisible = opt;
    }
    // If the current selection was filtered out, jump to the first match so the
    // chosen value always reflects what's visible.
    if (q && sel.selectedOptions[0] && sel.selectedOptions[0].hidden && firstVisible) {
      sel.value = firstVisible.value;
      sel.dispatchEvent(new Event("change"));
    }
  };
  return { element: el("div", {}, filter, sel), select: sel };
}
function headerWithAdd(title, btn, onClick) {
  return el("div", { style: "display:flex;align-items:center;justify-content:space-between" },
    el("h2", { style: "margin:0" }, title),
    el("button", { class: "btn small", onclick: onClick }, btn));
}
function simpleTable(headers, rows) {
  if (rows.length === 0) return el("p", { class: "muted", style: "margin-top:14px" }, "Inget att visa ännu.");
  return el("table", { style: "margin-top:14px" },
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) =>
      el("td", {}, c instanceof Node ? c : String(c)))))));
}

// A search box over a list: re-renders a simpleTable filtered by a free-text query.
//   items   – the data array
//   matchFn – (item, lowercased-query) -> bool
//   rowFn   – (item) -> array of cells for simpleTable
function searchTable(placeholder, headers, items, matchFn, rowFn) {
  const search = el("input", { type: "search", placeholder,
    style: "margin-top:12px;max-width:340px" });
  const body = el("div", {});
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = q ? items.filter((it) => matchFn(it, q)) : items;
    body.innerHTML = "";
    if (q && shown.length === 0) {
      body.appendChild(el("p", { class: "muted", style: "margin-top:14px" },
        "Inga träffar."));
    } else {
      body.appendChild(simpleTable(headers, shown.map(rowFn)));
    }
  };
  search.oninput = draw;
  draw();
  return el("div", {}, search, body);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
(async function boot() {
  try {
    // Phone build: wait for the in-process Python backend (Pyodide) to finish
    // loading before the first call. Desktop has no BokYupReady and skips this.
    if (window.BokYupReady) {
      const v = $("#view");
      if (v) v.appendChild(el("p", { class: "muted" }, "Startar bokföringsmotorn…"));
      await window.BokYupReady;
    }
    state.appVersion = await api("GET", "/").then((r) => r.version).catch(() => "");
    await loadBooks();
    renderHome();
  } catch (e) {
    toast("Kunde inte nå servern: " + e.message, true);
  }
})();
