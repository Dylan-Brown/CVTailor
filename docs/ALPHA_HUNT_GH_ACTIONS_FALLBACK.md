# GitHub Actions fallback for the ATS poller (not enabled)

`src/sourcing/py_poll_ats_boards.py` runs locally by default (Windows
Task Scheduler, same mechanism as `util/Register-FileWatcher.ps1`). This
document exists so that if a target ATS starts rate-limiting or blocking
the local poller's IP, there's already a worked-out path to move *just
the discovery step* to a scheduled GitHub Actions run instead -- the way
[alpha-hunt](https://github.com/mr-ravi26/alpha-hunt) itself does it.

**This is deliberately not turned on by default.** Doing so changes the
trust boundary this pipeline has run under so far: your curated
`config/target_companies.yaml` and `config/applicant_info.json` would
need to be readable by a GitHub Actions runner, which in practice means
committing them (or their contents as repo secrets) somewhere GitHub can
reach -- a different exposure than "stays on my PC." Flip this on only as
a deliberate decision when the local poller is actually blocked, not
preemptively.

## What would need to move

Only the **discovery** step, not tailoring or submission:

- `src/sourcing/ats_boards.py` and `src/sourcing/py_poll_ats_boards.py`
  have no dependency on the rest of the pipeline besides `schemas.py`
  (`JDInput`) and `batch_common.py`'s path constants -- they can run
  standalone against just a company list + keyword list.
- Everything downstream (tailoring, review, assembly, submission) still
  needs your full resume/claims/credentials and stays local. Discovery
  running in the cloud does **not** imply the rest of the pipeline does.

## Sketch, adapted from alpha-hunt's `.github/workflows/job-search.yml`

1. A **separate, private** repo (or a private mirror/subtree of just
   `src/sourcing/`) -- never the public `Dylan-Brown/CVTailor` repo --
   holding:
   - `src/sourcing/ats_boards.py`, `py_poll_ats_boards.py`, and their
     `src/core` dependencies (`schemas.py`, `batch_common.py`, `io_utils.py`).
   - `config/target_companies.yaml` and the `job_scoring.target_titles`
     list (as GitHub Actions **secrets**, injected at runtime -- not
     committed as plaintext files, unlike alpha-hunt's own
     "commit your filled-in config.yaml" approach).
2. A workflow file scheduled the same way alpha-hunt's is:
   ```yaml
   on:
     schedule:
       - cron: "*/15 * * * *"
   ```
3. Instead of writing straight to a local `applications/` folder (which
   doesn't exist on the runner), the workflow would need a notification
   step -- e.g. commit the new JD JSON to a `pending-intake/` folder in
   that same private repo, or push a Telegram/email notification the way
   alpha-hunt's `notify.py` does -- and a separate local step (or the
   file watcher) pulls those down into the real `applications/` folder
   the next time you're at your PC.
4. `run_pipeline.py`'s `drain_raw_intake()` needs no changes either way --
   it already picks up any raw JD JSON dropped into `applications/`,
   regardless of whether a local poller or a synced-down cloud poller put
   it there.

## When to actually do this

Only if `py_poll_ats_boards.py`'s logs (`log/ats_poll.jsonl`) show a
specific target ATS consistently failing/blocking from your home IP. At
that point, revisit this document, decide on the secrets-handling
approach above, and treat enabling it as its own reviewed change -- not
a default to fall back into quietly.
