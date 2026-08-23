If you just want to test it right now, you can try loading your exact Chrome folder into Firefox as-is.
How to load it in Firefox (Dev Mode)

    Open Firefox and type about:debugging in the URL bar.

    Click This Firefox in the left sidebar.

    Click the Load Temporary Add-on... button.

    Select any file inside your extension's folder (like manifest.json).

If it loads and works, you're done! However, because your specific extension uses a Native Messaging Host, there is one major hurdle you will have to adjust for.
The Catch: Native Messaging Differences

While your JavaScript and HTML will likely work perfectly, Chrome and Firefox handle Native Messaging hosts slightly differently. You will need to update your PowerShell registration script (Install-NativeHost.ps1) and your host manifest to accommodate Firefox:

    The Host Manifest: In your com.thesistoolkit.cvtailor_intake.json file, Chrome requires a key called allowed_origins (which looks like chrome-extension://[YOUR_ID]/). Firefox rejects this. Instead, Firefox requires a key called allowed_extensions, which just takes the raw ID or an email-style ID (e.g., cvtailor@thesistoolkit.com).

    The Registry Key: Chrome looks for the native host manifest in the Windows Registry under HKCU\Software\Google\Chrome\NativeMessagingHosts. Firefox looks for it under HKCU\Software\Mozilla\NativeMessagingHosts. Your PowerShell script will need to write to both places if you want to support both browsers.

Other Minor Gotchas

    Service Workers vs. Background Scripts: If you are using Manifest V3, Chrome forces you to use a Service Worker (background.service_worker). Firefox supports this, but historically preferred traditional background scripts (background.scripts). If you get a background error in Firefox, this is usually why.

    Extension ID: Firefox requires you to specify a browser-specific ID in your manifest.json if you are using native messaging. You do this by adding a browser_specific_settings.gecko.id key to the manifest.

Mozilla provides a fantastic official CLI tool called web-ext. If you run web-ext lint in your extension folder, it will instantly scan your code and highlight any Chrome-specific APIs that Firefox won't accept.
