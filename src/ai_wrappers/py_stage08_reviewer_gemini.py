"""
py_stage08_reviewer_gemini.py -- the "Agent" backend for stage 8. Drives
src/stages/py_stage08_agentic_update_review.py (the human review gate)
automatically, by watching its stdout for prompts and answering them the
way a careful reviewer would, then feeding it answers over stdin.
py_stage08_agentic_update_review.py itself is untouched by this -- it's
still the same interactive tool, still safe to run by hand (the "User"
backend -- see py_stage08_reviewer_interface.py, which picks between the
two based on pipeline.yaml's Review Stage Assignee setting).

What it actually decides, per edit:
  - stage8 already labeled it "APPROVE" (cleared fact-check + utility
    review, see show_edit() in py_stage08_agentic_update_review.py) ->
    approved as-is, text untouched. Nothing new or unverified is
    introduced here.
  - stage8 flagged it ("REVIEW CAREFULLY" or "REJECT") -> a replacement
    is generated via Gemini, grounded in the app's real JD, company
    research, and claims ledger (not a generic "sound impressive"
    prompt), then run back through stage 6's OWN verify_one() -- the
    exact fact-checker every other edit in this pipeline is gated by --
    before it's ever typed into the terminal. Rejected verdicts feed
    their reasoning back to Gemini for another attempt (--max-attempts,
    default 3); if nothing grounded comes out, the edit is rejected
    outright rather than ever submitting unverified text.

Context loading (JD/company brief/claims ledger) happens ONCE per
application, cached and reused across every edit reviewed for that
app -- not re-read per edit, which would just be redundant I/O and
redundant LLM context for content that hasn't changed between one
edit and the next in the same review batch.

Usage: identical to py_stage08_agentic_update_review.py -- everything
not recognized by this script is forwarded straight through.
    python src/ai_wrappers/py_stage08_reviewer_gemini.py --app-id <jd_name_or_prefix>
    python src/ai_wrappers/py_stage08_reviewer_gemini.py --force
    python src/ai_wrappers/py_stage08_reviewer_gemini.py
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from google import genai

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/ai_wrappers/ -> src/ -> jobs/
STAGES_DIR = JOBS_ROOT / "src" / "stages"
sys.path.insert(0, str(JOBS_ROOT / "src" / "core"))
sys.path.insert(0, str(STAGES_DIR))

import py_stage08_agentic_update_review as stage8  # noqa: E402
import py_stage06_fact_check_updates as stage6  # noqa: E402
from io_utils import load_model  # noqa: E402
from llm_dispatch import default_provider  # noqa: E402
from schemas import ClaimsLedger  # noqa: E402
from batch_common import APPLICATIONS_ROOT, CONFIG_ROOT, slugify_variant  # noqa: E402

client = genai.Client()

# Matches the header line show_edit()'s caller prints for every edit --
# "\n\n[{app_label} / {edit_id}] {section}{flag}" -- both to know which
# app we're in (so context loads/caches correctly across an auto-
# discover run spanning several apps) and to look up the FULL structured
# edit dict by id, rather than trying to re-derive target/edit_type/
# claim_ids_referenced/etc. by scraping the diff text stage8 prints.
EDIT_HEADER_RE = re.compile(r"\[(?P<app>[^/\]]+) / (?P<edit_id>[^\]]+)\]")


def load_app_context(app_dir: Path, provider: str) -> dict:
    """JD, company research, and the correct resume-variant claims
    ledger for ONE application. Called once per app (the caller caches
    by app name) and reused for every edit reviewed in that app --
    reading these same three files and re-deriving the same resume-
    variant routing fresh per edit would just be redundant I/O for
    content that's identical across every edit in one review batch."""
    jd_path = app_dir / "jd_input.json"
    jd = json.loads(jd_path.read_text(encoding="utf-8")) if jd_path.is_file() else {}

    company_brief_text, company_name = stage8._company_brief_text(app_dir)

    ledger_by_id: dict = {}
    routing_path = app_dir / "routing.json"
    if routing_path.is_file():
        try:
            variant = json.loads(routing_path.read_text(encoding="utf-8")).get("resume_variant", "")
            slug = slugify_variant(variant) if variant else ""
            ledger_path = APPLICATIONS_ROOT / "_shared" / "variants" / slug / "claims_ledger.json"
            if slug and ledger_path.is_file():
                ledger = load_model(ledger_path, ClaimsLedger)
                ledger_by_id = {c.claim_id: c for c in ledger.claims}
        except Exception:
            pass

    cover_letter_baseline_text = ""
    cl_sample_dir = CONFIG_ROOT / "cover_letter_sample"
    if cl_sample_dir.is_dir():
        txt_files = list(cl_sample_dir.glob("*.txt"))
        if txt_files:
            cover_letter_baseline_text = txt_files[0].read_text(encoding="utf-8")

    edits_by_id: dict = {}
    edits_path = stage8.edits_file_for(app_dir)
    if edits_path.is_file():
        try:
            data = json.loads(edits_path.read_text(encoding="utf-8"))
            for e in data.get("passed", []) + data.get("flagged", []):
                edits_by_id[e["edit_id"]] = e
        except Exception:
            pass

    return {
        "app_dir": app_dir,
        "jd": jd,
        "company_brief_text": company_brief_text,
        "company_name": company_name,
        "ledger_by_id": ledger_by_id,
        "cover_letter_baseline_text": cover_letter_baseline_text,
        "edits_by_id": edits_by_id,
        "provider": provider,
    }


def generate_dynamic_edit(edit: dict, ctx: dict, feedback: str = "") -> str:
    """Asks Gemini for replacement text grounded in the real claims this
    edit is allowed to draw from (not a generic 'sound impressive'
    prompt) -- the same claim texts stage 6's own verifier will check
    the result against a moment later, so generation and verification
    are reading from the same ground truth instead of two different
    ideas of what's true."""
    claim_ids = edit.get("claim_ids_referenced") or []
    source_claims = [ctx["ledger_by_id"][cid].text for cid in claim_ids if cid in ctx["ledger_by_id"]]

    parts = [f"Role: {ctx['jd'].get('role_title', '')} at {ctx['jd'].get('company', '')}"]
    if ctx["company_brief_text"]:
        parts.append(f"Researched company context (only source for any company-specific claim):\n{ctx['company_brief_text']}")
    if source_claims:
        parts.append(
            "Source claims this edit MUST stay strictly within -- do not add any fact, "
            "number, or technology not present here:\n" + "\n".join(f"- {c}" for c in source_claims)
        )
    else:
        parts.append(
            "This edit references no candidate claim -- it is company/role-specific content "
            "(e.g. a 'why this company' paragraph or closing sentence), not a factual claim "
            "about the candidate, so stay generic/professional rather than inventing candidate facts."
        )
    if edit.get("original_text"):
        parts.append(f"Original text being reworded (stay close in length -- this is an in-place reword, not a restructure):\n{edit['original_text']}")
    parts.append(f"Section: {edit.get('section', '')}")
    if feedback:
        parts.append(f"IMPORTANT -- your previous attempt was rejected: {feedback}")

    prompt = f"""You are an expert technical resume and cover letter writer.

{chr(10).join(parts)}

Task: Write the actual, polished replacement text for this specific resume bullet or cover
letter section, grounded ONLY in the source claims / context given above. Never invent a
fact, number, technology, or outcome that isn't already there.
Guidelines:
- Maintain a professional, confident tone.
- CRITICAL: Output ONLY the final replacement text itself -- no conversational filler, no
  markdown, no explanations.
"""
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text.strip().replace('"', "")


def generate_and_verify_edit(edit: dict, ctx: dict, max_attempts: int) -> str | None:
    """Generates via Gemini, then re-runs stage 6's OWN verify_one() --
    not a re-implementation, the actual function every other edit in
    this pipeline is gated by -- against the result before it's ever
    proposed. A rejected verdict's reasoning is fed back for another
    attempt; returns None (never a guess) if nothing grounded comes out
    after max_attempts, so the caller can reject the edit instead."""
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        suggested = generate_dynamic_edit(edit, ctx, feedback)
        candidate = {**edit, "suggested_text": suggested}
        result = stage6.verify_one(candidate, ctx["ledger_by_id"], ctx["provider"], ctx["cover_letter_baseline_text"])
        if result.verdict == "supported":
            print(f"[Driver Automation] Verified 'supported' on attempt {attempt}/{max_attempts}.")
            return suggested
        print(f"[Driver Automation] Attempt {attempt}/{max_attempts} verdict={result.verdict!r}: "
              f"{result.reasoning}")
        feedback = f"{result.reasoning} Unsupported spans: {result.unsupported_spans}"
    print(f"[Driver Automation] No grounded replacement found for [{edit.get('edit_id')}] after "
          f"{max_attempts} attempt(s) -- rejecting rather than submitting unverified text.")
    return None


def decide_action(item_text: str, edit: dict | None, ctx: dict | None, max_attempts: int) -> tuple[str, str | None]:
    """Reads the SAME suggestion show_edit() already computed and
    printed (Suggested: APPROVE / REVIEW CAREFULLY / REJECT) rather than
    re-deriving that verdict here too -- stage8_review.py is the single
    source of truth for it. APPROVE -> approved untouched. Anything
    flagged -> generate + verify a replacement, or reject if that never
    produces something grounded."""
    if edit is None or ctx is None:
        # Couldn't resolve the structured edit/context -- never guess.
        return "r", None
    if "Suggested: APPROVE" in item_text:
        return "a", None
    replacement = generate_and_verify_edit(edit, ctx, max_attempts)
    if replacement is None:
        return "r", None
    return "e", replacement


def run_stage8_automation(passthrough_args: list, provider: str, max_attempts: int):
    cmd = [sys.executable, str(STAGES_DIR / "py_stage08_agentic_update_review.py")] + passthrough_args
    print(f"[Driver] Launching review pipeline: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(JOBS_ROOT),
    )

    output_buffer = ""
    item_buffer = ""
    context_cache: dict[str, dict] = {}
    current_ctx: dict | None = None
    current_edit: dict | None = None
    current_edit_id: str | None = None
    pending_text: str | None = None

    try:
        while True:
            char = process.stdout.read(1)
            if not char and process.poll() is not None:
                break
            if not char:
                continue

            try:
                sys.stdout.write(char)
                sys.stdout.flush()
            except UnicodeEncodeError:
                sys.stdout.write("?")
                sys.stdout.flush()

            output_buffer += char
            item_buffer += char

            edit_match = EDIT_HEADER_RE.search(output_buffer)
            if edit_match:
                app_label = edit_match.group("app")
                if current_ctx is None or app_label != current_ctx["app_dir"].name:
                    if app_label not in context_cache:
                        print(f"\n[Driver] Loading context for {app_label} (JD, company "
                              f"research, claims ledger) -- once for this app, reused for "
                              f"every edit in it.")
                        context_cache[app_label] = load_app_context(APPLICATIONS_ROOT / app_label, provider)
                    current_ctx = context_cache[app_label]
                current_edit_id = edit_match.group("edit_id")
                current_edit = current_ctx["edits_by_id"].get(current_edit_id)
                item_buffer = output_buffer[edit_match.end():]

            if "(a)pprove / (r)eject / (e)dit / (q)uit:" in output_buffer:
                action, text = decide_action(item_buffer, current_edit, current_ctx, max_attempts)
                print(f"\n[Driver Automation] Decision for [{current_edit_id}]: "
                      f"{ {'a': 'approve', 'e': 'edit', 'r': 'reject'}[action] }")
                time.sleep(1.0)
                pending_text = text
                process.stdin.write(action + "\n")
                process.stdin.flush()
                output_buffer = ""
                item_buffer = ""

            elif "New text:" in output_buffer:
                print(f"[Driver Automation] Submitting verified replacement -> {pending_text}")
                time.sleep(1.0)
                process.stdin.write((pending_text or "") + "\n")
                process.stdin.flush()
                output_buffer = ""

            elif "Use this text? (y)es / (r)e-type:" in output_buffer:
                # Safe to always confirm here -- unlike the original
                # naive driver, the text being confirmed was already
                # run through stage 6's verify_one() before it was ever
                # typed in above, not blindly trusted after the fact.
                time.sleep(1.0)
                process.stdin.write("y\n")
                process.stdin.flush()
                output_buffer = ""

            elif "Bulk-accept those" in output_buffer and "(y)es / (n)o" in output_buffer:
                print("\n[Driver Automation] Bulk-accepting clean edits (already cleared "
                      "fact-check + utility review -- nothing new/unverified here)...")
                time.sleep(1.0)
                process.stdin.write("y\n")
                process.stdin.flush()
                output_buffer = ""

    except KeyboardInterrupt:
        print("\n[Driver] Interrupted by user. Terminating process...")
        process.terminate()

    rc = process.wait()
    print(f"\n[Driver] Review session exited with code {rc}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Drives src/stages/py_stage08_agentic_update_review.py automatically. "
                     "Approves edits stage 8 already labeled clean, and for flagged edits, "
                     "generates a replacement grounded in the app's real JD/company research/"
                     "claims ledger and verifies it via stage 6's own fact-checker before ever "
                     "proposing it -- falls back to rejecting rather than submitting unverified "
                     "text. All other arguments (--app-id, --force, --max-edits, ...) are "
                     "forwarded to py_stage08_agentic_update_review.py unchanged."
    )
    ap.add_argument("--provider", default=default_provider(), choices=["lmstudio", "claude", "gemini"],
                     help="Provider for the stage-5 verification step (generation always uses "
                          "Gemini directly). Defaults to $CVTAILOR_LLM_PROVIDER, or 'lmstudio' if unset.")
    ap.add_argument("--max-attempts", type=int, default=3,
                     help="Retries per flagged edit, feeding the verifier's rejection reason "
                          "back to Gemini each time, before giving up and rejecting the edit "
                          "instead of ever submitting unverified text (default: 3).")
    known, passthrough_args = ap.parse_known_args()
    run_stage8_automation(passthrough_args, provider=known.provider, max_attempts=known.max_attempts)
