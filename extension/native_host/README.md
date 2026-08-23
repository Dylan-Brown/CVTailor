# CVTailor Native Messaging Host

By default, Chrome extensions are strictly limited to saving files in your `Downloads` folder. This Native Messaging Host works around that limitation by creating a secure local pipe. It allows the "Job Data Harvester" extension to send job data directly into your CVTailor `applications/` folder automatically—no manual file moving required.

## 1. Extension Setup (One-Time)

To ensure the extension can reliably connect to the host, its ID has been permanently pinned. 

1. Go to `chrome://extensions` in your browser.
2. If you already have the extension loaded, **remove it**.
3. Click **Load unpacked** and select the `extension/chrome` folder to reload it.

*(Note: This locks in the ID so you never have to configure this again. Just make sure you don't delete the `EXTENSION_PRIVATE_KEY.pem` file in this folder!)*

## 2. Register the Host

Next, you need to run a quick script that tells Chrome where to find the native host on your computer. 

1. Copy `com.thesistoolkit.cvtailor_intake.example.json` to `com.thesistoolkit.cvtailor_intake.json` in this same folder (gitignored -- it ends up with your machine's real path and your real extension ID, neither of which belong in git).
2. Open PowerShell.
3. Navigate to this folder (`extension/native_host`).
4. Run the registration script:
   ```powershell
   .\Install-NativeHost.ps1
   ```

*(This script automatically handles the file paths for you. If you ever move the CVTailor project folder to a new location on your computer, just run this script again).*

## 3. Test It Out

1. Open any job posting page and run the Job Data Harvester extension as usual.
2. Look for the success toast on your screen: `✅ Sent to CVTailor -> app_id: ...`
3. Verify the data landed correctly by checking your CVTailor `applications/` folder—you should see a new subfolder containing your `jd_input.json` file!

### Troubleshooting

* **`❌ Native host error`:** Chrome cannot find the host. Double-check that you re-loaded the extension (Step 1) and ran the install script (Step 2). Chrome also caches host connections, so you may just need to fully close and reopen Chrome.
* **`❌ CVTailor intake error`:** The connection worked perfectly, but the CVTailor pipeline itself rejected the data. Read the error text provided in the toast to see what the python script didn't like.

*(Note: If you ever wish to revert to the old method of manually moving files from your Downloads folder, simply restore the previous `manifest.json` and `background.js` configurations).*
