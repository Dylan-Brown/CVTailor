# Goals & Benchmarks

Throughput and quality targets — not a checklist (see `DEFINITION_OF_DONE.md` for that).

## Throughput

- Applications prepared per week: track via `python src/util/py_applications_stats.py --archive`
- Resume variant split (see `config/resume_variants/`): `python src/util/py_applications_stats.py --variants`

## Quality

- Definition of Done pass rate (Tier 1 automated): should be 100% before send, every time — no target below that.
- Resume edits per app: 0 is a red flag (`py_pipeline_status.py` / `py_pipeline_prereview_summary.py` surface this).
- Coverage score (stage 4): no fixed target — a low score on a real-gap job is honest, not a failure.

## Review

Numbers above are signals, not gates on their own — a slow week chasing three strong-fit roles can beat a fast week of fifteen weak ones. Use these to notice drift, not to optimize blindly toward.
