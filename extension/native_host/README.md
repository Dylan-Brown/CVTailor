# CVTailor intake native messaging host

Lets the "Job Data Harvester" extension send job data straight to
`py_stage00_normalize_jd_input.py` over a local pipe, instead of
downloading a JSON file to your Downloads folder and moving it by hand.

## Stable extension ID -- set up once, no more re-registering

The extension's `manifest.json` now has a pinned `"key"` field, which
makes its ID **permanently fixed** at `miloklgjeihnnkohppgclinoellnpibp`
regardless of how many times you remove and re-load it (previously,
every fresh load generated a new random ID, breaking
`allowed_origins` below and requiring a manual fix each time -- this
happened three times before getting pinned). `com.thesistoolkit.
cvtailor_intake.json` already has this ID filled in.

`EXTENSION_PRIVATE_KEY.pem` (this folder) is the private half of that
keypair -- keep it, don't lose it. If you ever need to reinstall from
scratch and this file is gone, the extension will get a new random ID
again and you're back to re-registering by hand. It's git-ignored
(`.pem` is in the toolkit's `.gitignore`), so it won't end up committed
anywhere by accident.

**One-time step after this update:** remove and re-load the extension
once more at `chrome://extensions` so Chrome picks up the pinned key.
After this one reload, the ID should never change again.

## Setup (one-time, ID is already filled in)

1. **Remove and reload the extension** at `chrome://extensions` (the
   pinned key means this is the LAST time the ID will change).
2. **Register the host:**
   ```powershell
   cd path\to\this\folder\extension\native_host
   .\Install-NativeHost.ps1
   ```
   `Install-NativeHost.ps1` rewrites `com.thesistoolkit.cvtailor_intake.json`'s
   `path` field to wherever `native_host.bat` actually lives (derived
   from its own location, not hardcoded) every time it runs — no manual
   path editing needed, including after moving or cloning this project
   somewhere else. Just re-run it if you ever move the folder.

## Test procedure

1. Open any job posting page, run the extension as usual (extract job
   details).
2. Watch for the toast: `✅ Sent to CVTailor -> app_id: ...` means it
   worked -- confirm with:
   ```powershell
   Get-ChildItem "path\to\this\project\applications\<app_id>\jd_input.json"
   ```
   (`applications/` is a sibling of `extension/`, `src/`, etc. at the project root.)
3. If you instead see `❌ Native host error: ...`, that's Chrome unable
   to launch/connect to the host at all -- double check:
   - The extension ID in `allowed_origins` actually matches (it changes
     if you ever reinstall/reload the extension from a different path).
   - `native_host.bat`'s path resolves and `python` is on PATH for the
     account Chrome runs as.
   - Re-run `Install-NativeHost.ps1` after any path/ID change --
     Chrome caches native host manifests per-session, so also try fully
     closing and reopening Chrome if a fix doesn't seem to take.
4. If you see `❌ CVTailor intake error: ...`, the connection worked but
   `py_stage00_normalize_jd_input.py` itself rejected the data -- the
   error text is that script's own stderr, same as you'd see running it
   directly from the command line.

## Reverting to the old download-based flow

If this turns out not to work and you want the old behavior back
immediately: `manifest.json`'s permissions and `background.js`'s
`sendToCVTailorIntake()` call are the only two changed pieces --
the previous `chrome.downloads.download(...)` version is preserved in
this conversation's history if you need to paste it back in.
