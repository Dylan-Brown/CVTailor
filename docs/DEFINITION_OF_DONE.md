# Definition of Done — job application materials

A concrete, checkable gate for "ready to submit," not a vibe. Two
tiers: automated (run a script, ~5 seconds) and human (you, ~60
seconds). Both required, every time, no exceptions for time pressure —
the automated tier costs almost nothing to run, and skipping the
human tier is exactly how the Maven Clinic application (generic
company paragraph, 5-claim ledger, `coverage_score` off by 100x) would
have gone out the door if it hadn't been caught by chance.

## Tier 1 — automated

```powershell
python src\checkpoints\py_post_pipeline_verify_materials.py --app-id <app_id>
python src\checkpoints\py_post_pipeline_verify_materials.py --all
```

Checks, and the real incident that motivated each one:

| Check | Real incident it catches |
|---|---|
| Claims ledger has ≥10 claims | A local-model run silently extracted 5 claims from a full resume (should be 30-50+); nothing caught it because the schema had no floor |
| `coverage_score` is a real 0-1 value | A local model returned `60.0` (meant as "60%") where `0.60` was expected; displayed as `6000%`, schema had no bounds |
| At least one edit was approved | Catches an accidentally-empty review pass producing a functionally-untailored application |
| No leftover placeholders (`<COMPANY_NAME>`, `{{...}}`, etc.) | Self-explanatory — any of these in real output means a swap failed |
| No known-bad leftover phrases (e.g. "Core Technology team") | A claim about a team at a *previous* employer, nonsensically surviving into a new application — happened twice |
| Company name appears ≥3 times in the cover letter | Cheap proxy for "the free-written company paragraph actually engaged with the company," not just the boilerplate Re:/salutation lines |
| Mandatory company paragraph was actually rewritten | The single most important cover-letter edit silently not firing — this is exactly what happened with Maven Clinic |
| Resume ≤ target pages, cover letter ≤ target pages | Page-limit enforcement, checked against the real rendered PDF |
| Resume PDF text-extracts cleanly (name/email found, not garbled) | Simulates how a real ATS's primitive, non-layout-aware PDF parser would read the file -- catches multi-column scrambling, image-based content, encoding issues |
| Required JD keywords present in resume text | Most ATS keyword search is literal, not semantic -- "Kubernetes" in the JD needs "Kubernetes" in the resume, not just "container orchestration" |

A `definition_of_done_report.json` gets written into each app's
folder either way, so you (or a future session) can check what passed
without re-running anything.

**A clean Tier 1 pass is necessary, not sufficient.** It catches
specific, previously-real failure modes — it has no opinion on
whether the letter is actually *good*.

## Tier 2 — human, every time, ~60 seconds

1. Does the "What draws me to [Company]" paragraph name something
   *true and specific* about this company — not just present, but
   actually accurate and not generic filler?
2. Does at least one resume bullet read as genuinely tailored to this
   JD, not just template with a name swapped in?
3. Read the closing sentence out loud. Does it reference the actual
   role, or is it generic?
4. Contact info in the header — name, phone, email, links — all
   correct? (Real bug, already caught once.)
5. Does anything read like it was written by a model rather than by
   you? Voice/tense drift is the thing most likely to slip past
   automated checks (see stage 5's rule 5 in
   `src/stages/py_stage05_propose_updates.py` — present-perfect tense
   preservation is a known trouble spot).

## When Tier 1 fails

Don't hand-patch the specific application and move on — that fixes
this one instance and leaves the underlying cause live for the next
JD. Prefer the real fix in order:

1. **Ledger too thin / coverage_score broken** → the model that ran
   stage 2/3 is unreliable for this task. Re-run that specific stage
   with `--provider claude` (or whichever cloud provider is your
   fallback) rather than accepting a degraded local-model result.
2. **Mandatory paragraph not rewritten** → check `candidate_edits.json`
   for whether stage 5 even proposed the edit; if it did but stage 6
   or 7 killed it, check `discarded_edits.jsonl` /
   `critiqued_out.jsonl` for why — this class of edit should be
   exempt (see `src/core/prose.is_company_specific_edit`), so a
   failure here means that exemption itself needs revisiting.
3. **Known-bad phrase resurfaced** → check
   `config/cover_letter_sample/dylan_brown_cover_letter_new.txt`
   directly. This has reverted at least twice now — if it's back, the
   file itself needs re-fixing, not just the one application.

## Keyword-gap feedback loop

Stage 4 now deterministically checks every JD-required keyword against
the claims ledger (no LLM call) and splits them into two genuinely
different cases -- see `src/core/prose.classify_keyword_gaps`:

- **Actionable** (a real claim already supports the term, it just
  hasn't surfaced into an edit yet) -- fed to stage 5 as a pre-computed
  instruction, since the model no longer has to find the match itself,
  just use it.
- **Unsupported** (no claim mentions the term at all) -- deliberately
  NOT fed back into generation, since there's nothing genuine to build
  an edit from. Surfaced instead in the follow-up-steps .txt file as a
  direct signal to you: either you have real, undocumented experience
  worth adding to your actual resume, or the JD wants something you
  genuinely don't have. Both are useful to know; neither is this
  pipeline's decision to make on your behalf.

## What this deliberately does NOT check

- Whether the JD itself is worth applying to
- Grammar/spelling beyond what the pipeline itself introduces (proofread your baseline documents once; the pipeline mostly preserves your own wording verbatim)
- Salary fit, timing, or anything about the actual decision to apply

Those are judgment calls. This document is about the mechanical,
previously-actually-broken things — the stuff that's boring to check
by hand every time and therefore the stuff most likely to slip
through under real time pressure.
