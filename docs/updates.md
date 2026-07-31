# Distribution & updates

BokYup ships through **GitHub Releases**. A push of a version tag (`v0.2.0`) runs
`.github/workflows/release.yml`, which builds the Windows desktop app and the Android
APK and attaches them to the release. Updates are **never automatic** — the app checks
for a newer release (optionally on startup) and the user clicks **"Uppdatera nu"**.

## Cutting a release

```bash
# bump backend/__init__.py __version__ to match, then:
git tag v0.2.0
git push origin v0.2.0
```

The workflow stamps the version into the build, so the running app reports the tag's
version and the in-app check compares correctly.

## Windows (.exe) — in-app update

- The build is a PyInstaller one-folder app zipped as `BokYup-windows-<version>.zip`,
  with `BokYupUpdater.exe` bundled inside.
- On startup (if "sök automatiskt" is on) the app calls `GET /update-check` against this
  repo's latest release. If newer, the home screen shows a banner.
- **Uppdatera nu** → `POST /update-apply`: the app downloads the new zip, launches a temp
  copy of `BokYupUpdater.exe`, and exits. The updater waits for the app to close, backs up
  the old install to `…\BokYup.bak`, extracts the new build, and relaunches.
- User books live in `~/.buyn` (or `BUYN_DATA_DIR`), **outside** the install dir, so an
  update never touches the encrypted 7-year archive. `schema.migrate()` upgrades a book on
  open, so a schema bump across an update is handled.
- **Code signing** (optional but recommended): an unsigned exe triggers SmartScreen. Sign
  the exe/installer with a code-signing cert for distribution.

## Android (.apk)

**Now: Obtainium** (zero infrastructure). Users install
[Obtainium](https://github.com/ImranR98/Obtainium), add this GitHub repo as an app source,
and Obtainium notifies + installs new APKs from the releases. The release asset is named
`bokyup-<version>.apk` and the tag carries the version, which Obtainium reads.

- For real (updatable) releases the APK must be **signed with a stable key**. Add these
  repo secrets and the workflow signs `assembleRelease`:
  `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
  `ANDROID_KEY_PASSWORD`. **Keep the keystore safe** — Android requires the same signing
  key for every update. Without the secrets the workflow builds an unsigned debug APK
  (fine for testing, not for update chains).

**Later: Capgo (OTA web/WASM updates).** Because the whole app (UI + Python via Pyodide)
is web assets inside the WebView, most updates change only the `www` bundle — no new APK.
[Capgo](https://capgo.app) (`@capgo/capacitor-updater`) can push those over-the-air, so
the phone updates instantly and a full APK is only needed when the native shell changes.
Not wired up yet — noted for a future increment.

## Settings / behaviour

- **No auto-install**, ever — the user must click and approve.
- **Auto-check on startup** is on by default; a checkbox on the home screen turns it off,
  after which the user searches manually via "Sök efter uppdateringar".
- Offline / rate-limited checks fail softly (no error popups).
