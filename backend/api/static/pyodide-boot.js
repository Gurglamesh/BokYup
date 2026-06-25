"use strict";
/*
 * pyodide-boot.js — phone / native transport (Phase 2 of the phone plan).
 *
 * On the phone the app has no server: the SAME web frontend runs in a WebView and
 * the SAME Python backend runs as WebAssembly via Pyodide, in-process. This script
 * boots Pyodide, loads the crypto wheels and the backend source (all bundled
 * locally — no network, which also satisfies the offline/privacy stance), mounts an
 * IndexedDB-backed filesystem so books persist across launches, and exposes
 * `window.__BOKYUP_NATIVE__.call(...)` — the in-process equivalent of an HTTP call.
 *
 * app.js detects `window.__BOKYUP_NATIVE__` and routes through it instead of fetch,
 * so exactly one frontend and one Python backend serve both PC and phone.
 *
 * This file is only included by the phone (Capacitor) build; the desktop server
 * never serves it, so the desktop app keeps using fetch.
 */

(function () {
  // Where Pyodide + wheels + backend_src.zip live, relative to the page. The phone
  // build vendors these as local assets (see tools/build_phone_assets.py).
  const VENDOR = (window.BOKYUP_VENDOR_URL || "vendor/");
  const DATA_DIR = "/bokyup-data";      // IndexedDB-backed; holds the books + photos
  const SRC_DIR = "/home/pyodide/src";  // the backend package, on sys.path

  let pyodide = null;
  let phone = null;                     // Python PhoneApp proxy

  function syncfs(populate) {
    return new Promise((resolve, reject) =>
      pyodide.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
  }

  async function init() {
    // loadPyodide is provided by vendor/pyodide.js (loaded before this script).
    pyodide = await loadPyodide({ indexURL: VENDOR });
    // Crypto stack as WASM wheels (must be parallelism=1 — see crypto.py).
    await pyodide.loadPackage(["cryptography", "argon2-cffi"]);

    // Persist the data directory in IndexedDB and load anything already there.
    pyodide.FS.mkdirTree(DATA_DIR);
    pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, DATA_DIR);
    await syncfs(true);

    // Unpack the backend source and instantiate the in-process app.
    const buf = await (await fetch(VENDOR + "backend_src.zip")).arrayBuffer();
    pyodide.unpackArchive(buf, "zip", { extractDir: SRC_DIR });
    phone = pyodide.runPython(
      `import sys
sys.path.insert(0, ${JSON.stringify(SRC_DIR)})
from backend.api.phone import PhoneApp
PhoneApp(${JSON.stringify(DATA_DIR)})`);

    window.__BOKYUP_NATIVE__ = { call };
    return true;
  }

  // The transport app.js calls. Same (method, path, body, query) contract as the
  // REST API; returns {ok, status, result} | {ok:false, status, detail}.
  async function call(method, path, body, query) {
    const out = JSON.parse(phone.call(
      method, path,
      body == null ? null : JSON.stringify(body),
      query == null ? null : JSON.stringify(query),
    ));
    // Any write may have changed the SQLite file or a photo blob — persist it.
    if (method !== "GET") await syncfs(false);
    return out;
  }

  // app.js awaits this before its first call when running natively.
  window.BokYupReady = init();
})();
