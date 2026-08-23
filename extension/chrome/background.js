chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "RUN_EXTRACTION") {
    const tabId = request.tabId || sender.tab?.id;
    runExtraction(tabId, request.url);
  }
});

// Fires on the Alt+Shift+E (Option+Shift+E on Mac) shortcut declared in
// manifest.json's "commands" -- runs the same flow as clicking the popup
// button, straight from the active tab, without needing the popup open.
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "extract-job-details") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return;
  runExtraction(tab.id, tab.url);
});

// Shared by the popup button and the keyboard shortcut: injects the
// on-page status toast, pulls the job-posting text out of the tab, and
// hands it to processWithGeminiNano. Lives here (not popup.js) so the
// shortcut path works with the popup never having been opened.
async function runExtraction(tabId, tabUrl, intakeMeta = null) {
  if (!tabId) return;

  // Wall-clock span for the extension's own work: DOM scrape + Gemini
  // Nano inference + JSON parse, ending right before handoff to native
  // messaging (see the ExtractionRuntimeMs stamp in processWithGeminiNano).
  // Not started any earlier than this -- popup-open time, tab-switch
  // time, etc. aren't part of "how long did extraction take."
  const extractionStartedAt = Date.now();

  // intakeMeta is only ever set when this run came from the file
  // watcher's trigger server (see the save_job_details handler) -- the
  // popup button and keyboard-shortcut paths call this with no 3rd arg,
  // so IntakeTriggerId/IntakeEnqueuedAt simply won't be set on parsedData
  // for those, same as py_file_watcher.py's own "absent = metric A not
  // recorded, correct behavior" comment already documents.

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

  processWithGeminiNano(pageText, tabId, tabUrl, extractionStartedAt, intakeMeta);
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

    // Was 7000 -- too tight once real job postings' Requirements/
    // Responsibilities sections are included; that limit combined with
    // whole-page extraction is what caused early runs to come back with
    // empty Requirements/Responsibilities and a mid-sentence-truncated
    // description. Gemini Nano's context window has room for more; this
    // is still a real limit, just a more realistic one -- if extraction
    // is thin again, check the [debug] log line above for how many
    // characters were actually extracted vs. how many made it through.
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

    // The tab URL is known deterministically from chrome.tabs -- no
    // reason to have the model guess at it from page text (which
    // usually doesn't even contain its own URL), so it's stamped in
    // here rather than added to the extraction prompt.
    parsedData.SourceURL = sourceUrl || null;

    // Stamped here, not passed to the model -- this is our own wall-clock
    // measurement, not something Gemini Nano could reliably report on
    // itself. Covers DOM scrape + inference + parse; see the const's own
    // comment in runExtraction() for exactly what's in/out of this span.
    parsedData.ExtractionRuntimeMs = Date.now() - extractionStartedAt;

    // Only set when this run came from an intake trigger -- see
    // runExtraction()'s intakeMeta comment. harvester_adapter.py maps
    // these into JDInput.intake_trigger_id / intake_enqueued_at, which
    // py_file_watcher.py's Metric A reads back out.
    if (intakeMeta) {
      parsedData.IntakeTriggerId = intakeMeta.intakeTriggerId;
      parsedData.IntakeEnqueuedAt = intakeMeta.intakeEnqueuedAt;
    }

    // Previously: JSON.stringify + a data: URL passed to chrome.downloads.download(),
    // landing in the Downloads folder alongside unrelated files. Now: send the parsed
    // object straight to CVTailor's intake stage over native messaging -- no file in
    // Downloads, no manual move, no naming collisions with anything else you download.
    sendToCVTailorIntake(parsedData, isFallback, tabId);

  } catch (error) {
    console.error("AI Processing failed:", error);
    updateToast(tabId, `❌ Error: ${error.message || "Processing failed"}`, true);
  }
}

function sendToCVTailorIntake(parsedData, isFallback, tabId) {
  let port;
  try {
    port = chrome.runtime.connectNative('com.thesistoolkit.cvtailor_intake');
  } catch (error) {
    updateToast(tabId, `❌ Native host connection failed: ${error.message}`, true);
    return;
  }

  port.onMessage.addListener((response) => {
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

  port.onDisconnect.addListener(() => {
    if (chrome.runtime.lastError) {
      console.error("Native host disconnected:", chrome.runtime.lastError);
      updateToast(
        tabId,
        `❌ Native host error: ${chrome.runtime.lastError.message} ` +
        `(is it installed? see extension/native_host/README.md)`,
        true
      );
    }
  });

  port.postMessage(parsedData);
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

        // Update only the message text, not the whole toast -- a full
        // innerHTML replace here would also wipe out the close button,
        // which needs to survive every status update (capturing ->
        // success/error), not just exist at creation time.
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

        // No auto-dismiss timer -- the whole point of this fix is that
        // a quick glance at the tab tells you whether it succeeded,
        // without needing to check the extraction folder on disk. The
        // toast now sits until you actually click the × to close it.
      }
    },
    args: [message, isError]
  });
}


// Polls the local trigger server every few seconds. When a pending trigger
// shows up, dispatches to the matching handler by `action` name, then acks
// it so it isn't processed twice. Requires "http://127.0.0.1/*" (or the
// specific port) in the extension's `host_permissions` in manifest.json.
 
const TRIGGER_SERVER = "http://127.0.0.1:8765";
const POLL_INTERVAL_MS = 3000;
 
// Map frontmatter `action` names -> handler functions.
// Add a new case any time you add a new location in config.yaml.
const ACTION_HANDLERS = {
  save_job_details: async (trigger) => {
    const tab = await chrome.tabs.create({ url: trigger.url, active: false });
    // Wait for the page to finish loading, then run the SAME extraction
    // path the popup button and keyboard shortcut already use -- not a
    // separate content-script message (there's no content script
    // registered to receive one; that would be a dead end every time).
    // trigger.id / trigger.created_at ride along so the resulting
    // jd_input.json can carry them forward as IntakeTriggerId /
    // IntakeEnqueuedAt -- see runExtraction()'s intakeMeta param.
    await waitForTabComplete(tab.id);
    await runExtraction(tab.id, trigger.url, {
      intakeTriggerId: trigger.id,
      intakeEnqueuedAt: trigger.created_at,
    });
  },
 
  archive_application: async (trigger) => {
    // e.g. mark an existing tracked application as archived — may not need a tab at all
    console.log("Archiving application, meta:", trigger.meta);
  },
 
  scrape_comp_data: async (trigger) => {
    const tab = await chrome.tabs.create({ url: trigger.url, active: false });
    await waitForTabComplete(tab.id);
    await chrome.tabs.sendMessage(tab.id, { type: "RUN_COMP_SCRAPE" });
  },
};
 
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
 
async function pollOnce() {
  try {
    const res = await fetch(`${TRIGGER_SERVER}/poll`);
    if (!res.ok) return;
    const { pending } = await res.json();
 
    for (const trigger of pending) {
      const meta = JSON.parse(trigger.meta || "{}");
      const handler = ACTION_HANDLERS[trigger.action];
 
      if (!handler) {
        console.warn(`No handler for action "${trigger.action}", skipping.`);
        continue;
      }
 
      try {
        await handler({ ...trigger, meta });
        await fetch(`${TRIGGER_SERVER}/ack/${trigger.id}`, { method: "POST" });
      } catch (err) {
        console.error(`Handler for trigger #${trigger.id} failed:`, err);
        // Deliberately not acking on failure — it'll retry next poll.
        // Add a retry-count / dead-letter check here if that becomes noisy.
      }
    }
  } catch (err) {
    // Local server not running (PC asleep, task not started yet) — normal, just skip.
  }
}
 
// chrome.alarms survives service worker suspension better than setInterval.
chrome.alarms.create("poll-job-pipeline", { periodInMinutes: POLL_INTERVAL_MS / 60000 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "poll-job-pipeline") pollOnce();
});
