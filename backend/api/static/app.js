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
}

// ---------------------------------------------------------------------------
// Workspace (active book)
// ---------------------------------------------------------------------------
const SECTIONS = [
  ["transactions", "Transaktioner"],
  ["record", "Bokför"],
  ["invoices", "Fakturor"],
  ["articles", "Artiklar"],
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
    panel.appendChild(simpleTable(
      ["Artikelnr", "Beskrivning", "À-pris", "Moms", "Husavdrag", "Kategori", ""],
      list.map((a) => [a.article_number, a.description, toKr(a.unit_price_ore) + " kr",
        rateLabel(a.rate_code), a.reduction_type ? a.reduction_type.toUpperCase() : "—",
        a.category_name || el("span", { class: "muted" }, "Okategoriserad"),
        el("span", { style: "display:inline-flex;gap:4px" },
          editBtn(() => guard(() => editArticleFlow(a, incomeCats))),
          el("button", { class: "btn small ghost danger", onclick: () => guard(async () => {
            const f = await modal(`Ta bort artikel ${a.article_number}?`, [], "Ta bort");
            if (!f) return;
            await api("DELETE", `/books/${bid()}/articles/${a.id}`);
            toast("Artikel borttagen"); renderWorkspace();
          }) }, "Ta bort"))]),
    ));
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
      ["Nr", "Typ", "Namn", "Org/Pers", "E-post", ""],
      list,
      (c, q) => [String(c.kundnummer), cname(c), c.org_nr || "",
        c.email || "", c.phone || ""].join(" ").toLowerCase().includes(q),
      (c) => [c.kundnummer, c.type, cname(c), c.org_nr || "", c.email || "", actions(c)],
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
    const [claims, customers] = await Promise.all([
      api("GET", `/books/${bid()}/rut-claims`),
      api("GET", `/books/${bid()}/customers`),
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
  async skatt(panel) {
    const year = new Date().getFullYear();
    const fyStart = el("input", { type: "date", value: `${year}-01-01` });
    const fyEnd = el("input", { type: "date", value: `${year}-12-31` });
    const out = el("div", {});
    panel.appendChild(el("h2", {}, "Skatt att betala (uppskattning)"));
    panel.appendChild(el("p", { class: "muted" },
      "Enskild näringsidkare. En uppskattning av vad som bör sättas undan till "
      + "Skatteverket för räkenskapsåret — moms, egenavgifter och inkomstskatt — "
      + "beräknad ur din bokföring och satserna nedan. Detta är ett hjälpmedel, inte en "
      + "deklaration: grundavdrag, jobbskatteavdrag och de slutliga egenavgifterna stäms "
      + "av i INK1/slutskattebeskedet."));
    panel.appendChild(el("div", { class: "row" },
      wrap("Räkenskapsår fr.o.m.", fyStart), wrap("t.o.m.", fyEnd),
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn brand", onclick: () => guard(draw) }, "Visa"))));

    // --- editable tax rates (config; percentages stored as centi-percent) ---
    const cfg = await api("GET", `/books/${bid()}/tax-config`);
    const numIn = (v) => el("input", { type: "number", step: "0.01", value: String(v / 100), style: "width:110px" });
    const fEA = numIn(cfg.egenavgift_pct_centi), fSch = numIn(cfg.egenavgift_schablon_pct_centi);
    const fKom = numIn(cfg.kommunal_skattesats_pct_centi), fStat = numIn(cfg.statlig_skatt_pct_centi);
    const fBryt = numIn(cfg.statlig_brytpunkt_ore), fGrund = numIn(cfg.grundavdrag_ore);
    panel.appendChild(el("details", { style: "margin:14px 0" },
      el("summary", { style: "cursor:pointer;font-weight:600" }, "Skattesatser (redigerbara)"),
      el("p", { class: "muted" },
        "Satserna ändras årligen och varierar per kommun — kontrollera dina värden hos "
        + "Skatteverket. Ange procent (t.ex. 32,37) och kronor."),
      el("div", { class: "row" },
        wrap("Egenavgifter %", fEA), wrap("Schablonavdrag %", fSch),
        wrap("Kommunalskatt %", fKom), wrap("Statlig skatt %", fStat)),
      el("div", { class: "row" },
        wrap("Brytpunkt statlig (kr)", fBryt), wrap("Grundavdrag (kr)", fGrund),
        el("div", { style: "align-self:flex-end" },
          el("button", { class: "btn", onclick: () => guard(save) }, "Spara satser")))));
    panel.appendChild(out);

    const pctc = (inp) => Math.round(parseFloat(inp.value || "0") * 100);   // % -> centi-percent
    async function save() {
      await api("PUT", `/books/${bid()}/tax-config`, {
        egenavgift_pct_centi: pctc(fEA), egenavgift_schablon_pct_centi: pctc(fSch),
        kommunal_skattesats_pct_centi: pctc(fKom), statlig_skatt_pct_centi: pctc(fStat),
        statlig_brytpunkt_ore: pctc(fBryt), grundavdrag_ore: pctc(fGrund),
      });
      toast("Skattesatser sparade");
      await draw();
    }

    const pctLabel = (c) => c == null ? "" :
      (c / 100).toLocaleString("sv-SE", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + " %";
    const kv = (k, ore) => el("div", {}, el("div", { class: "k" }, k), el("div", { class: "v" }, toKr(ore) + " kr"));
    async function draw() {
      out.innerHTML = "";
      const r = await api("GET", `/books/${bid()}/reports/tax?start=${fyStart.value}&end=${fyEnd.value}`);
      out.appendChild(el("div", { class: "box", style: "margin-top:14px" },
        el("div", { class: "row" },
          kv("Årets överskott (resultat)", r.overskott_ore),
          kv("− Schablonavdrag egenavgifter", r.schablonavdrag_ore),
          kv("= Taxerad näringsinkomst", r.taxerad_inkomst_ore),
          kv("− Grundavdrag", r.grundavdrag_ore),
          kv("= Beskattningsbar inkomst", r.beskattningsbar_inkomst_ore))));
      const row = (label, ore, note, bold) => el("tr", { style: bold ? "border-top:2px solid var(--border,#ccc)" : "" },
        el("td", { style: bold ? "font-weight:700" : "" }, label),
        el("td", { class: "num", style: "font-variant-numeric:tabular-nums" + (bold ? ";font-weight:700" : "") }, toKr(ore) + " kr"),
        el("td", { class: "muted", style: "font-size:12px" }, note || ""));
      const lines = r.lines.map((l) => row(
        l.label + (l.pct_centi != null ? "  (" + pctLabel(l.pct_centi)
          + (l.underlag_ore != null ? " på " + toKr(l.underlag_ore) + " kr" : "") + ")" : ""),
        l.amount_ore, l.note));
      out.appendChild(el("table", { style: "width:100%;margin-top:14px" },
        el("thead", {}, el("tr", {}, el("th", {}, "Skatt"), el("th", { class: "num" }, "Belopp"), el("th", {}, ""))),
        el("tbody", {}, lines,
          row("Att sätta undan totalt", r.total_ore, "moms + egenavgifter + inkomstskatt", true))));
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
    const [list, drafts, customers] = await Promise.all([
      api("GET", `/books/${bid()}/invoices`),
      api("GET", `/books/${bid()}/invoice-drafts`),
      api("GET", `/books/${bid()}/customers`),
    ]);
    const custName = Object.fromEntries(customers.map((c) =>
      [c.kundnummer, c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim()]));
    panel.appendChild(headerWithAdd("Fakturor", "+ Ny faktura", () => guard(() => invoiceForm(panel))));

    // Drafts: unissued invoices saved to continue later.
    if (drafts.length) {
      panel.appendChild(el("h3", { style: "margin-top:14px" }, "Utkast"));
      panel.appendChild(simpleTable(
        ["Sparat", "Rader", "Summa", ""],
        drafts.map((d) => [d.updated_at ? d.updated_at.slice(0, 16).replace("T", " ") : "",
          d.line_count, toKr(d.total_ore || 0) + " kr",
          el("span", { style: "display:inline-flex;gap:4px" },
            el("button", { class: "btn small", onclick: () => guard(async () => {
              const full = await api("GET", `/books/${bid()}/invoice-drafts/${d.id}`);
              invoiceForm(panel, full);
            }) }, "Fortsätt"),
            el("button", { class: "btn small ghost danger", onclick: () => guard(async () => {
              await api("DELETE", `/books/${bid()}/invoice-drafts/${d.id}`);
              toast("Utkast borttaget"); renderWorkspace();
            }) }, "Ta bort"))]),
      ));
    }

    if (list.length === 0) {
      panel.appendChild(el("p", { class: "muted", style: "margin-top:14px" },
        "Inga fakturor ännu. Ställ in företagsuppgifter och betalsätt under Inställningar."));
      return;
    }
    panel.appendChild(el("h3", { style: "margin-top:18px" }, "Fakturor"));
    const STATE = {
      paid: ["paid", "Betald"], pending: ["pending", "Obetald"],
      partial: ["pending", "Delbetald"],
      cancelled: ["", "Makulerad"], credited: ["", "Krediterad"],
    };
    const act = (label, cls, fn) => el("button",
      { class: "btn small " + cls, style: "margin-left:4px", onclick: () => guard(fn) }, label);
    const rowFor = (iv) => {
      const [cls, label] = STATE[iv.state] || STATE.pending;
      // RUT and ROT invoices both carry a husavdrag receivable -> full Skatteverket flow.
      const isRut = (iv.rut_total_ore > 0) || (iv.rot_total_ore > 0);
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
      return el("tr", {},
        el("td", { class: "num" }, String(iv.invoice_number)),
        el("td", {}, custName[iv.customer_id] || ("Kund " + iv.customer_id)),
        el("td", {}, iv.invoice_date),
        el("td", {}, iv.due_date),
        el("td", { class: "num" }, toKr(iv.inc_moms_ore) + " kr"),
        el("td", { class: "num", title: owed ? "Att återbetala till kund" : "Kvar att betala" }, kvar),
        el("td", {}, el("span", { class: "pill " + cls }, label),
          owed ? el("span", { class: "pill", style: "margin-left:4px" }, "återbet.") : null),
        actions);
    };
    const ivMatch = (iv, q) => [String(iv.invoice_number), custName[iv.customer_id] || "",
      iv.invoice_date || "", iv.due_date || ""].join(" ").toLowerCase().includes(q);
    const search = el("input", { type: "search", placeholder: "Sök faktura (nr, kund, datum)…",
      style: "margin-top:12px;max-width:340px" });
    const tbody = el("tbody", {});
    const drawRows = () => {
      const q = search.value.trim().toLowerCase();
      const shown = q ? list.filter((iv) => ivMatch(iv, q)) : list;
      tbody.innerHTML = "";
      if (q && shown.length === 0) {
        tbody.appendChild(el("tr", {}, el("td", { colspan: "8", class: "muted" }, "Inga träffar.")));
      } else {
        for (const iv of shown) tbody.appendChild(rowFor(iv));
      }
    };
    search.oninput = drawRows;
    drawRows();
    panel.appendChild(search);
    panel.appendChild(el("table", { style: "margin-top:14px" },
      el("thead", {}, el("tr", {},
        el("th", { class: "num" }, "Nr"), el("th", {}, "Kund"), el("th", {}, "Datum"),
        el("th", {}, "Förfaller"), el("th", { class: "num" }, "Summa"),
        el("th", { class: "num" }, "Kvar"), el("th", {}, "Status"), el("th", {}, ""))),
      tbody));
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
  const f = await modal("Bokför Skatteverkets utbetalning", [
    { name: "payment_date", label: "Utbetalningsdatum", type: "date", value: new Date().toISOString().slice(0, 10) },
    { name: "received", label: "Mottaget belopp (kr)",
      value: claimedOre != null ? toKr(claimedOre) : "" },
    { name: "reference", label: "RUT/ROT-begäran (namn, t.ex. RUT1)", value: "" },
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
        ...incomeCats.map((c) => ({ value: String(c.id), label: c.name }))] },
  ];
}

async function addArticleFlow(incomeCats) {
  const f = await modal("Ny artikel", [
    { name: "prefix", label: "Artikelnr-prefix (4 siffror)", value: "1000" },
    ...articleFields(null, incomeCats),
  ], "Skapa");
  if (!f || !f.description) return;
  await api("POST", `/books/${bid()}/articles`, {
    prefix: (f.prefix || "").trim(), description: f.description,
    unit_price_ore: toOre(f.unit_price_kr), unit: f.unit || null,
    rate_code: f.rate_code, reduction_type: f.reduction_type || null,
    category_id: f.category_id ? parseInt(f.category_id, 10) : null,
  });
  toast("Artikel skapad");
  renderWorkspace();
}

async function editArticleFlow(a, incomeCats) {
  const f = await modal(`Ändra artikel ${a.article_number}`, [
    { name: "article_number", label: "Artikelnummer", value: a.article_number },
    ...articleFields(a, incomeCats),
  ], "Spara");
  if (!f || !f.description) return;
  await api("PATCH", `/books/${bid()}/articles/${a.id}`, {
    article_number: f.article_number || null, description: f.description,
    unit_price_ore: toOre(f.unit_price_kr), unit: f.unit || null,
    rate_code: f.rate_code, reduction_type: f.reduction_type || null,
    category_id: f.category_id ? parseInt(f.category_id, 10) : null,
  });
  toast("Artikel uppdaterad");
  renderWorkspace();
}

async function addCategoryFlow() {
  const f = await modal("Ny kategori", [
    { name: "name", label: "Namn (t.ex. Försäljning IT-tjänster)" },
    { name: "kind", label: "Typ", type: "select", value: "expense",
      options: [{ value: "income", label: "Inkomst" }, { value: "expense", label: "Utgift" }] },
    { name: "bas_konto", label: "BAS-konto (t.ex. 3001 eller 5460)" },
    { name: "default_rate_code", label: "Standardmoms", type: "select", value: "25",
      options: RATE_OPTIONS.map((r) => ({ value: r, label: rateLabel(r) })) },
  ], "Spara");
  if (!f || !f.name || !f.bas_konto) return;
  await api("POST", `/books/${bid()}/categories`, {
    name: f.name, kind: f.kind, bas_konto: parseInt(f.bas_konto, 10),
    default_rate_code: f.default_rate_code || null,
  });
  toast("Kategori sparad");
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
    const dest = `/bokyup-data/${name.replace(/\W+/g, "_")}_${Date.now()}.db`;
    const rec = await api("POST", "/books/import",
                          { bundle_path: picked.fsPath, dest_db_path: dest, display_name: name });
    await loadBooks();
    renderHome();
    toast(`Återställd: ${rec.display_name} — lås upp med dess lösenord`);
    return;
  }
  const f = await modal("Återställ från säkerhetskopia", [
    { name: "bundle_path", label: "Sökväg till .buyn-fil" },
    { name: "dest_db_path", label: "Spara databasen som (t.ex. C:\\bokforing\\restored.db)" },
    { name: "display_name", label: "Namn på boken (valfritt)" },
  ], "Återställ");
  if (!f || !f.bundle_path || !f.dest_db_path) return;
  const rec = await api("POST", "/books/import", {
    bundle_path: f.bundle_path, dest_db_path: f.dest_db_path,
    display_name: f.display_name || null,
  });
  await loadBooks();
  renderHome();
  toast(`Återställd: ${rec.display_name} — lås upp med dess lösenord`);
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
  const lines = lineItemsEditor(incomeCats, () => recips.recompute(), dp.lines || [], articles);
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
  panel.appendChild(el("div", { class: "row", style: "margin-top:14px" },
    wrap("Betalningsvillkor", terms), wrap("Er referens", yourRef), wrap("Notering", note)));
  panel.appendChild(el("div", { style: "margin-top:16px" },
    el("button", { class: "btn brand", onclick: () => guard(submit) }, "Skapa faktura"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => guard(saveDraft) }, "Spara utkast"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => { state.section = "invoices"; renderWorkspace(); } }, "Avbryt")));

  await recips.reloadPeople();

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

function lineItemsEditor(incomeCats, onChange, initialLines, articles) {
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
    const desc = el("input", { type: "text", placeholder: "Beskrivning", value: v.description || "" });
    const cat = el("select", {},
      el("option", { value: "" }, "(standard)"),
      ...cats.map((c) => el("option", { value: c.id }, c.name)));
    if (v.category_id) cat.value = String(v.category_id);
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
      if (!a) { articleId = null; return; }
      articleId = a.id;
      desc.value = a.description || "";
      price.value = a.unit_price_ore != null ? toKr(a.unit_price_ore) : price.value;
      if (a.unit) unit.value = a.unit;
      if (a.rate_code) rate.value = a.rate_code;
      red.value = a.reduction_type || "";
      cat.value = a.category_id ? String(a.category_id) : "";
      fire();
    };
    // Typing a fresh description detaches the row from a picked article.
    desc.oninput = () => { articleId = null; pick.value = ""; };
    // Save this row's current values to the catalog as a new article.
    const saveBtn = el("button", { class: "btn small ghost", type: "button", title: "Spara som artikel",
      onclick: () => guard(() => saveRowAsArticle(row)) }, "★");
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end;flex-wrap:wrap" },
      wrap("Artikel", pick), wrap("Beskrivning", desc), wrap("Kategori (BAS)", cat),
      wrap("Antal", qty), wrap("Enhet", unit), wrap("À-pris ex moms", price),
      wrap("% rabatt", disc), wrap("Moms", rate), wrap("Husavdrag", red), saveBtn,
      el("button", { class: "btn small ghost", onclick: (e) => { e.target.closest(".row").remove(); fire(); } }, "✕"));
    row._desc = desc; row._cat = cat; row._unit = unit; row._price = price; row._disc = disc; row._rate = rate; row._red = red;
    row._get = () => ({
      description: desc.value.trim(),
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      quantity_centi: Math.round(parseFloat((qty.value || "0").replace(",", ".")) * 100),
      unit: unit.value || null, unit_price_ore: toOre(price.value),
      discount_pct_centi: Math.round(parseFloat((disc.value || "0").replace(",", ".")) * 100) || 0,
      rate_code: rate.value, reduction_type: red.value || null, article_id: articleId,
    });
    rowsBox.appendChild(row);
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
  return { element, get: () => [...rowsBox.children].map((r) => r._get()).filter((l) => l.description) };
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
    await loadBooks();
    renderHome();
  } catch (e) {
    toast("Kunde inte nå servern: " + e.message, true);
  }
})();
