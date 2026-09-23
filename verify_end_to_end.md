Pre-Pipeline Execution Steps (unchanged from your plan):
1. Stop the LM Studio server and quit the app.
2. Verify lm_studio.model_id in config/file_watcher.json exactly matches a real identifier from lms ps --json / lms ls.
3. Run util\Test-FileWatcherRunning.ps1. If not up, run util\Register-FileWatcher.ps1, then the test script again.

Pipeline Execution Steps (corrected):
1. Drop the markdown file with the LinkedIn URL into the intake folder.
2. Corrected expectation: a new background tab will appear in the tab strip - it will not jump to foreground or steal your window focus (active: false, by design, so automated intake doesn't yank your attention). Watch the tab strip, not for a window popping up. You should still see the "Capturing Job Details..." toast render inside that tab once extraction starts, and the same success/error toast as manual runs. If you're not watching closely enough to catch the toast, that's exactly what step 4 below is for - don't rely on the toast alone as your only signal.
3. Check applications/ for the new jd_input.json, confirm intake_enqueued_at / intake_trigger_id are populated (not null) - this is the part that genuinely didn't exist before today's fixes, so it's a real test of new code, not just a sanity check.
4. Confirm the pipeline is running on its own: GET /runs 
