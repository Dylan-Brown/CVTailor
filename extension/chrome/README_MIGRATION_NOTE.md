# extension/chrome/

This is the Chrome extension formerly tracked as its own top-level
folder (`Jobs_JDtoJSON`), then consolidated into `jobs/extension/`
directly, then moved here (a `chrome/` subfolder, sibling to
`extension/native_host/`) as part of the src/ restructure. It isn't a
standalone tool -- it only exists to produce the JSON that
`src/stages/py_stage00_normalize_jd_input.py` consumes as its
`--jd-json` input. It has no independent use outside the CVTailor
pipeline.

See py_stage00_normalize_jd_input's docstring for the JSON contract
this extension needs to produce (JDInput's fields, or the Job Data
Harvester's 13-key schema, which stage 0 auto-detects and normalizes).
