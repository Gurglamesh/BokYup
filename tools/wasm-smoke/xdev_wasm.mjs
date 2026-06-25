// Cross-device .buyn round-trip — the PHONE (WASM) half.
//
// Runs the REAL backend under Pyodide, imports the PC-made pc.buyn, asserts every
// encrypted field decrypts, then makes a phone book and exports phone.buyn for the
// native side to re-import. Pair with xdev.py (see README.md).
//
//   node xdev_wasm.mjs <workdir>
//
// Requires: `npm i pyodide` here, the crypto wasm wheels placed next to the pyodide
// dist (so loadPackage finds them offline), and <workdir>/backend_src.zip
// (python tools/build_phone_assets.py --out <workdir>).
import { loadPyodide } from "pyodide";
import { readFileSync, writeFileSync } from "node:fs";

const WD = process.argv[2];
if (!WD) { console.error("usage: node xdev_wasm.mjs <workdir>"); process.exit(2); }

const py = await loadPyodide();
await py.loadPackage(["cryptography", "argon2-cffi"]);
py.unpackArchive(readFileSync(`${WD}/backend_src.zip`).buffer, "zip",
                 { extractDir: "/home/pyodide/src" });

py.FS.mkdirTree("/in");
py.FS.writeFile("/in/pc.buyn", new Uint8Array(readFileSync(`${WD}/pc.buyn`)));
py.globals.set("expected_json", readFileSync(`${WD}/pc_expected.json`, "utf8"));

const out = await py.runPythonAsync(`
import sys, json
sys.path.insert(0, "/home/pyodide/src")
from backend.api.phone import PhoneApp
exp = json.loads(expected_json)
def jc(app, *a): return json.loads(app.call(*a))

app = PhoneApp("/home/pyodide/data", autolock_seconds=900)
bid = jc(app, "POST", "/books/import", json.dumps(
    {"bundle_path": "/in/pc.buyn", "dest_db_path": "/home/pyodide/data/imported.db",
     "display_name": "From PC"}))["result"]["id"]
jc(app, "POST", f"/books/{bid}/unlock", json.dumps({"passphrase": exp["passphrase"]}))
cust = jc(app, "GET", f"/books/{bid}/customers/1")["result"]
vers = jc(app, "GET", f"/books/{bid}/verifikationer")["result"]
rec = jc(app, "GET", f"/books/{bid}/receipts/{exp['receipt_id']}")["result"]
res = {
  "pc_to_phone_personnummer_ok": cust["personnummer"] == exp["customer_personnummer"],
  "pc_to_phone_name_ok": f"{cust['first_name']} {cust['last_name']}" == exp["customer_name"],
  "pc_to_phone_ver_ok": len(vers) == 1 and vers[0]["ver_number"] == exp["ver_number"],
  "pc_to_phone_receipt_ok": rec["base64"] == exp["receipt_b64"],
}

# phone -> PC: make a new book under WASM and export it.
pb = jc(app, "POST", "/books", json.dumps(
    {"display_name": "PHONE AB", "db_path": "/home/pyodide/data/phone.db",
     "passphrase": "phone-secret-pass"}))["result"]["id"]
pc = jc(app, "POST", f"/books/{pb}/categories",
        json.dumps({"name": "Konsult", "kind": "income", "bas_konto": 3001}))["result"]["id"]
pk = jc(app, "POST", f"/books/{pb}/customers", json.dumps(
    {"type": "private", "first_name": "Björn", "last_name": "Berg",
     "personnummer": "19811218-9876"}))["result"]["kundnummer"]
jc(app, "POST", f"/books/{pb}/incomes", json.dumps(
    {"customer_id": pk, "category_id": pc,
     "lines": [{"rate_code": "12", "amount_ore": 5600, "inclusive": True}],
     "trans_date": "2026-04-01", "paid_date": "2026-04-01"}))
jc(app, "POST", f"/books/{pb}/export", json.dumps({"out_path": "/home/pyodide/phone.buyn"}))
json.dumps(res)
`);

writeFileSync(`${WD}/phone.buyn`, Buffer.from(py.FS.readFile("/home/pyodide/phone.buyn")));
const res = JSON.parse(out);
console.log("PC -> phone (WASM):", JSON.stringify(res));
if (!Object.values(res).every(Boolean)) { console.error("FAILED"); process.exit(1); }
console.log("phone.buyn written for native re-import");
