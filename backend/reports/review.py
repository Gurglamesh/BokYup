"""
review.py — the whole bookkeeping as ONE readable Markdown file, written to be handed
to a reviewer (a revisor, or an AI) with the question "är något fel här?".

This is deliberately NOT a machine format. SIE (`sie.py`) is what a revisor imports and
CSV (`csv_export.py`) is what you pivot in Excel; both assume the reader already knows
the book. A reviewer who has never seen it needs the *context* to judge: which method the
book is kept on, what the book's own konton are called, which entries are egenupprättade,
where underlag is missing — and the same numbers from several angles (saldolista,
resultat, moms, årsbokslut) so an error shows up as a contradiction.

It also runs a handful of **mechanical checks** first. Verifikationsnummer gaps, postings
that do not sum to zero, missing underlag: facts that are cheap to compute here and
unreliable to eyeball in prose. They direct attention; they do not replace the review.

Privacy: the file is meant to leave the machine, so it carries the ledger and nothing
else. No personnummer (they are encrypted and never decrypted into this), no addresses,
e-mail or phone numbers, and customer names are replaced with "[Kund N]" unless the
user explicitly asks for them. Supplier names are kept — the counterparty is usually the whole
reason a konto is right or wrong.
"""

from __future__ import annotations

import datetime

# A verifikation recorded this long after the business event is worth a look (BFL 5 kap.
# requires bookkeeping without delay). Informational only — a first-year book entered in
# one sitting will trip it for everything, which the note says.
LATE_BOOKING_DAYS = 60
_MAX_LIST = 25          # how many ids a single check lists before it says "…"


def _kr(ore: int | None) -> str:
    """Integer ören -> '1234,56'. Decimal comma, no thousands separator."""
    return f"{(ore or 0) / 100:.2f}".replace(".", ",")


def _dr(ore: int) -> str:
    return _kr(ore) if ore > 0 else ""


def _cr(ore: int) -> str:
    return _kr(-ore) if ore < 0 else ""


def _cell(s) -> str:
    """Markdown table cell: never break the table on a pipe or a newline."""
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def _table(rows: list[list], headers: list[str], align: str = "") -> list[str]:
    """A GitHub-flavoured Markdown table. `align` is one char per column: 'r' or 'l'."""
    sep = []
    for i in range(len(headers)):
        a = align[i] if i < len(align) else "l"
        sep.append("---:" if a == "r" else "---")
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |",
           "|" + "|".join(sep) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    out.append("")
    return out


def _konto_kind(konto: int) -> str:
    if konto < 2000:
        return "tillgång"
    if konto < 3000:
        return "eget kapital / skuld"
    if konto < 4000:
        return "intäkt"
    if konto < 8000:
        return "kostnad"
    return "finansiell post"


def _masker(pairs):
    """Literal name -> tag replacements, longest first (so a full name wins over a
    first name). Applied to every free-text field that reaches the file."""
    if not pairs:
        return lambda s: s

    def mask(s):
        if not s:
            return s
        for name, tag in pairs:
            if name in s:
                s = s.replace(name, tag)
        return s
    return mask


# ---------------------------------------------------------------------------
# Mechanical checks
# ---------------------------------------------------------------------------

def _vnum(v: dict) -> str:
    """Display name of a verifikation. An unnumbered one is never produced by the app —
    the number is assigned at posting — so say so plainly instead of printing "ANone"."""
    if v.get("ver_number") is None:
        return f"(onumrerad, id {v['id']})"
    return f"{v['series']}{v['ver_number']}"


def _ids(items: list[str]) -> str:
    if len(items) <= _MAX_LIST:
        return ", ".join(items)
    return ", ".join(items[:_MAX_LIST]) + f" … (+{len(items) - _MAX_LIST} fler)"


def kontroller(data: dict) -> list[dict]:
    """
    [{level, text}] where level is 'ok' | 'flag' | 'info'. Pure function of `data`,
    so each check is testable on its own.
    """
    vers = data["verifikationer"]
    out: list[dict] = []
    vnum = {v["id"]: _vnum(v) for v in vers}

    # 1. Every verifikation must balance. The engine asserts this when posting, so a hit
    #    here means the database has been changed outside the app.
    bad = [(vnum[v["id"]], sum(p["amount_ore"] for p in v["postings"])) for v in vers
           if sum(p["amount_ore"] for p in v["postings"]) != 0]
    if bad:
        out.append({"level": "flag", "text":
                    "ALLVARLIGT: verifikationer där debet ≠ kredit: "
                    + _ids([f"{n} (diff {_kr(d)})" for n, d in bad])})
    else:
        out.append({"level": "ok", "text":
                    f"Alla {len(vers)} verifikationer balanserar (debet = kredit)."})

    # 2. Unbroken number series. Only meaningful over the whole book — a date filter cuts
    #    the series at both ends, and the holes that leaves are not holes in the books.
    if data["period"].get("filtered"):
        out.append({"level": "info", "text":
                    "Nummerserien kontrolleras inte: exporten är filtrerad på datum, så "
                    "serien är beskuren i båda ändar."})
    else:
        for series in sorted({v["series"] for v in vers}):
            nums = sorted(v["ver_number"] for v in vers
                          if v["series"] == series and v["ver_number"] is not None)
            if not nums:
                continue
            gaps = [n for n in range(nums[0], nums[-1] + 1) if n not in set(nums)]
            dupes = sorted({n for n in nums if nums.count(n) > 1})
            if gaps:
                out.append({"level": "flag", "text":
                            f"Serie {series} har hopp i nummerserien: "
                            + _ids([f"{series}{n}" for n in gaps])})
            if dupes:
                out.append({"level": "flag", "text":
                            f"Serie {series} har dubbla nummer: "
                            + _ids([f"{series}{n}" for n in dupes])})
            if not gaps and not dupes:
                out.append({"level": "ok", "text":
                            f"Serie {series}: {series}{nums[0]}–{series}{nums[-1]}, "
                            f"{len(nums)} verifikationer, inga hopp och inga dubbletter."})

    # 3. Underlag. A rättelse points at the entry it corrects and an egenupprättad entry
    #    carries its motivering as underlag, so neither is expected to have a file.
    missing = [vnum[v["id"]] for v in vers
               if not v.get("underlag") and not v.get("egenupprattad")
               and not v.get("rattelse_of")]
    if missing:
        out.append({"level": "flag", "text":
                    f"{len(missing)} verifikationer saknar bifogat underlag "
                    f"(kvitto/faktura): " + _ids(missing)})
    else:
        out.append({"level": "ok", "text":
                    "Alla verifikationer som ska ha underlag har ett bifogat."})

    # 4. An egenupprättad verifikation without a motivering has no underlag at all.
    nomot = [vnum[v["id"]] for v in vers
             if v.get("egenupprattad") and not (v.get("motivering") or "").strip()]
    if nomot:
        out.append({"level": "flag", "text":
                    "Egenupprättade verifikationer utan motivering (motiveringen ÄR "
                    "underlaget): " + _ids(nomot)})

    # 5. Unbooked business events — they are not in the books yet, so no report shows them.
    pend = [t for t in data["transaktioner"] if t["status"] != "paid"]
    if pend:
        tot = sum(sum(l["inc_moms_ore"] for l in t["lines"]) for t in pend)
        n = len(pend)
        vad = ("1 obokförd affärshändelse" if n == 1
               else f"{n} obokförda affärshändelser")
        out.append({"level": "info", "text":
                    f"{vad} (väntar på betalning), totalt {_kr(tot)} kr. "
                    + ("Den ingår" if n == 1 else "De ingår")
                    + " INTE i saldon eller rapporter nedan."})

    # 6. Saldolista must net to zero for the same reason as (1) — stated separately
    #    because it is the number a reviewer looks for first.
    tot_d = sum(a["debit_ore"] for a in data["accounts"])
    tot_k = sum(a["credit_ore"] for a in data["accounts"])
    if tot_d != tot_k:
        out.append({"level": "flag", "text":
                    f"Saldolistan går inte jämnt ut: debet {_kr(tot_d)} mot kredit "
                    f"{_kr(tot_k)} (diff {_kr(tot_d - tot_k)})."})

    # 7. A negative bank saldo is the classic sign of a missed deposit or a payment booked
    #    from the wrong konto.
    bank = data.get("bank_konto")
    for a in data["accounts"]:
        if a["bas_konto"] == bank and a["saldo_ore"] < 0:
            out.append({"level": "flag", "text":
                        f"Konto {bank} {a['konto_namn']} har NEGATIVT saldo "
                        f"({_kr(a['saldo_ore'])} kr) — pengar har betalats ut som inte "
                        "fanns på kontot."})

    # 8. Future-dated entries.
    today = data.get("today") or datetime.date.today().isoformat()
    future = [vnum[v["id"]] for v in vers if v["ver_date"] > today]
    if future:
        out.append({"level": "flag", "text":
                    f"Verifikationer daterade i framtiden (idag {today}): " + _ids(future)})

    # 9. Late bookkeeping — informational, and noisy by design for a book entered in one
    #    sitting, which is why the count comes with that caveat.
    late = 0
    for v in vers:
        try:
            d1 = datetime.date.fromisoformat(v["ver_date"])
            d2 = datetime.date.fromisoformat((v["registration_date"] or "")[:10])
        except ValueError:
            continue
        if (d2 - d1).days > LATE_BOOKING_DAYS:
            late += 1
    if late:
        out.append({"level": "info", "text":
                    f"{late} verifikationer registrerades mer än {LATE_BOOKING_DAYS} dagar "
                    "efter affärshändelsens datum. (Normalt om boken lagts in i efterhand.)"})

    # 10. Duplicate suspicion: same kvitto-/fakturanummer on two different entries. The
    #     double-booked receipt is the error this file exists to catch.
    seen: dict[str, list[str]] = {}
    for v in vers:
        ref = (v.get("ext_ref") or "").strip()
        if ref:
            seen.setdefault(ref.lower(), []).append(vnum[v["id"]])
    dup_ref = {r: ids for r, ids in seen.items() if len(ids) > 1}
    if dup_ref:
        out.append({"level": "flag", "text":
                    "Samma kvitto-/fakturanummer på flera verifikationer (kan vara en "
                    "dubbelbokföring, kan vara en delbetalning): "
                    + _ids([f"{r} → {', '.join(ids)}" for r, ids in dup_ref.items()])})
    return out


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

_UPPDRAG = """## Uppdrag till granskaren

Du granskar bokföringen för en **enskild näringsidkare** i Sverige. Allt som finns i
boken ligger i den här filen. Gå igenom den och svara på:

1. Är något bokfört på ett konto som inte passar vad posten beskriver?
2. Finns kostnader som ser privata ut och alltså inte hör till verksamheten?
3. Ser momsen rimlig ut post för post — rätt momssats, moms lyft där avdragsrätt kan
   saknas (privata inköp, representation, personbil), eller moms som saknas helt?
4. Är omvänd betalningsskyldighet (EU-tjänster m.m.) hanterad konsekvent — både
   utgående och ingående moms redovisad på samma post?
5. Finns dubbelbokföringar — samma belopp, datum eller kvittonummer två gånger?
6. Saknas underlag där det borde finnas? Är de egenupprättade verifikationernas
   motiveringar begripliga och tillräckliga?
7. Är något som ser ut som en inventarie kostnadsfört direkt, eller omvänt — en småsak
   aktiverad i stället för att dras av direkt? Saknas avskrivning på något som aktiverats?
8. Stämmer konteringarnas riktning (debet/kredit) med vad texten säger?
9. Ser saldon orimliga ut — negativ kassa/bank, kundfordringar som ligger kvar,
   momskonton som inte nollas när deklarationen lämnas, eget kapital som ser fel ut?
10. Något annat som sticker ut.

Svara med en lista: **verifikationsnummer, vad som ser fel ut, varför, och vad du skulle
bokföra i stället.** Skriv hur säker du är på varje punkt.

Viktigt: du ser bara bokföringen, inte underlagen (kvitton, fakturor, avtal). Skilj
därför på "det här **är** fel" och "det här **bör kontrolleras** mot underlaget". Hitta
inte på regler du är osäker på — säg i stället att punkten behöver verifieras mot
Skatteverket eller en revisor.

### Konventioner i filen

* Belopp i svenska kronor med decimalkomma och utan tusentalsavgränsare.
* Debet och kredit står i egna kolumner, aldrig som en teckenförsedd kolumn. Varje
  verifikation balanserar: summa debet = summa kredit.
* Kontonumren följer BAS-kontoplanen, men **kontonamnen är bokens egna** — läs dem, de
  kan avvika från standardbenämningen.
* "Ver" = verifikationsnummer. Serien ska vara obruten; nummer återanvänds aldrig.
* **Egenupprättad verifikation** = det finns inget externt underlag (t.ex. en privat
  tillgång som förts in i verksamheten, en ingående balans, en avskrivning). Då är
  motiveringen underlaget (BFL 5 kap.), och den redovisas under verifikationen.
* En **rättelse** bokas som en egen verifikation som refererar till originalet. Båda
  står kvar — en bokförd verifikation får aldrig ändras eller tas bort.
"""


def review_markdown(data: dict) -> str:
    """Render the whole book as Markdown. `data` is assembled by `BookOps.review_export`."""
    mask = _masker(data.get("mask") or [])
    period = data["period"]
    vers = data["verifikationer"]
    vnum = {v["id"]: _vnum(v) for v in vers}
    konto_namn = {a["bas_konto"]: a["konto_namn"] for a in data["accounts"]}
    L: list[str] = []

    def h(level, text):
        L.append("")
        L.append("#" * level + " " + text)
        L.append("")

    L.append("# Bokföringsunderlag för granskning")
    L.append("")
    L.append(f"Exporterad ur BokYup {data.get('today') or ''} "
             f"(schemaversion {data.get('schema_version')}).")
    L.append("")
    L.append(_UPPDRAG)

    # ---- om boken ----------------------------------------------------------
    h(2, "Om boken")
    metod = {"kontantmetod": "kontantmetod (bokförs när pengarna rör sig)",
             "fakturametod": "fakturametod (bokförs vid fakturadatum och vid betalning)"}
    span = (f"{period['start']} – {period['end']}"
            + (" (hela boken)" if not period.get("filtered") else " (filtrerad)"))
    rows = [
        ["Företagsform", "Enskild näringsidkare (Sverige)"],
        ["Bokföringsmetod", metod.get(data["method"], data["method"])],
        ["Period i exporten", span],
        ["Antal verifikationer", len(vers)],
        ["Antal konteringar", sum(len(v["postings"]) for v in vers)],
        ["Antal affärshändelser", len(data["transaktioner"])],
        ["Antal kundfakturor", len(data.get("invoices") or [])],
        ["Konton i bruk", len(data["accounts"])],
    ]
    if data.get("company_name"):
        rows.insert(0, ["Verksamhet", data["company_name"]])
    L += _table(rows, ["Uppgift", "Värde"])

    locks = data.get("locks") or []
    if locks:
        L.append("**Låsta perioder** (momsdeklaration inlämnad — inget får bokföras in i "
                 "dem i efterhand):")
        L.append("")
        L += _table([[l["period_start"], l["period_end"], l["kind"], l["locked_at"][:10]]
                     for l in locks],
                    ["Från", "Till", "Typ", "Låst"])
    else:
        L.append("Inga låsta perioder (ingen momsdeklaration är markerad som inlämnad).")
        L.append("")

    L.append("**Vad filen innehåller och inte.** Den innehåller bokföringen: konton, "
             "belopp, datum, verifikationstexter, momsrader och leverantörsnamn. Den "
             "innehåller INTE personnummer, adresser, e-post eller telefonnummer."
             + (" Kundnamn är maskerade till \"Kund N\" (N = kundnummer i boken)."
                if data.get("mask") else
                " Kundnamn är med, på begäran."))
    L.append("")

    # ---- maskinella kontroller --------------------------------------------
    h(2, "Maskinella kontroller (körda av appen)")
    icon = {"ok": "✓", "flag": "⚠", "info": "·"}
    for k in kontroller(data):
        L.append(f"* {icon.get(k['level'], '·')} {k['text']}")
    L.append("")
    L.append("Kontrollerna ovan är mekaniska. Att de går igenom betyder att bokföringen "
             "är formellt hel — inte att den är rätt bokförd.")
    L.append("")

    # ---- kontoplan ---------------------------------------------------------
    h(2, "Kontoplan (de konton som används)")
    L += _table([[a["bas_konto"], a["konto_namn"], _konto_kind(a["bas_konto"]),
                  len(a["lines"])]
                 for a in data["accounts"]],
                ["Konto", "Namn", "Typ", "Antal konteringar"], align="llll")

    # ---- saldolista --------------------------------------------------------
    h(2, "Saldolista (saldobalans)")
    L.append("Debet och kredit per konto för perioden, och saldot. Ett saldo är positivt "
             "på debetsidan: tillgångar och kostnader är normalt positiva, eget kapital, "
             "skulder och intäkter normalt negativa.")
    L.append("")
    rows = [[a["bas_konto"], a["konto_namn"], _kr(a["debit_ore"]), _kr(a["credit_ore"]),
             _kr(a["saldo_ore"])] for a in data["accounts"]]
    tot_d = sum(a["debit_ore"] for a in data["accounts"])
    tot_k = sum(a["credit_ore"] for a in data["accounts"])
    rows.append(["", "**Summa**", _kr(tot_d), _kr(tot_k), _kr(tot_d - tot_k)])
    L += _table(rows, ["Konto", "Namn", "Debet", "Kredit", "Saldo"], align="llrrr")

    # ---- resultat ----------------------------------------------------------
    res = data["resultat"]
    h(2, "Resultat per konto")
    rows = [[c["bas_konto"], c["name"],
             "intäkt" if c["kind"] == "income" else "kostnad", _kr(c["amount_ore"])]
            for c in res["by_category"]]
    L += _table(rows, ["Konto", "Namn", "Typ", "Belopp"], align="lllr")
    L += _table([["Intäkter", _kr(res["income_ore"])],
                 ["Kostnader", _kr(res["expense_ore"])],
                 ["**Resultat**", _kr(res["result_ore"])]],
                ["Post", "Belopp"], align="lr")

    # ---- moms --------------------------------------------------------------
    moms = data["moms"]
    h(2, "Momsdeklaration för perioden")
    box_label = {
        "05": "Momspliktig försäljning (beskattningsunderlag)",
        "10": "Utgående moms 25 %", "11": "Utgående moms 12 %", "12": "Utgående moms 6 %",
        "20": "Inköp av varor från annat EU-land",
        "21": "Inköp av tjänster från annat EU-land",
        "22": "Inköp av tjänster från land utanför EU",
        "23": "Inköp av varor i Sverige, omvänd skattskyldighet",
        "24": "Inköp av tjänster i Sverige, omvänd skattskyldighet",
        "30": "Utgående moms 25 % på omvänd skattskyldighet",
        "31": "Utgående moms 12 % på omvänd skattskyldighet",
        "32": "Utgående moms 6 % på omvänd skattskyldighet",
        "48": "Ingående moms att dra av",
        "49": "Moms att betala (+) eller få tillbaka (−)",
    }
    L += _table([[b, box_label.get(b, ""), _kr(v)] for b, v in moms["boxes"].items()],
                ["Ruta", "Vad rutan är", "Belopp"], align="llr")
    L.append(f"Momsfri försäljning {_kr(moms['momsfri_sales_ore'])} kr, "
             f"nollskattesatsad {_kr(moms['zero_rated_sales_ore'])} kr.")
    L.append("")

    # ---- årsbokslut --------------------------------------------------------
    ab = data["arsbokslut"]
    h(2, "Förenklat årsbokslut (SKV 2150), beräknat på perioden")
    L.append("Bokens konton mappade in i blankettens rutor. Resultatrutorna (R) är "
             "periodens rörelse, balansrutorna (B) är ackumulerat saldo till periodens "
             "slut. Rutorna är bara meningsfulla för ett **helt räkenskapsår**; är "
             "perioden kortare eller längre läser du R-sidan med det i minnet.")
    L.append("")
    rows = [[b, ab["resultat"][b]["label"], _kr(ab["resultat"][b]["value_ore"])]
            for b in ab["resultat_order"] if b in ab["resultat"]]
    L += _table(rows, ["Ruta", "Post", "Belopp"], align="llr")
    rows = []
    for b in ab["balans_tillgangar_order"] + ab["balans_ek_skulder_order"]:
        if b in ab["balans"]:
            rows.append([b, ab["balans"][b]["label"], _kr(ab["balans"][b]["value_ore"])])
    L += _table(rows, ["Ruta", "Post", "Belopp"], align="llr")
    L.append(f"Summa tillgångar {_kr(ab['summa_tillgangar_ore'])} kr mot summa eget "
             f"kapital och skulder {_kr(ab['summa_ek_skulder_ore'])} kr — "
             + ("balanserar." if ab["balanserar"]
                else f"**balanserar inte, differens {_kr(ab['diff_ore'])} kr.**"))
    L.append("")

    # ---- affärshändelser ---------------------------------------------------
    h(2, "Affärshändelser med momsrader")
    L.append("Varje inköp och inkomst som gått genom appens formulär, med momsraderna som "
             "bestämmer momsdeklarationen. Kontot på raden är det konto raden faktiskt "
             "bokades till. Här syns momssatsen per rad, vilket inte framgår av "
             "konteringarna i grundboken. Momsen på ett **inköp** är ingående (avdragsgill), "
             "på en **inkomst** utgående (att betala).")
    L.append("")
    rows = []
    for t in data["transaktioner"]:
        # direction 'in' = the goods/service came IN (a purchase); 'out' = a sale.
        kind = "Inköp" if t["direction"] == "in" else "Inkomst"
        if t["supplier_name"]:
            motpart = mask(t["supplier_name"])
        elif t["customer_id"]:
            # Same bracketed tag as the masked free text, so a redaction always looks
            # like one and is never mistaken for a name the user typed.
            motpart = (f"[Kund {t['customer_id']}]" if data.get("mask")
                       else (t.get("customer_name") or f"Kund {t['customer_id']}"))
        else:
            motpart = ""
        ver = (f"{t['series']}{t['ver_number']}" if t["ver_number"] else "")
        status = "bokförd" if t["status"] == "paid" else "OBOKFÖRD (pending)"
        for i, ln in enumerate(t["lines"]):
            rows.append([
                t["trans_date"] if i == 0 else "",
                kind if i == 0 else "",
                motpart if i == 0 else "",
                f"{ln['konto'] or ''} {konto_namn.get(ln['konto'], '')}".strip(),
                t["ext_ref"] or "",
                ln["rate_code"],
                ln["reverse_charge"] or "",
                _kr(ln["ex_moms_ore"]), _kr(ln["moms_ore"]), _kr(ln["inc_moms_ore"]),
                (("ja" if t["receipts"] else "nej") if i == 0 else ""),
                status if i == 0 else "",
                ver if i == 0 else "",
            ])
        extra = [x for x in (mask(t["note"]),
                             "öresavrundad till hel krona" if t["ores_rounding"] else None)
                 if x]
        if extra:
            rows.append(["", "", "", "↳ " + " · ".join(extra), "", "", "", "", "", "",
                         "", "", ""])
    L += _table(rows, ["Datum", "Typ", "Motpart", "Konto", "Kvitto/Faktnr", "Moms%",
                       "Omvänd moms", "Ex moms", "Moms", "Inkl moms", "Underlag",
                       "Status", "Ver"],
                align="lllllllrrrlll")

    # ---- kundfakturor ------------------------------------------------------
    invs = data.get("invoices") or []
    if invs:
        h(2, "Kundfakturor")
        rows = []
        for iv in invs:
            kund = (f"[Kund {iv['customer_id']}]" if data.get("mask")
                    else (iv.get("customer_name") or f"Kund {iv['customer_id']}"))
            rows.append([
                iv["invoice_number"], iv["invoice_date"], iv.get("due_date") or "", kund,
                _kr(iv["ex_moms_ore"]), _kr(iv["inc_moms_ore"]),
                _kr((iv.get("rut_total_ore") or 0) + (iv.get("rot_total_ore") or 0)),
                _kr(iv.get("outstanding_ore")), iv.get("state") or "",
                "ja" if iv.get("husavdrag_shortfall_ore") else "",
            ])
        L += _table(rows, ["Nr", "Datum", "Förfaller", "Kund", "Ex moms", "Inkl moms",
                           "Husavdrag", "Kvar", "Status", "Uppföljning"],
                    align="llllrrrrll")

    # ---- grundbok ----------------------------------------------------------
    h(2, "Grundbok — alla verifikationer med konteringar")
    L.append("Verifikationerna i nummerordning. Detta är den legala bokföringen; allt "
             "ovanför är sammanställningar av just dessa konteringar.")
    for v in vers:
        pills = []
        if v.get("egenupprattad"):
            pills.append("egenupprättad")
        if v.get("rattelse_of"):
            pills.append("rättelse av " + vnum.get(v["rattelse_of"],
                                                   f"ver-id {v['rattelse_of']}"))
        if v.get("ext_ref"):
            pills.append("kvitto/faktnr " + str(v["ext_ref"]))
        if v.get("underlag"):
            pills.append(f"underlag: {v['underlag']} fil(er)")
        else:
            pills.append("inget underlag bifogat")
        if not v.get("posted"):
            pills.append("EJ BOKFÖRD")
        h(3, f"{vnum[v['id']]} · {v['ver_date']} · {mask(v['text'])}")
        L.append("*" + " · ".join(pills) + "*")
        L.append("")
        rows = [[p["bas_konto"], p["konto_namn"], _dr(p["amount_ore"]),
                 _cr(p["amount_ore"]), mask(p.get("text"))]
                for p in v["postings"]]
        tot = sum(p["amount_ore"] for p in v["postings"])
        rows.append(["", "**Summa**",
                     _kr(sum(p["amount_ore"] for p in v["postings"] if p["amount_ore"] > 0)),
                     _kr(-sum(p["amount_ore"] for p in v["postings"] if p["amount_ore"] < 0)),
                     "" if tot == 0 else f"**OBALANS {_kr(tot)}**"])
        L += _table(rows, ["Konto", "Namn", "Debet", "Kredit", "Text"], align="llrrl")
        if v.get("motivering"):
            L.append("Motivering (egenupprättad verifikation): " + mask(v["motivering"]))
            L.append("")
        if v.get("kommentar"):
            L.append("Kommentar: " + mask(v["kommentar"]))
            L.append("")
        if v.get("registration_date") and v["registration_date"][:10] != v["ver_date"]:
            L.append(f"Registrerad {v['registration_date'][:10]} "
                     f"(affärshändelsens datum {v['ver_date']}).")
            L.append("")

    L.append("")
    L.append("*Slut på exporten.*")
    return "\n".join(L) + "\n"
