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
    el("h2", {}, "Dina böcker"),
    state.books.length === 0
      ? el("p", { class: "muted" }, "Inga böcker ännu. Skapa en med “+ Ny bok”.")
      : el("div", { class: "cards" }, state.books.map((b) =>
          el("div", { class: "card" },
            el("h4", {}, b.display_name),
            el("p", { class: "muted" }, b.last_opened ? ("Senast: " + b.last_opened.slice(0, 10)) : "Aldrig öppnad"),
            el("button", { class: "btn small", onclick: () => guard(() => openBook(b)) }, "Öppna"),
          ))),
  ));
}

// ---------------------------------------------------------------------------
// Workspace (active book)
// ---------------------------------------------------------------------------
const SECTIONS = [
  ["transactions", "Transaktioner"],
  ["record", "Bokför"],
  ["customers", "Kunder"],
  ["suppliers", "Leverantörer"],
  ["categories", "Kategorier"],
  ["reports", "Rapporter"],
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
      el("td", { class: "num" }, t.status === "pending"
        ? el("button", { class: "btn small", onclick: () => guard(() => payFlow(t.id)) }, "Bokför betalning")
        : ""),
    ));
    panel.appendChild(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Datum"), el("th", {}, "Typ"), el("th", {}, "Kategori"),
        el("th", {}, "Status"), el("th", { class: "num" }, "Verifikat"), el("th", {}, ""))),
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
    const amount = el("input", { type: "text", value: "0,00" });
    const rate = el("select", {},
      ...["25", "12", "6", "0", "momsfri", "ej_avdragsgill"].map((r) =>
        el("option", { value: r }, r === "momsfri" || r === "ej_avdragsgill" ? r : r + "%")));
    const date = el("input", { type: "date", value: new Date().toISOString().slice(0, 10) });
    const paidNow = el("select", {}, el("option", { value: "yes" }, "Ja, betald nu"), el("option", { value: "no" }, "Nej, väntar"));
    const rut = el("input", { type: "text", value: "0,00" });

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
    };
    kind.addEventListener("change", () => refreshFor(kind.value));
    refreshFor("income");

    const form = el("div", {},
      el("div", { class: "row" }, wrap("Typ", kind), wrap("Motpart", counter), wrap("Kategori", cat)),
      el("div", { class: "row" }, wrap("Belopp (kr, inkl. moms)", amount), wrap("Moms", rate), wrap("Datum", date)),
      el("div", { class: "row" }, wrap("Betald?", paidNow), wrap("RUT-belopp (kr, endast inkomst)", rut)),
      el("div", { style: "margin-top:14px" }, el("button", { class: "btn brand", onclick: () => guard(submit) }, "Bokför")),
    );
    panel.appendChild(form);

    async function submit() {
      const lines = [{ rate_code: rate.value, amount_ore: toOre(amount.value), inclusive: true }];
      const paid_date = paidNow.value === "yes" ? date.value : null;
      if (kind.value === "income") {
        await api("POST", `/books/${bid()}/incomes`, {
          customer_id: parseInt(counter.value, 10), category_id: parseInt(cat.value, 10),
          lines, trans_date: date.value, rut_amount_ore: toOre(rut.value) || 0, paid_date,
        });
      } else {
        await api("POST", `/books/${bid()}/expenses`, {
          supplier_id: counter.value ? parseInt(counter.value, 10) : null,
          category_id: parseInt(cat.value, 10), lines, trans_date: date.value, paid_date,
        });
      }
      toast("Bokfört");
      state.section = "transactions";
      renderWorkspace();
    }
  },

  // ----- customers -----
  async customers(panel) {
    const list = await api("GET", `/books/${bid()}/customers`);
    panel.appendChild(headerWithAdd("Kunder", "+ Ny kund", () => guard(addCustomerFlow)));
    panel.appendChild(simpleTable(
      ["Nr", "Typ", "Namn", "Org/Pers", "E-post"],
      list.map((c) => [c.kundnummer, c.type,
        c.company_name || `${c.first_name || ""} ${c.last_name || ""}`.trim(),
        c.org_nr || "", c.email || ""]),
    ));
  },

  // ----- suppliers -----
  async suppliers(panel) {
    const list = await api("GET", `/books/${bid()}/suppliers`);
    panel.appendChild(headerWithAdd("Leverantörer", "+ Ny leverantör", () => guard(addSupplierFlow)));
    panel.appendChild(simpleTable(
      ["Namn", "Moms", "Org.nr"],
      list.map((s) => [s.name, s.default_moms_rate, s.org_nr || ""]),
    ));
  },

  // ----- categories -----
  async categories(panel) {
    const list = await api("GET", `/books/${bid()}/categories`);
    panel.appendChild(headerWithAdd("Kategorier", "+ Ny kategori", () => guard(addCategoryFlow)));
    panel.appendChild(simpleTable(
      ["Namn", "Typ", "BAS-konto"],
      list.map((c) => [c.name, c.kind === "income" ? "Inkomst" : "Utgift", c.bas_konto]),
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
      el("div", { style: "align-self:flex-end" },
        el("button", { class: "btn ghost", onclick: () => guard(sie) }, "Exportera SIE")),
    ));
    panel.appendChild(out);

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
      const text = await api("GET", `/books/${bid()}/reports/sie`);
      const blob = new Blob([text], { type: "text/plain" });
      const a = el("a", { href: URL.createObjectURL(blob), download: "bokforing.se" });
      document.body.appendChild(a); a.click(); a.remove();
      toast("SIE-fil nedladdad");
    }
  },
};

// ---------------------------------------------------------------------------
// Flows used by sections
// ---------------------------------------------------------------------------
async function payFlow(txId) {
  const f = await modal("Bokför betalning", [
    { name: "payment_date", label: "Betaldatum", type: "date", value: new Date().toISOString().slice(0, 10) },
  ], "Bokför");
  if (!f) return;
  await api("POST", `/books/${bid()}/transaktioner/${txId}/pay`, { payment_date: f.payment_date });
  toast("Betalning bokförd");
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
    { name: "email", label: "E-post" },
  ], "Spara");
  if (!f) return;
  const body = { type: f.type };
  for (const k of ["first_name", "last_name", "personnummer", "company_name", "org_nr", "email"]) {
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
    { name: "name", label: "Namn" },
    { name: "kind", label: "Typ", type: "select", value: "expense",
      options: [{ value: "income", label: "Inkomst" }, { value: "expense", label: "Utgift" }] },
    { name: "bas_konto", label: "BAS-konto (t.ex. 3001 eller 5460)" },
  ], "Spara");
  if (!f || !f.name || !f.bas_konto) return;
  await api("POST", `/books/${bid()}/categories`, {
    name: f.name, kind: f.kind, bas_konto: parseInt(f.bas_konto, 10),
  });
  toast("Kategori sparad");
  renderWorkspace();
}

// ---------------------------------------------------------------------------
// Small render helpers
// ---------------------------------------------------------------------------
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
    await loadBooks();
    renderHome();
  } catch (e) {
    toast("Kunde inte nå servern: " + e.message, true);
  }
})();
