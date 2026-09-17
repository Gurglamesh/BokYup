"""
bas_catalog.py — a preset chart of BAS-konton to pick from.

A small business should not have to type account numbers from memory. This is a curated
subset of the standard **BAS-kontoplan** (the chart every Swedish accounting program and
revisor uses, and the one Skatteverket's blanketter are mapped from) covering what an
enskild firma actually books: sales, purchases, premises, consumables, vehicles, travel,
office, external services, and the balance-sheet accounts a manual verifikation needs.

Two deliberate properties:

* **It is a suggestion, not a rule.** Every entry is created as an ordinary, fully
  editable category/konto — name, number and default moms can all be changed afterwards,
  and the user may add konton that are not listed here.
* **It is not exhaustive.** Konton for areas where the standard chart has moved recently
  (e.g. the 2026 restructuring of class 4, reverse-charge and EU/import purchase konton)
  are deliberately left out rather than guessed at — check those against the current
  BAS-kontoplan and add them by hand.

`kind`:
  income / expense — bookable as a category (the entry/report accounts)
  asset / debt / equity — balance-sheet konton; added to the chart of accounts only, so
  they can be picked in a manual verifikation (a category is always income or expense).

`rate_code` is the moms rate the line editor pre-fills; None = no suggestion.
"""

from __future__ import annotations

# (bas_konto, name, kind, rate_code, group, description)
_ENTRIES: list[tuple] = [
    # ---------------------------------------------------------------- intäkter
    (3001, "Försäljning inom Sverige, 25 % moms", "income", "25",
     "Intäkter – varor", "Varuförsäljning till kund i Sverige, normal momssats."),
    (3002, "Försäljning inom Sverige, 12 % moms", "income", "12",
     "Intäkter – varor", "Varor med 12 % moms (t.ex. livsmedel, restaurang)."),
    (3003, "Försäljning inom Sverige, 6 % moms", "income", "6",
     "Intäkter – varor", "Varor med 6 % moms (t.ex. böcker, tidskrifter)."),
    (3004, "Försäljning inom Sverige, momsfri", "income", "momsfri",
     "Intäkter – varor", "Försäljning som är undantagen från moms."),
    (3041, "Försäljning tjänster inom Sverige, 25 % moms", "income", "25",
     "Intäkter – tjänster", "Tjänsteförsäljning till kund i Sverige, normal momssats. "
     "Det vanligaste intäktskontot för ett tjänsteföretag."),
    (3042, "Försäljning tjänster inom Sverige, 12 % moms", "income", "12",
     "Intäkter – tjänster", "Tjänster med 12 % moms."),
    (3043, "Försäljning tjänster inom Sverige, 6 % moms", "income", "6",
     "Intäkter – tjänster", "Tjänster med 6 % moms (t.ex. viss kultur och transport)."),
    (3044, "Försäljning tjänster inom Sverige, momsfri", "income", "momsfri",
     "Intäkter – tjänster", "Tjänster undantagna från moms (t.ex. vissa vårdtjänster)."),
    (3105, "Försäljning varor till annat EU-land", "income", "0",
     "Intäkter – utland", "Momsfri EU-försäljning av varor till momsregistrerad köpare "
     "(köparens VAT-nummer på fakturan; redovisas i periodisk sammanställning)."),
    (3108, "Försäljning varor till land utanför EU (export)", "income", "0",
     "Intäkter – utland", "Export av varor — ingen svensk moms."),
    (3305, "Försäljning tjänster till annat EU-land", "income", "0",
     "Intäkter – utland", "Tjänster till näringsidkare i annat EU-land — omvänd "
     "skattskyldighet, köparen redovisar momsen."),
    (3308, "Försäljning tjänster till land utanför EU", "income", "0",
     "Intäkter – utland", "Tjänster till köpare utanför EU — ingen svensk moms."),
    (3520, "Fakturerade frakter", "income", "25",
     "Intäkter – sidointäkter", "Frakt/leverans som du fakturerar kunden vidare. "
     "Följer varans momssats."),
    (3540, "Faktureringsavgifter", "income", "25",
     "Intäkter – sidointäkter", "Fakturaavgift du tar ut av kunden."),
    (3590, "Övriga sidointäkter", "income", "25",
     "Intäkter – sidointäkter", "Sidointäkter som inte hör hemma på ett eget konto."),
    (3740, "Öres- och kronutjämning", "income", "momsfri",
     "Intäkter – övrigt", "Öresavrundning enligt avrundningslagen. Programmet bokför "
     "hit automatiskt — lägg inte upp egna transaktioner här."),
    (3960, "Valutakursvinster på fordringar och skulder av rörelsekaraktär", "income", "momsfri",
     "Intäkter – övrigt", "Kursvinst när en kund- eller leverantörsskuld i utländsk "
     "valuta regleras."),
    (3973, "Vinst vid avyttring av maskiner och inventarier", "income", "momsfri",
     "Intäkter – övrigt", "Vinst när du säljer en inventarie för mer än dess "
     "bokförda värde."),
    (3985, "Erhållna statliga bidrag", "income", "momsfri",
     "Intäkter – övrigt", "Bidrag och stöd från stat/myndighet."),
    (3994, "Försäkringsersättningar", "income", "momsfri",
     "Intäkter – övrigt", "Ersättning från försäkringsbolag."),
    (3990, "Övriga ersättningar och intäkter", "income", "momsfri",
     "Intäkter – övrigt", "Intäkter som inte passar någon annanstans."),

    # ------------------------------------------------------- varor och material
    (4010, "Inköp material och varor", "expense", "25",
     "Varor och material", "Varor du köper in för att sälja vidare, och material som "
     "går åt i uppdragen."),
    (4056, "Inköp av varor från annat EU-land, 25 % moms", "expense", "25",
     "Varor och material", "Unionsinternt förvärv: säljaren fakturerar utan moms och du "
     "redovisar både ut- och ingående moms själv."),
    (4545, "Import av varor, 25 % moms", "expense", "25",
     "Varor och material", "Varuimport från land utanför EU. Momsen redovisas mot "
     "Skatteverket utifrån Tullverkets monetära tullvärde."),
    (4600, "Legoarbeten och underentreprenader", "expense", "25",
     "Varor och material", "Arbete du köper in av en underleverantör för ett uppdrag."),

    # ------------------------------------------------------------------- lokal
    (5010, "Lokalhyra", "expense", "25",
     "Lokal", "Hyra för verkstad, kontor eller lager."),
    (5020, "El för belysning", "expense", "25",
     "Lokal", "Elkostnad för verksamhetslokalen."),
    (5090, "Övriga lokalkostnader", "expense", "25",
     "Lokal", "Städning, larm och annat som hör till lokalen."),

    # ----------------------------------------------------- hyra och förbrukning
    (5220, "Hyra av inventarier och verktyg", "expense", "25",
     "Hyra och förbrukning", "Korttidshyra av maskiner och verktyg."),
    (5250, "Hyra av datorer", "expense", "25",
     "Hyra och förbrukning", "Hyrd/leasad datorutrustning."),
    (5410, "Förbrukningsinventarier", "expense", "25",
     "Hyra och förbrukning", "Inventarier av mindre värde eller med kort livslängd — "
     "dras av direkt istället för att skrivas av. Gränsen är ett halvt prisbasbelopp "
     "exkl. moms; dyrare inköp bokförs som inventarie (1220/1250) och skrivs av."),
    (5420, "Programvaror", "expense", "25",
     "Hyra och förbrukning", "Licenser och abonnemang på programvara."),
    (5460, "Förbrukningsmaterial", "expense", "25",
     "Hyra och förbrukning", "Material som förbrukas löpande i arbetet."),
    (5480, "Arbetskläder och skyddsmaterial", "expense", "25",
     "Hyra och förbrukning", "Skyddskläder och skyddsutrustning (vanliga kläder är "
     "inte avdragsgilla)."),
    (5500, "Reparation och underhåll", "expense", "25",
     "Hyra och förbrukning", "Reparation av verksamhetens utrustning."),

    # ------------------------------------------------------------------ fordon
    (5611, "Drivmedel för personbilar", "expense", "25",
     "Fordon", "Bensin/diesel/el för företagets personbil."),
    (5615, "Leasingavgifter för personbilar", "expense", "25",
     "Fordon", "Leasing av personbil (halva momsen är normalt avdragsgill)."),
    (5616, "Försäkring och skatt för personbilar", "expense", "momsfri",
     "Fordon", "Fordonsskatt och trafikförsäkring."),
    (5619, "Övriga personbilskostnader", "expense", "25",
     "Fordon", "Service, däck, parkering och liknande."),

    # ------------------------------------------------------- frakt och resor
    (5710, "Frakter, transporter och försäkringar vid varudistribution", "expense", "25",
     "Frakt och resor", "Frakt du betalar för att få ut varor till kund."),
    (5810, "Biljetter", "expense", "6",
     "Frakt och resor", "Tåg-, buss- och flygbiljetter i tjänsten (persontransport "
     "inom Sverige har 6 % moms)."),
    (5841, "Milersättning, avdragsgill", "expense", "momsfri",
     "Frakt och resor", "Schablonersättning för tjänsteresa med egen bil. I en enskild "
     "firma bokas den mot eget kapital (2018), inte mot banken — det är ett avdrag, "
     "ingen utbetalning."),
    (5890, "Övriga resekostnader", "expense", "25",
     "Frakt och resor", "Parkering, broavgift och annat under tjänsteresa."),

    # -------------------------------------------------- reklam och representation
    (5910, "Annonsering", "expense", "25",
     "Reklam", "Annonser och betald marknadsföring."),
    (5930, "Reklamtrycksaker och direktreklam", "expense", "25",
     "Reklam", "Trycksaker, visitkort och utskick."),
    (5990, "Övriga reklam- och PR-kostnader", "expense", "25",
     "Reklam", "Webbplats, domän och annan marknadsföring."),
    (6071, "Representation, avdragsgill", "expense", "25",
     "Representation", "Den avdragsgilla delen av representation (kraftigt begränsad — "
     "kontrollera aktuellt belopp hos Skatteverket)."),
    (6072, "Representation, ej avdragsgill", "expense", "ej_avdragsgill",
     "Representation", "Den del av representationen som inte får dras av. Hela "
     "beloppet inkl. moms blir kostnad."),

    # ------------------------------------------------------------------ kontor
    (6110, "Kontorsmateriel", "expense", "25",
     "Kontor", "Papper, pennor, pärmar och liknande."),
    (6150, "Trycksaker", "expense", "25",
     "Kontor", "Tryck av dokument och blanketter."),
    (6211, "Fast telefoni", "expense", "25",
     "Kontor", "Fast telefonabonnemang."),
    (6212, "Mobiltelefon", "expense", "25",
     "Kontor", "Mobilabonnemang som används i verksamheten."),
    (6230, "Datakommunikation", "expense", "25",
     "Kontor", "Bredband och internetuppkoppling."),
    (6250, "Porto", "expense", "momsfri",
     "Kontor", "Frimärken och portokostnad."),

    # -------------------------------------------------- försäkring och risker
    (6310, "Företagsförsäkringar", "expense", "momsfri",
     "Försäkring och risk", "Företagsförsäkring och ansvarsförsäkring (försäkring är "
     "momsfri)."),
    (6351, "Konstaterade förluster på kundfordringar", "expense", "momsfri",
     "Försäkring och risk", "Kundfordran som säkert inte kommer att betalas. Vid "
     "konstaterad kundförlust får den utgående momsen justeras."),
    (6390, "Övriga riskkostnader", "expense", "momsfri",
     "Försäkring och risk", "Självrisker och andra riskkostnader."),

    # ------------------------------------------------------------- externa tjänster
    (6420, "Ersättningar till revisor", "expense", "25",
     "Externa tjänster", "Revision och revisorsarvode."),
    (6530, "Redovisningstjänster", "expense", "25",
     "Externa tjänster", "Bokförings- och deklarationshjälp."),
    (6540, "IT-tjänster", "expense", "25",
     "Externa tjänster", "Webbhotell, molntjänster och inköpt IT-support."),
    (6550, "Konsultarvoden", "expense", "25",
     "Externa tjänster", "Inköpta konsulttjänster."),
    (6570, "Bankkostnader", "expense", "momsfri",
     "Externa tjänster", "Bankavgifter, kortavgifter och avgifter för delbetalning/"
     "faktura (finansiella tjänster är momsfria)."),
    (6590, "Övriga externa tjänster", "expense", "25",
     "Externa tjänster", "Köpta tjänster som inte passar någon annanstans."),

    # ------------------------------------------------------------ övriga kostnader
    (6970, "Tidningar, tidskrifter och facklitteratur", "expense", "6",
     "Övriga kostnader", "Facklitteratur och branschtidningar."),
    (6981, "Föreningsavgifter, avdragsgilla", "expense", "momsfri",
     "Övriga kostnader", "Serviceavgift till bransch-/näringslivsorganisation "
     "(medlemsavgiften i sig är inte avdragsgill)."),
    (6991, "Övriga externa kostnader, avdragsgilla", "expense", "25",
     "Övriga kostnader", "Avdragsgilla småkostnader utan eget konto."),
    (6992, "Övriga externa kostnader, ej avdragsgilla", "expense", "ej_avdragsgill",
     "Övriga kostnader", "Kostnader som inte får dras av — hela beloppet inkl. moms "
     "blir kostnad och momsen dras inte."),

    # ---------------------------------------------------------- personal och avskrivning
    (7210, "Löner till tjänstemän", "expense", "momsfri",
     "Personal", "Bruttolön till anställd. (Ägarens egna uttag i en enskild firma är "
     "INTE lön — de bokförs mot eget kapital, 2013.)"),
    (7510, "Arbetsgivaravgifter", "expense", "momsfri",
     "Personal", "Lagstadgade sociala avgifter på utbetald lön."),
    (7690, "Övriga personalkostnader", "expense", "25",
     "Personal", "Friskvård, kaffe och annat kring personalen."),
    (7832, "Avskrivningar på inventarier och verktyg", "expense", "momsfri",
     "Avskrivningar", "Årets planenliga avskrivning på inventarier."),

    # ------------------------------------------------------ balanskonton (tillgångar)
    (1220, "Inventarier och verktyg", "asset", None,
     "Tillgångar", "Inventarie som ska skrivas av över flera år (över ett halvt "
     "prisbasbelopp exkl. moms)."),
    (1229, "Ackumulerade avskrivningar på inventarier och verktyg", "asset", None,
     "Tillgångar", "Summan av gjorda avskrivningar (minskar tillgångens värde)."),
    (1250, "Datorer", "asset", None,
     "Tillgångar", "Datorer som aktiveras och skrivs av."),
    (1259, "Ackumulerade avskrivningar på datorer", "asset", None,
     "Tillgångar", "Summan av gjorda avskrivningar på datorer."),
    (1510, "Kundfordringar", "asset", None,
     "Tillgångar", "Fakturerat men ännu inte betalt av kunden."),
    (1630, "Avräkning för skatter och avgifter (skattekonto)", "asset", None,
     "Tillgångar", "Ditt skattekonto hos Skatteverket."),
    (1650, "Momsfordran", "asset", None,
     "Tillgångar", "Moms du har till godo för perioden."),
    (1910, "Kassa", "asset", None,
     "Tillgångar", "Kontanter."),
    (1930, "Företagskonto / checkkonto", "asset", None,
     "Tillgångar", "Firmans bankkonto. Programmet bokför hit automatiskt."),

    # ------------------------------------------------- balanskonton (eget kapital/skulder)
    (2010, "Eget kapital", "equity", None,
     "Eget kapital", "Ägarens kapital i den enskilda firman."),
    (2013, "Övriga egna uttag", "equity", None,
     "Eget kapital", "Pengar eller varor du tar ut ur firman privat."),
    (2018, "Övriga egna insättningar", "equity", None,
     "Eget kapital", "Privata pengar du skjuter till firman — t.ex. när du betalar ett "
     "företagsinköp med privat kort eller privat delbetalning."),
    (2019, "Årets resultat", "equity", None,
     "Eget kapital", "Resultatet som förs över vid bokslutet."),
    (2440, "Leverantörsskulder", "debt", None,
     "Skulder", "Mottagna leverantörsfakturor som ännu inte betalats."),
    (2610, "Utgående moms, 25 %", "debt", None,
     "Skulder – moms", "Moms du tagit ut av kunden och är skyldig staten."),
    (2620, "Utgående moms, 12 %", "debt", None,
     "Skulder – moms", "Utgående moms med 12 %."),
    (2630, "Utgående moms, 6 %", "debt", None,
     "Skulder – moms", "Utgående moms med 6 %."),
    (2614, "Utgående moms omvänd skattskyldighet, 25 %", "debt", None,
     "Skulder – moms", "Moms du själv beräknar på ett inköp med omvänd "
     "betalningsskyldighet (t.ex. en EU-tjänst). Motsvaras av avdraget på 2645."),
    (2624, "Utgående moms omvänd skattskyldighet, 12 %", "debt", None,
     "Skulder – moms", "Som 2614 men med 12 % moms."),
    (2634, "Utgående moms omvänd skattskyldighet, 6 %", "debt", None,
     "Skulder – moms", "Som 2614 men med 6 % moms."),
    (2640, "Ingående moms", "debt", None,
     "Skulder – moms", "Moms på dina inköp, som du får dra av."),
    (2645, "Beräknad ingående moms på förvärv från utlandet", "debt", None,
     "Skulder – moms", "Avdragsgill motpost till den moms du själv beräknat vid "
     "omvänd betalningsskyldighet. Nettoeffekten blir noll vid full avdragsrätt."),
    (2650, "Redovisningskonto för moms", "debt", None,
     "Skulder – moms", "Nettot som deklareras för perioden."),
    (2710, "Personalskatt", "debt", None,
     "Skulder", "Avdragen preliminärskatt på lön, att betala in."),
    (2731, "Avräkning lagstadgade sociala avgifter", "debt", None,
     "Skulder", "Arbetsgivaravgifter att betala in."),
]

BAS_CATALOG: list[dict] = [
    {"bas_konto": k, "name": n, "kind": kind, "rate_code": rate,
     "group": group, "description": desc}
    for (k, n, kind, rate, group, desc) in _ENTRIES
]

CATALOG_BY_KONTO: dict[int, dict] = {e["bas_konto"]: e for e in BAS_CATALOG}

#: kinds that can be created as a bookable category (the rest are balance-sheet konton)
CATEGORY_KINDS = ("income", "expense")


def catalog_entry(bas_konto: int) -> dict | None:
    return CATALOG_BY_KONTO.get(int(bas_konto))
