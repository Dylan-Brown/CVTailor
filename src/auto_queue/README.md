# src/auto_queue/

Optional always-on automation -- nothing here is required for the normal
manual/batch workflow.

* **`py_file_watcher.py`** — background watcher (see `util/Register-FileWatcher.ps1`
  to run it as a Windows Scheduled Task) that automatically queues new JDs
  the moment they land and syncs generated materials, complete with
  LM Studio warm-up and cloud-drive syncing.
* **`ensure_lm_studio_ready.py`** — auto-starts LM Studio and confirms the
  configured model is fully loaded before the pipeline triggers, so a
  cold LM Studio instance doesn't cause the first real request to fail
  or hang.
