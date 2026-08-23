# Browser AI Prompts (Personal Reference)

This directory is an unpublished, personal reference folder. It contains prompts designed to be pasted directly into web-based LLMs (like Gemini) to speed up the manual parts of your job search workflow. 

## Keeping Your Prompts Synced

Instead of manually maintaining your salary requirements or target roles in a static text file, this folder includes a script to keep your pre-screening prompt synchronized with your actual pipeline configuration.

*   **`generate_gemini_prompt.py`**: Run this script anytime you update `config/applicant_info.json`'s `job_scoring` section. It pulls your target roles, minimum salary, and preferred firms directly from that config (the same source `src/stages/py_stage01_score_jd_input.py`'s real scorer reads) and dynamically rebuilds the `Gemini JD Eval Prompt.txt` file. This ensures your web prompt never drifts from your core pipeline logic. Errors clearly if `job_scoring` isn't configured yet -- see `config/applicant_info.example.json` for the shape.

## The Prompts

*   **`Gemini JD Eval Prompt.txt`**: Use this to pre-screen a job posting *before* you bother feeding it into the CVTailor pipeline. Paste this into Gemini along with a job description to get a quick "APPLY / MAYBE / SKIP" verdict based on your strict personal criteria (like your base salary floor, Philadelphia-area hybrid limits, and zero-micromanagement preferences). 
*   **`Gemini Resume Cover Letter Edit Flow Initiate Prompt.txt`**: A highly specific system prompt for interactive editing. Paste this into a web LLM when you want to manually review and tweak the pipeline's generated edits in the browser rather than the CLI. It instructs the model to provide specific, single-character feedback flags like `(a)`, `(r)`, `(e)`, and `(q)` to streamline your cover letter and resume updates.