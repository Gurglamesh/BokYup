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
const toOre = (kr) => Math.round(parseFloat(String(kr).replace(",", ".")) * 100);
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
      for (const [k, v] of Object.entries(inputs)) out[k] = v.value;
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
  ["customers", "Kunder"],
  ["suppliers", "Leverantörer"],
  ["categories", "BAS-konton"],
  ["rut", "RUT"],
  ["verifikat", "Verifikat"],
  ["reports", "Rapporter"],
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

  // ----- customers -----
  async customers(panel) {
    const list = await api("GET", `/books/${bid()}/customers`);
    panel.appendChild(headerWithAdd("Kunder", "+ Ny kund", () => guard(addCustomerFlow)));
    panel.appendChild(simpleTable(
      ["Nr", "Typ", "Namn", "Org/Pers", "E-post", ""],
      list.map((c) => [c.kundnummer, c.type,
        c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim(),
        c.org_nr || "", c.email || "",
        editBtn(() => guard(() => editCustomerFlow(c.kundnummer)))]),
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
    const rows = claims.map((c) => el("tr", {},
      el("td", {}, String(c.id)),
      el("td", {}, custName[c.customer_id] || ("Kund " + c.customer_id)),
      el("td", { class: "num" }, toKr(c.rut_amount_ore) + " kr"),
      el("td", {}, el("span", { class: "pill " + (c.state === "skatteverket_paid" ? "paid" : c.state === "customer_paid" ? "" : "pending") },
        STATE_LABEL[c.state] || c.state)),
      el("td", {}, c.customer_payment_date || "—"),
      el("td", {}, c.skatteverket_payment_date || "—"),
      el("td", { class: "num" }, c.state === "customer_paid"
        ? el("button", { class: "btn small", onclick: () => guard(() => rutSkvPayFlow(c.id)) }, "Bokför SKV-utbetalning")
        : ""),
    ));
    panel.appendChild(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Nr"), el("th", {}, "Kund"), el("th", { class: "num" }, "Belopp"),
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
    panel.appendChild(el("p", { class: "muted" }, "T.ex. Swish, Bankgiro, IBAN — namn + nummer/länk."));
    panel.appendChild(simpleTable(["Betalsätt", "Nummer/länk", "Aktiv"],
      methods.map((m) => [m.label, m.value, m.active ? "Ja" : "Nej"])));
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
    const list = await api("GET", `/books/${bid()}/invoices`);
    panel.appendChild(headerWithAdd("Fakturor", "+ Ny faktura", () => guard(() => invoiceForm(panel))));
    if (list.length === 0) {
      panel.appendChild(el("p", { class: "muted", style: "margin-top:14px" },
        "Inga fakturor ännu. Ställ in företagsuppgifter och betalsätt under Inställningar."));
      return;
    }
    const STATE = {
      paid: ["paid", "Betald"], pending: ["pending", "Obetald"],
      partial: ["pending", "Delbetald"],
      cancelled: ["", "Makulerad"], credited: ["", "Krediterad"],
    };
    const act = (label, cls, fn) => el("button",
      { class: "btn small " + cls, style: "margin-left:4px", onclick: () => guard(fn) }, label);
    const rows = list.map((iv) => {
      const [cls, label] = STATE[iv.state] || STATE.pending;
      const isRut = iv.rut_total_ore > 0;
      const owed = iv.outstanding_ore < 0 ? -iv.outstanding_ore : 0;   // we owe the customer
      const actions = el("td", { class: "num" },
        el("button", { class: "btn small ghost", onclick: () => guard(() => invoicePdf(iv.id, iv.invoice_number)) }, "PDF"));
      for (const cn of iv.credit_notes || []) {
        actions.appendChild(el("button", { class: "btn small ghost", style: "margin-left:4px",
          title: `Kreditfaktura nr ${cn.credit_note_number}`,
          onclick: () => guard(() => creditNotePdf(iv.id, cn.id, cn.credit_note_number)) },
          `Kreditnota ${cn.credit_note_number}`));
      }
      if (isRut) {
        // RUT invoices keep the full payment + Skatteverket flow (RUT-tab)
        if (iv.state === "pending") {
          actions.appendChild(act("Bokför betalning", "", () => payFlow(iv.transaktion_id)));
          actions.appendChild(act("Makulera", "ghost danger", () => makuleraInvoiceFlow(iv)));
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
        el("td", {}, iv.invoice_date),
        el("td", {}, iv.due_date),
        el("td", { class: "num" }, toKr(iv.inc_moms_ore) + " kr"),
        el("td", { class: "num", title: owed ? "Att återbetala till kund" : "Kvar att betala" }, kvar),
        el("td", {}, el("span", { class: "pill " + cls }, label),
          owed ? el("span", { class: "pill", style: "margin-left:4px" }, "återbet.") : null),
        actions);
    });
    panel.appendChild(el("table", { style: "margin-top:14px" },
      el("thead", {}, el("tr", {},
        el("th", { class: "num" }, "Nr"), el("th", {}, "Datum"), el("th", {}, "Förfaller"),
        el("th", { class: "num" }, "Summa"), el("th", { class: "num" }, "Kvar"),
        el("th", {}, "Status"), el("th", {}, ""))),
      el("tbody", {}, rows)));
  },
};

// ---------------------------------------------------------------------------
// Moms-lines editor — one row per momssats (a receipt can mix 6/12/25 %)
// ---------------------------------------------------------------------------
const RATE_OPTIONS = ["25", "12", "6", "0", "momsfri", "ej_avdragsgill"];
function rateLabel(r) {
  if (!r) return "—";
  return (r === "momsfri" || r === "ej_avdragsgill") ? r : r + "%";
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

async function rutSkvPayFlow(claimId) {
  const f = await modal("Bokför Skatteverkets utbetalning", [
    { name: "payment_date", label: "Utbetalningsdatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Bokför");
  if (!f) return;
  await api("POST", `/books/${bid()}/rut/${claimId}/skatteverket-payment`, { payment_date: f.payment_date });
  toast("Husavdrag bokfört");
  renderWorkspace();
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
async function invoiceForm(panel) {
  const [customers, cats] = await Promise.all([
    api("GET", `/books/${bid()}/customers`),
    api("GET", `/books/${bid()}/categories`),
  ]);
  const incomeCats = cats.filter((c) => c.kind === "income");
  if (customers.length === 0 || incomeCats.length === 0) {
    toast("Lägg till minst en kund och en inkomstkategori först", true);
    return;
  }
  panel.innerHTML = "";
  panel.appendChild(el("h2", {}, "Ny faktura"));

  const customer = el("select", {}, ...customers.map((c) => el("option", { value: c.kundnummer },
    c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim() || ("Kund " + c.kundnummer))));
  // Optional default category for rows that don't set their own (blank = pick per row).
  const cat = el("select", {},
    el("option", { value: "" }, "— välj per rad —"),
    ...incomeCats.map((c) => el("option", { value: c.id }, c.name)));
  const today = new Date().toISOString().slice(0, 10);
  const due = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
  const invDate = el("input", { type: "date", value: today });
  const dueDate = el("input", { type: "date", value: due });
  const delivery = el("input", { type: "date", value: "" });
  const terms = el("input", { type: "text", value: "30 dagar netto" });
  const yourRef = el("input", { type: "text" });
  const note = el("input", { type: "text" });
  const lines = lineItemsEditor(incomeCats);
  const recips = recipientsEditor();

  panel.appendChild(el("div", { class: "row" },
    wrap("Kund", customer), wrap("Standardkategori (BAS, valfri)", cat)));
  panel.appendChild(el("div", { class: "row" },
    wrap("Fakturadatum", invDate), wrap("Förfallodatum", dueDate),
    wrap("Leveransdatum (valfritt)", delivery)));
  panel.appendChild(el("h3", { style: "margin-top:18px" }, "Artikelrader"));
  panel.appendChild(el("p", { class: "muted" },
    "Varje rad bokförs på sin egen kategori (BAS-konto). Lämna radens kategori tom för "
    + "att använda standardkategorin ovan."));
  panel.appendChild(lines.element);
  panel.appendChild(el("h3", { style: "margin-top:18px" }, "RUT-mottagare (hushåll, valfritt)"));
  panel.appendChild(el("p", { class: "muted" },
    "Lägg till en rad per person som delar på RUT-avdraget — namn, personnummer och "
    + "personens del av skattereduktionen."));
  panel.appendChild(recips.element);
  panel.appendChild(el("div", { class: "row", style: "margin-top:14px" },
    wrap("Betalningsvillkor", terms), wrap("Er referens", yourRef), wrap("Notering", note)));
  panel.appendChild(el("div", { style: "margin-top:16px" },
    el("button", { class: "btn brand", onclick: () => guard(submit) }, "Skapa faktura"),
    el("button", { class: "btn ghost", style: "margin-left:8px", onclick: () => { state.section = "invoices"; renderWorkspace(); } }, "Avbryt")));

  async function submit() {
    const lineData = lines.get();
    if (lineData.length === 0) { toast("Lägg till minst en artikelrad", true); return; }
    const body = {
      customer_id: parseInt(customer.value, 10),
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      invoice_date: invDate.value, due_date: dueDate.value,
      delivery_date: delivery.value || null, payment_terms: terms.value || null,
      your_reference: yourRef.value || null, note: note.value || null,
      lines: lineData, recipients: recips.get(),
    };
    const res = await api("POST", `/books/${bid()}/invoices`, body);
    toast(`Faktura ${res.invoice_number} skapad`);
    state.section = "invoices";
    renderWorkspace();
    invoicePdf(res.invoice_id, res.invoice_number);
  }
}

function lineItemsEditor(incomeCats) {
  const rowsBox = el("div", {});
  const cats = incomeCats || [];
  function addRow() {
    const desc = el("input", { type: "text", placeholder: "Beskrivning" });
    const cat = el("select", {},
      el("option", { value: "" }, "(standard)"),
      ...cats.map((c) => el("option", { value: c.id }, c.name)));
    const qty = el("input", { type: "text", value: "1", style: "width:64px" });
    const unit = el("input", { type: "text", value: "st", style: "width:54px" });
    const price = el("input", { type: "text", value: "0,00", style: "width:96px" });
    const rate = el("select", {}, ...RATE_OPTIONS.map((r) => el("option", { value: r }, rateLabel(r))));
    const rut = el("input", { type: "checkbox" });
    // Picking a category pre-fills the moms rate from that category's default.
    cat.onchange = () => {
      const c = cats.find((x) => String(x.id) === cat.value);
      if (c && c.default_rate_code) rate.value = c.default_rate_code;
    };
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end" },
      wrap("Beskrivning", desc), wrap("Kategori (BAS)", cat), wrap("Antal", qty), wrap("Enhet", unit),
      wrap("À-pris ex moms", price), wrap("Moms", rate), wrap("RUT", rut),
      el("button", { class: "btn small ghost", onclick: (e) => e.target.closest(".row").remove() }, "✕"));
    row._get = () => ({
      description: desc.value.trim(),
      category_id: cat.value ? parseInt(cat.value, 10) : null,
      quantity_centi: Math.round(parseFloat((qty.value || "0").replace(",", ".")) * 100),
      unit: unit.value || null, unit_price_ore: toOre(price.value),
      rate_code: rate.value, rut_eligible: rut.checked,
    });
    rowsBox.appendChild(row);
  }
  addRow();
  const element = el("div", {}, rowsBox,
    el("button", { class: "btn small ghost", onclick: () => addRow() }, "+ Rad"));
  return { element, get: () => [...rowsBox.children].map((r) => r._get()).filter((l) => l.description) };
}

function recipientsEditor() {
  const rowsBox = el("div", {});
  function addRow() {
    const fn = el("input", { type: "text", placeholder: "Förnamn" });
    const ln = el("input", { type: "text", placeholder: "Efternamn" });
    const pnr = el("input", { type: "text", placeholder: "ÅÅMMDD-XXXX" });
    const amt = el("input", { type: "text", value: "0,00", style: "width:96px" });
    const row = el("div", { class: "row", style: "gap:6px;align-items:flex-end" },
      wrap("Förnamn", fn), wrap("Efternamn", ln), wrap("Personnummer", pnr),
      wrap("Skattereduktion (kr)", amt),
      el("button", { class: "btn small ghost", onclick: (e) => e.target.closest(".row").remove() }, "✕"));
    row._get = () => ({
      first_name: fn.value.trim(), last_name: ln.value.trim(),
      personnummer: pnr.value.trim(), rut_amount_ore: toOre(amt.value),
    });
    rowsBox.appendChild(row);
  }
  const element = el("div", {}, rowsBox,
    el("button", { class: "btn small ghost", onclick: () => addRow() }, "+ Mottagare"));
  return {
    element,
    get: () => [...rowsBox.children].map((r) => r._get()).filter((x) => x.first_name && x.personnummer),
  };
}

// Download/preview an invoice PDF (HTTP link on desktop, Blob from base64 on phone).
async function invoicePdf(invoiceId, number) {
  const path = `/books/${bid()}/invoices/${invoiceId}/pdf`;
  const fname = `faktura-${number || invoiceId}.pdf`;
  if (window.__BOKYUP_NATIVE__) {
    const r = await api("GET", path);                 // { raw, base64, media_type }
    const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: r.media_type }));
    const a = el("a", { href: url, download: fname }); document.body.appendChild(a); a.click(); a.remove();
    return;
  }
  const a = el("a", { href: path, download: fname, target: "_blank" });
  document.body.appendChild(a); a.click(); a.remove();
}

// Download/preview a numbered kreditfaktura (credit note) PDF.
async function creditNotePdf(invoiceId, eventId, number) {
  const path = `/books/${bid()}/invoices/${invoiceId}/credit-notes/${eventId}/pdf`;
  const fname = `kreditfaktura-${number || eventId}.pdf`;
  if (window.__BOKYUP_NATIVE__) {
    const r = await api("GET", path);
    const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: r.media_type }));
    const a = el("a", { href: url, download: fname }); document.body.appendChild(a); a.click(); a.remove();
    return;
  }
  const a = el("a", { href: path, download: fname, target: "_blank" });
  document.body.appendChild(a); a.click(); a.remove();
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
