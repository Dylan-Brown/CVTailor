// popup.js
//
// The actual page-text extraction and Gemini Nano handoff now live in
// background.js's runExtraction(), shared with the Alt+Shift+E keyboard
// shortcut (see manifest.json's "commands" + background.js's
// chrome.commands.onCommand listener) -- that shortcut has to work with
// the popup never having been opened, so this button is now just a
// thin trigger for the same background flow, not its own copy of it.

document.addEventListener('DOMContentLoaded', () => {
  const shortcutLabel = document.getElementById('shortcut-label');
  const setShortcutLink = document.getElementById('set-shortcut-link');

  chrome.commands.getAll((commands) => {
    const cmd = commands.find((c) => c.name === 'extract-job-details');
    if (cmd && cmd.shortcut) {
      shortcutLabel.textContent = ` (${cmd.shortcut})`;
      return;
    }

    // Chrome only auto-applies manifest.json's suggested_key the FIRST time
    // it sees a given command name -- if it was ever unassigned (a fresh
    // install where the suggested key collided with something else on that
    // machine, or an existing install after the command's key changed),
    // there is no way for the extension to bind it programmatically. This
    // is the only recourse: point straight at chrome://extensions/shortcuts
    // instead of leaving the user to go find it themselves.
    shortcutLabel.textContent = ' (no shortcut set)';
    setShortcutLink.hidden = false;
    setShortcutLink.addEventListener('click', () => {
      chrome.tabs.create({ url: 'chrome://extensions/shortcuts' });
      window.close();
    });
  });

  const versionLabel = document.getElementById('version-label');
  versionLabel.textContent = `v${chrome.runtime.getManifest().version}`;
});

document.getElementById('extract-btn').addEventListener('click', async () => {
  const btn = document.getElementById('extract-btn');
  const status = document.getElementById('status');

  btn.disabled = true;
  status.textContent = 'Reading page...';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      status.textContent = 'Could not find the active tab.';
      btn.disabled = false;
      return;
    }

    chrome.runtime.sendMessage({
      action: 'RUN_EXTRACTION',
      tabId: tab.id,
      url: tab.url,
    });

    status.textContent = 'Sent. Watch the page for a status toast.';
    // Extraction continues in background.js after the popup closes --
    // service worker messaging doesn't require the popup to stay open.
    setTimeout(() => window.close(), 600);

  } catch (error) {
    console.error('Extraction failed:', error);
    status.textContent = `Error: ${error.message}`;
    btn.disabled = false;
  }
});
