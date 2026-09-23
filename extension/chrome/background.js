chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "RUN_EXTRACTION") {
    const tabId = request.tabId || sender.tab?.id;
    runExtraction(tabId, request.url);
  }
});

// Alt+Shift+E shortcut (see manifest.json "commands") -- same flow as
// the popup button, without needing the popup open.
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "extract-job-details") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return;
  runExtraction(tab.id, tab.url);
});

// Shared by the popup button and the shortcut. Lives here (not popup.js)
// so the shortcut path works without the popup ever having been opened.
async function runExtraction(tabId, tabUrl, intakeMeta = null) {
  if (!tabId) return;

  // Excludes popup-open/tab-switch time -- starts right at DOM scrape.
  const extractionStartedAt = Date.now();

  // Only set when triggered by the file watcher's server; popup/shortcut
  // paths pass no 3rd arg, so IntakeTriggerId/IntakeEnqueuedAt stay unset.

  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      if (document.getElementById('job-harvester-toast')) return;
      const toast = document.createElement('div');
      toast.id = 'job-harvester-toast';
      toast.style.cssText = `
        position: fixed; top: 16px; right: 16px; z-index: 2147483647;
        max-width: 340px; padding: 12px 36px 12px 16px; border-radius: 8px;
        background: #064e3b; color: #ecfdf5; border: 2px solid #22c55e;
        font-family: -apple-system, "Segoe UI", sans-serif; font-size: 13px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3); transition: opacity 0.3s;
      `;
      toast.innerHTML = '<span class="job-harvester-toast-msg">⏳ Capturing Job Details...</span>';
      const closeBtn = document.createElement('button');
      closeBtn.textContent = '×';
      closeBtn.setAttribute('aria-label', 'Dismiss');
      closeBtn.style.cssText = `
        position: absolute; top: 6px; right: 8px; background: transparent;
        border: none; color: #ecfdf5; font-size: 18px; line-height: 1;
        cursor: pointer; padding: 2px 4px; opacity: 0.7;
      `;
      closeBtn.onmouseenter = () => { closeBtn.style.opacity = '1'; };
      closeBtn.onmouseleave = () => { closeBtn.style.opacity = '0.7'; };
      closeBtn.onclick = () => toast.remove();
      toast.appendChild(closeBtn);
      document.body.appendChild(toast);
    }
  });

  const [{ result: pageText }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: async () => {
      const EXPAND_PATTERN = /^(show|read|see|view)?\s*(more|full|entire|complete)(\s*(description|details|posting|job description))?\.*$|^more\.*$|^expand$|^\.\.\.\s*(more|show more)?$|^continue reading$/i;

      const definiteCandidates = document.querySelectorAll('button, [role="button"], [onclick]');
      const maybeCandidates = document.querySelectorAll('a, span, div');

      function normalizeLabel(el) {
        return (el.innerText || el.textContent || el.getAttribute('aria-label') || '')
          .trim().replace(/…/g, '').trim();
      }

      function isRealLink(el) {
        const anchor = el.closest('a');
        if (!anchor) return false;
        const href = anchor.getAttribute('href');
        return href && href !== '#' && !href.startsWith('javascript:');
      }

      let clicked = 0;
      for (const el of definiteCandidates) {
        if (isRealLink(el)) continue;
        const label = normalizeLabel(el);
        if (label.length > 0 && label.length < 40 && EXPAND_PATTERN.test(label)) {
          try { el.click(); clicked++; } catch (e) { /* not clickable, skip */ }
        }
      }
      for (const el of maybeCandidates) {
        if (isRealLink(el)) continue;
        const label = normalizeLabel(el);
        if (label.length > 0 && label.length < 40 && EXPAND_PATTERN.test(label)
            && getComputedStyle(el).cursor === 'pointer') {
          try { el.click(); clicked++; } catch (e) { /* not clickable, skip */ }
        }
      }
      if (clicked > 0) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }

      const selectors = [
        'main', '[role="main"]', 'article',
        '.job-description', '#job-description',
        '[class*="job-description"]', '[class*="jobDescription"]',
        '[data-testid*="job-description"]',
      ];
      let extractedText = document.body.innerText;
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.trim().length > 200) {
          extractedText = el.innerText;
          break;
        }
      }

      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });

      return extractedText;
    },
  });
  console.log(`[debug] extracted ${pageText.length} chars of page text`);

  await processWithGeminiNano(pageText, tabId, tabUrl, extractionStartedAt, intakeMeta);
}

async function processWithGeminiNano(rawText, tabId, sourceUrl, extractionStartedAt, intakeMeta = null) {
  try {
    let aiModel;

    if ('ai' in self && self.ai?.languageModel) {
      aiModel = self.ai.languageModel;
    } else if ('LanguageModel' in self) {
      aiModel = self.LanguageModel;
    } else {
      updateToast(tabId, "❌ Error: Chrome Built-in AI not detected.", true);
      return;
    }

    let isAvailable = 'no';
    if (typeof aiModel.availability === 'function') {
      isAvailable = await aiModel.availability(); 
    } else if (typeof aiModel.capabilities === 'function') {
      const caps = await aiModel.capabilities();
      isAvailable = caps.available;
    }
    console.log("[debug] raw availability value:", isAvailable);

    if (isAvailable === 'no') {
      updateToast(tabId, "❌ Error: Gemini Nano not available.", true);
      return;
    }
    console.log("[debug] availability check passed, value was:", isAvailable);

    const session = await aiModel.create({
      systemPrompt: "You are a rigid, programmatic JSON parser. You never output conversational text. You never invent new JSON keys.",
      monitor(m) {
        m.addEventListener("downloadprogress", (e) => {
          console.log(`[debug] model download progress: ${e.loaded}/${e.total}`);
        });
      }
    });
    console.log("[debug] session created, about to call prompt()");

    // If extraction looks thin, check the [debug] line above for how
    // many chars were actually extracted vs. how many made it through.
    const safeText = rawText.substring(0, 14000);
    console.log(`[debug] rawText was ${rawText.length} chars, sending ${safeText.length} to the model`);
    
    // Updated Sandwich Prompt with Checkmark Mapping and Output Limits
    const enforcedPrompt = `Extract the following job posting into a strict JSON object.

CRITICAL RULES:
1. You MUST find and extract the hiring "Company" name.
2. You MUST use EXACTLY the 17 keys listed below. Do not add any new keys.
3. Do not nest objects except WarmApplication, which is itself a fixed
   sub-object with exactly the 4 keys shown -- do not add keys to it either.
4. DO NOT SUMMARIZE OR TRUNCATE the arrays. Extract complete, verbatim sentences for Responsibilities, Requirements, and NiceToHave.
5. ISOLATE DESCRIPTIONS: Ensure the 'CompanyDescription' and 'RoleDescription' are distinctly different. Do not copy the same text into both fields.
6. WarmApplication fields are best-effort and frequently absent -- use
   an empty string "" for any of the 4 sub-fields you can't find
   explicitly stated in the posting. NEVER invent a name, contact
   method, email, or title that isn't actually written on the page --
   an empty string is correct and expected far more often than not.

REQUIRED TEMPLATE:
{
  "JobTitle": "exact title",
  "Company": "exact company name",
  "Location": "Extract the geographic location (e.g., 'United States') from the top header metadata.",
  "WorkplaceType": "Extract 'Remote', 'Hybrid', or 'Onsite' from the header metadata.",
  "Salary": "salary string",
  "EmploymentType": "Extract 'Full-time', 'Part-time', or 'Contract' from the header metadata.",
  "Department": "department string (or 'not specified')",
  "CompanyDescription": "Extract ONLY the first paragraph of the company bio/overview to conserve space.",
  "RoleDescription": "Extract the day-to-day role overview (usually located under 'About the job' or 'Overview'). Do not include the company description here.",
  "Benefits": ["benefit 1", "benefit 2"],
  "Responsibilities": ["exact verbatim from Responsibilities or What You'll Do"],
  "Requirements": ["exact verbatim from Requirements or Essential Qualifications"],
  "NiceToHave": ["exact verbatim from Nice to Have or Desired Qualifications"],
  "WarmApplication": {
    "PointOfContactName": "Name of the job post's poster, recruiter, or hiring manager if stated anywhere on the page -- else \"\"",
    "PointOfContactMethod": "The stated or most obvious way to reach that person -- e.g. 'LinkedIn message', 'Email' -- else \"\"",
    "PointOfContactEmail": "That person's email address if explicitly given -- else \"\"",
    "PointOfContactPosition": "That person's job title if stated -- else \"\""
  }
}

--- START JOB POSTING ---
${safeText}
--- END JOB POSTING ---

Generate the JSON object using ONLY the required template keys now:`;

    const result = await session.prompt(enforcedPrompt);
    session.destroy(); 

    console.log("Gemini Nano Raw Result:", result);

    const jsonMatch = result.match(/\{[\s\S]*\}/);
    let parsedData = null;

    if (jsonMatch) {
      let rawJsonString = jsonMatch[0];
      rawJsonString = rawJsonString.replace(/,\s*([}\]])/g, '$1');

      try {
        parsedData = JSON.parse(rawJsonString);
      } catch (parseError) {
        console.warn("Direct JSON.parse failed. Attempting newline sanitization...", parseError);
        try {
          const sanitizedString = rawJsonString.replace(/(?<=:\s*"[^"]*)\n(?=[^"]*")/g, "\\n");
          parsedData = JSON.parse(sanitizedString);
        } catch (e) {
          console.error("Sanitization parsing failed:", e);
        }
      }
    }

    let isFallback = false;
    if (!parsedData) {
      isFallback = true;
      parsedData = {
        JobTitle: "Extracted_Job",
        Company: "Job_Board",
        RawOutput: result,
        ParseWarning: "Model output contained invalid JSON formatting, raw text preserved."
      };
    }

    // Stamped from chrome.tabs, not the extraction prompt -- page text
    // usually doesn't contain its own URL anyway.
    parsedData.SourceURL = sourceUrl || null;

    // Our own wall-clock measurement (DOM scrape + inference + parse),
    // not something the model could report on itself.
    parsedData.ExtractionRuntimeMs = Date.now() - extractionStartedAt;

    // harvester_adapter.py maps these into JDInput.intake_trigger_id /
    // intake_enqueued_at for py_file_watcher.py's Metric A.
    if (intakeMeta) {
      parsedData.IntakeTriggerId = intakeMeta.intakeTriggerId;
      parsedData.IntakeEnqueuedAt = intakeMeta.intakeEnqueuedAt;
    }

    // Sends straight to CVTailor's intake stage over native messaging --
    // no Downloads-folder file, no manual move.
    await sendToCVTailorIntake(parsedData, isFallback, tabId);

  } catch (error) {
    console.error("AI Processing failed:", error);
    updateToast(tabId, `❌ Error: ${error.message || "Processing failed"}`, true);
  }
}

function sendToCVTailorIntake(parsedData, isFallback, tabId) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connectNative('com.thesistoolkit.cvtailor_intake');
    } catch (error) {
      updateToast(tabId, `❌ Native host connection failed: ${error.message}`, true);
      resolve();
      return;
    }

    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutId);
      resolve();
    };

    // Chrome's native messaging can silently wedge: connectNative() succeeds
    // but the port NEVER fires onMessage or onDisconnect. Observed firsthand
    // -- a trigger that hit this froze pollInFlight forever, since pollOnce()
    // itself never returned, silently disabling ALL future polling with no
    // error anywhere. This timeout is the hard ceiling that guarantees this
    // promise (and therefore pollOnce) always eventually resolves.
    const timeoutId = setTimeout(() => {
      console.error("Native host call timed out after 20s -- treating as failed.");
      updateToast(tabId, `❌ Native host timed out (no response after 20s)`, true);
      try { port.disconnect(); } catch (e) { /* already gone */ }
      finish();
    }, 20000);

    let gotResponse = false;

    port.onMessage.addListener((response) => {
      gotResponse = true;
      if (response.ok) {
        const msg = isFallback
          ? `⚠️ Sent (with warnings) to CVTailor -> app_id: ${response.app_id}`
          : `✅ Sent to CVTailor -> app_id: ${response.app_id}`;
        updateToast(tabId, msg, isFallback);
      } else {
        updateToast(tabId, `❌ CVTailor intake error: ${response.error}`, true);
      }
      port.disconnect();
    });

    // Fires after EVERY disconnect, including the clean one triggered by our
    // own port.disconnect() above -- only treat it as an error if we never
    // got a response first.
    port.onDisconnect.addListener(() => {
      if (!gotResponse && chrome.runtime.lastError) {
        console.error("Native host disconnected:", chrome.runtime.lastError);
        updateToast(
          tabId,
          `❌ Native host error: ${chrome.runtime.lastError.message} ` +
          `(is it installed? see extension/native_host/README.md)`,
          true
        );
      }
      finish();
    });

    port.postMessage(parsedData);
  });
}

function updateToast(tabId, message, isError = false) {
  if (!tabId) return;
  chrome.scripting.executeScript({
    target: { tabId: tabId },
    func: (msg, err) => {
      const toast = document.getElementById('job-harvester-toast');
      if (toast) {
        toast.style.background = err ? '#7f1d1d' : '#064e3b';
        toast.style.borderColor = err ? '#ef4444' : '#22c55e';
        toast.style.borderWidth = '2px';

        // Text-only update -- a full innerHTML replace would wipe the close button.
        const msgSpan = toast.querySelector('.job-harvester-toast-msg');
        if (msgSpan) {
          msgSpan.textContent = msg;
        } else {
          toast.innerHTML = `<span class="job-harvester-toast-msg">${msg}</span>`;
        }
        if (!toast.querySelector('button')) {
          const closeBtn = document.createElement('button');
          closeBtn.textContent = '×';
          closeBtn.setAttribute('aria-label', 'Dismiss');
          closeBtn.style.cssText = `
            position: absolute; top: 6px; right: 8px; background: transparent;
            border: none; color: #ecfdf5; font-size: 18px; line-height: 1;
            cursor: pointer; padding: 2px 4px; opacity: 0.7;
          `;
          closeBtn.onmouseenter = () => { closeBtn.style.opacity = '1'; };
          closeBtn.onmouseleave = () => { closeBtn.style.opacity = '0.7'; };
          closeBtn.onclick = () => toast.remove();
          toast.appendChild(closeBtn);
        }

        // No auto-dismiss -- sits until you click × to close it.
      }
    },
    args: [message, isError]
  });
}


// Polls the local trigger server, dispatches by `action` name, then acks
// it. Requires "http://127.0.0.1/*" in manifest.json's host_permissions.
 
const TRIGGER_SERVER = "http://127.0.0.1:8765";
const POLL_INTERVAL_MS = 3000;
 
// Map frontmatter `action` names -> handler functions.
// Add a new case any time you add a new location in config.yaml.
const ACTION_HANDLERS = {
  save_job_details: async (trigger) => {
    const tabId = await loadInSharedWindow(trigger.url);
    // Runs the SAME extraction path as the popup/shortcut. trigger.id /
    // created_at ride along as IntakeTriggerId/IntakeEnqueuedAt.
    await runExtraction(tabId, trigger.url, {
      intakeTriggerId: trigger.id,
      intakeEnqueuedAt: trigger.created_at,
    });
  },

  archive_application: async (trigger) => {
    // e.g. mark an existing tracked application as archived — may not need a tab at all
    console.log("Archiving application, meta:", trigger.meta);
  },

  scrape_comp_data: async (trigger) => {
    const tabId = await loadInSharedWindow(trigger.url);
    await chrome.tabs.sendMessage(tabId, { type: "RUN_COMP_SCRAPE" });
  },
};

// ---------------------------------------------------------------------------
// ONE reused background window for all auto-triggered extractions, rather
// than spawning a new window per trigger. (A prior version created a fresh
// window every poll and only closed it on success -- a trigger that kept
// failing kept retrying every 3s with NOTHING to stop it, spawning a new
// window each time until Chrome nearly took the machine down. Never again:
// there is exactly one window, created lazily once, and every extraction
// just navigates its single tab.)
// ---------------------------------------------------------------------------
let sharedWindowId = null;
let sharedTabId = null;

// Chrome requires new window bounds to be >=50% within SOME visible screen
// space (an arbitrary off-screen coordinate throws "Invalid value for
// bounds"), so on a multi-monitor setup we place it on a non-primary
// display -- it'll physically appear there, just not in front of you.
async function pickWindowBounds() {
  try {
    const displays = await chrome.system.display.getInfo();
    const secondary = displays.find((d) => !d.isPrimary) || displays[0];
    const b = secondary.workArea || secondary.bounds;
    return { left: b.left + 20, top: b.top + 20, width: Math.min(1280, b.width - 40), height: Math.min(900, b.height - 40) };
  } catch (e) {
    // system.display unavailable (e.g. some Linux setups) -- fall back to
    // whatever Chrome's default placement is rather than failing the run.
    return {};
  }
}

// Background tab (active: false) is render-throttled by Chrome -- and heavy
// SPAs like LinkedIn additionally gate their own hydration on the Page
// Visibility API -- so scraping it right after 'complete' often grabs an
// unrendered shell (promo banners, empty description arrays) instead of the
// real page. An unfocused window's ACTIVE tab is not considered hidden by
// Chrome, so it renders normally without ever stealing focus.
async function getSharedWindow() {
  if (sharedWindowId !== null) {
    try {
      await chrome.windows.get(sharedWindowId);
      return { windowId: sharedWindowId, tabId: sharedTabId };
    } catch (e) {
      // User closed it (or it never existed) -- fall through and recreate.
      sharedWindowId = null;
      sharedTabId = null;
    }
  }
  const bounds = await pickWindowBounds();
  const win = await chrome.windows.create({ url: "about:blank", focused: false, type: "normal", ...bounds });
  sharedWindowId = win.id;
  sharedTabId = win.tabs[0].id;
  return { windowId: sharedWindowId, tabId: sharedTabId };
}

async function loadInSharedWindow(url) {
  const { tabId } = await getSharedWindow();
  await chrome.tabs.update(tabId, { url });
  await waitForTabComplete(tabId);
  await waitForContentReady(tabId);
  return tabId;
}

function waitForTabComplete(tabId) {
  return new Promise((resolve) => {
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// 'complete' only means the network load finished, not that a client-side
// SPA has hydrated real content in yet. Poll body text length until it looks
// like a real page (or give up after ~6s and proceed anyway).
async function waitForContentReady(tabId, { minChars = 500, attempts = 8, delayMs = 750 } = {}) {
  for (let i = 0; i < attempts; i++) {
    try {
      const [{ result: length }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => (document.body?.innerText || "").trim().length,
      });
      if (length >= minChars) return;
    } catch (e) {
      // Tab may still be navigating -- ignore and retry.
    }
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
}
 
// trigger.id -> { attempts, nextRetryAt }. In-memory only (resets on
// service worker restart) -- fine, since its only job is to stop a broken
// trigger from being retried every single 3s tick forever. Capped backoff,
// not unlimited retries: a trigger that just won't succeed still gets
// retried, but slowly, instead of hammering the same failure in a tight
// loop (which is what turned one bad trigger into 38 Chrome processes).
const triggerBackoff = new Map();
const MAX_BACKOFF_MS = 5 * 60 * 1000;

// A single extraction (page load + content-ready wait + Gemini Nano
// inference + native host round trip) routinely takes far longer than the
// 3s alarm interval. Without this guard, chrome.alarms just keeps firing
// pollOnce() again on top of the still-running one -- and since a trigger
// isn't marked "done" until it fully finishes, EVERY overlapping call sees
// the same still-pending trigger and starts processing it AGAIN, each racing
// to create/grab the shared window. That's what caused windows to pile up
// every 2-3 seconds even after switching to a single reused window: the
// window was reused fine within one pollOnce() call, but 4-7 overlapping
// calls each thought they were the only one running.
let pollInFlight = false;

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      (v) => { clearTimeout(timer); resolve(v); },
      (e) => { clearTimeout(timer); reject(e); },
    );
  });
}

async function pollOnce() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const res = await fetch(`${TRIGGER_SERVER}/poll`);
    if (!res.ok) return;
    const { pending } = await res.json();

    for (const trigger of pending) {
      const backoff = triggerBackoff.get(trigger.id);
      if (backoff && Date.now() < backoff.nextRetryAt) continue;

      const meta = JSON.parse(trigger.meta || "{}");
      const handler = ACTION_HANDLERS[trigger.action];

      if (!handler) {
        console.warn(`No handler for action "${trigger.action}", skipping.`);
        continue;
      }

      try {
        // Outer ceiling on top of the native-host-specific one -- covers a
        // hang ANYWHERE in the chain (tab load, content-ready wait, Gemini
        // Nano inference), not just native messaging. A hung handler here
        // means pollInFlight never clears and NOTHING ever gets processed
        // again, so this must never be allowed to wait forever.
        await withTimeout(handler({ ...trigger, meta }), 90000, `trigger #${trigger.id}`);
        await fetch(`${TRIGGER_SERVER}/ack/${trigger.id}`, { method: "POST" });
        triggerBackoff.delete(trigger.id);
      } catch (err) {
        console.error(`Handler for trigger #${trigger.id} failed:`, err);
        // Deliberately not acking on failure — it'll retry, but with
        // growing backoff (3s, 6s, 12s, ... capped at 5min) instead of
        // every poll tick.
        const attempts = (backoff?.attempts || 0) + 1;
        const delay = Math.min(POLL_INTERVAL_MS * 2 ** attempts, MAX_BACKOFF_MS);
        triggerBackoff.set(trigger.id, { attempts, nextRetryAt: Date.now() + delay });
      }
    }
  } catch (err) {
    // Local server not running (PC asleep, task not started yet) — normal, just skip.
  } finally {
    pollInFlight = false;
  }
}

// chrome.alarms survives service worker suspension better than setInterval.
chrome.alarms.create("poll-job-pipeline", { periodInMinutes: POLL_INTERVAL_MS / 60000 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "poll-job-pipeline") pollOnce();
});
