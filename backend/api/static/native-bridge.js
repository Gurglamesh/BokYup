"use strict";
/*
 * native-bridge.js — phone (.buyn) file bridge for backup/restore.
 *
 * Only the phone (Capacitor) build includes this. It implements
 * window.__BOKYUP_FILES__, the seam app.js calls so that "Exportera säkerhetskopia"
 * and "Återställ säkerhetskopia" move a .buyn file in/out of the OS:
 *
 *   shareFile(fsPath)       read the bundle from the app FS and hand it to the OS
 *                           share sheet (save to Files / send to another device).
 *   pickBuynIntoFs()        let the user pick a .buyn, copy it into the app FS, and
 *                           return { fsPath, name } for POST /books/import.
 *
 * Bytes cross between the OS and the Pyodide filesystem via the readFile/writeFile
 * helpers pyodide-boot.js exposes on window.__BOKYUP_NATIVE__. Capacitor's
 * Filesystem + Share plugins are reached through the runtime plugin registry
 * (window.Capacitor.Plugins), so this file needs no bundler.
 */

(function () {
  function cap() {
    return (window.Capacitor && window.Capacitor.Plugins) || {};
  }
  function toBase64(bytes) {
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }
  function fromBase64(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // Export: app FS -> a cache file -> OS share sheet.
  async function shareFile(fsPath) {
    const { Filesystem, Share } = cap();
    const name = fsPath.split("/").pop() || "bok.buyn";
    const bytes = window.__BOKYUP_NATIVE__.readFile(fsPath);
    if (!Filesystem || !Share) {
      // Fallback (e.g. web preview): trigger a normal browser download.
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
      const a = document.createElement("a");
      a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      return;
    }
    const written = await Filesystem.writeFile({
      path: name, data: toBase64(bytes), directory: "CACHE",
    });
    await Share.share({ title: name, url: written.uri });
  }

  // Restore: pick a .buyn -> copy into the app FS -> return its in-FS path.
  function pickBuynIntoFs() {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".buyn,application/octet-stream";
      input.style.display = "none";
      input.addEventListener("change", async () => {
        const file = input.files && input.files[0];
        input.remove();
        if (!file) { resolve(null); return; }
        const bytes = new Uint8Array(await file.arrayBuffer());
        const fsPath = `/bokyup-data/incoming_${Date.now()}.buyn`;
        await window.__BOKYUP_NATIVE__.writeFile(fsPath, bytes);
        resolve({ fsPath, name: file.name.replace(/\.buyn$/i, "") });
      });
      document.body.appendChild(input);
      input.click();
    });
  }

  window.__BOKYUP_FILES__ = { shareFile, pickBuynIntoFs, _fromBase64: fromBase64 };
})();
