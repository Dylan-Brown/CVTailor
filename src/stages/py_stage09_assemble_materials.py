#!/usr/bin/env python3
"""
py_stage09_assemble_materials.py — Stage 9: Document Assembly
=============================================================
Applies approved edits to your resume `.docx` template using formatting-preserving, 
table-aware text replacement. Generates the final cover letter and converts both to PDF.

Includes an interactive review loop for the cover letter:
    (a)ccept   — Build the document as-is.
    (m)anual   — Open the text in Notepad for manual tweaking.
    (f)eedback — Send a note to the LLM to rewrite the letter based on your critique.

Usage:
    python py_stage09_assemble_materials.py \
        --resume-template path/to/template.docx \
        --review-decisions path/to/decisions.json \
        --verified-edits path/to/verified.json \
        --out-dir path/to/output/ \
        --company "Acme" --role-title "Cloud Architect" --applicant-name "Dylan Brown"
"""
import argparse
import json
import sys
import time
import subprocess
import os
from pathlib import Path

_JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/stages/ -> src/ -> jobs/
sys.path.append(str(_JOBS_ROOT / "src" / "core"))
sys.path.append(str(_JOBS_ROOT / "src" / "prompt_library"))
from io_utils import log_event, save_model, append_jsonl
from docx_utils import docx_replace_text, iter_all_paragraphs
from llm_dispatch import call_llm_structured, default_provider, CLAUDE_MODELS, GEMINI_MODELS
from text_matching import _normalize_quotes, find_edit_match
from prose import simplify_role_title_for_prose
from py_prompt_template_handler import load_prompt
from schemas import AssemblyLogEntry


def _kill_word_processes():
    """Forcefully kills any lingering WINWORD.EXE processes on Windows to prevent COM locking."""
    if os.name == 'nt':
        try:
            # /F is force, /IM is image name, /T kills child processes
            subprocess.run(["taskkill", "/F", "/IM", "WINWORD.EXE", "/T"], capture_output=True, text=True)
        except Exception as e:
            print(f"  [Debug] Could not execute taskkill: {e}", file=sys.stderr)


def apply_edits_to_docx(template_path: str, edits_by_id: dict, out_path: str, dry_run: bool) -> tuple[int, list[str]]:
    import docx
    doc = docx.Document(template_path)

    applied = 0
    unmatched = []
    remaining = {eid: e for eid, e in edits_by_id.items()}

    for para in iter_all_paragraphs(doc):
        for eid, edit in list(remaining.items()):
            original = (edit.get("original_text") or "").strip()
            if not original:
                continue
            if docx_replace_text(para, original, edit["final_text"]):
                applied += 1
                del remaining[eid]

    if remaining:
        for eid, e in remaining.items():
            original = (e.get("original_text") or "").strip()
            if not original:
                unmatched.append(
                    f"{eid}: no original_text (edit_type={e.get('edit_type')!r}) — this "
                    f"edit type can't be mechanically applied; it should have been caught "
                    f"by stage 6, so seeing it here means something upstream let it through"
                )
            else:
                unmatched.append(
                    f"{eid}: {original[:60]!r} not found verbatim in template — likely "
                    f"whitespace/smart-quote drift between the ledger and the actual docx"
                )
        for msg in unmatched:
            print(f"  WARNING: {msg}", file=sys.stderr)

    if not dry_run:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(out_path)
    return applied, unmatched


def _build_contact_line(email, phone, linkedin, github) -> str:
    # Pre-existing bug: this used to join linkedin/github directly without
    # filtering None -- crashed with a TypeError the moment either flag
    # was omitted (str.join requires every item to be a string). Filter
    # falsy values at every step instead.
    email_phone = " • ".join(x for x in [email, phone] if x)
    parts = [p for p in [email_phone, linkedin, github] if p]
    return "\n".join(parts)


_BULLET_MARKERS = ("•", "*", "- ", "\u2022")


def _split_bullet_marker(text: str) -> tuple[bool, str]:
    """Returns (is_bullet, text_without_marker). Recognizes a leading
    bullet character (•, *, or a hyphen used as a bullet, i.e. "- " not
    a real word starting with a hyphen) followed by whitespace."""
    stripped = text.lstrip()
    for marker in ("•", "\u2022", "*"):
        if stripped.startswith(marker + " ") or stripped.startswith(marker + "\t"):
            return True, stripped[len(marker):].lstrip()
    if stripped.startswith("- ") and not stripped.startswith("-- "):
        return True, stripped[2:].lstrip()
    return False, text


def _expand_placeholder_paragraph(placeholder_para, lines: list[str], double_last_gap: bool = False,
                                   tight_except_last: bool = False, detect_bullets: bool = False):
    from docx.shared import Pt
    from docx.enum.text import WD_TAB_ALIGNMENT

    src_run = placeholder_para.runs[0] if placeholder_para.runs else None
    base_space_after = placeholder_para.paragraph_format.space_after
    # Copy paragraph-level formatting from the placeholder so inserted
    # text matches the template's margins, indentation, and alignment,
    # not just the style definition's defaults (which can differ from
    # the placeholder's own overrides).
    src_fmt = placeholder_para.paragraph_format
    inserted = []
    for text in lines:
        is_bullet = False
        if detect_bullets:
            is_bullet, text = _split_bullet_marker(text)

        new_p = placeholder_para.insert_paragraph_before(
            "" if is_bullet else text, style=placeholder_para.style)
        new_p.paragraph_format.space_after = base_space_after
        if src_fmt.left_indent is not None:
            new_p.paragraph_format.left_indent = src_fmt.left_indent
        if src_fmt.right_indent is not None:
            new_p.paragraph_format.right_indent = src_fmt.right_indent
        if src_fmt.first_line_indent is not None:
            new_p.paragraph_format.first_line_indent = src_fmt.first_line_indent
        if src_fmt.alignment is not None:
            new_p.alignment = src_fmt.alignment

        if is_bullet:
            # Real hanging indent, not a literal bullet character sitting
            # flush against a wrapped second line with no alignment.
            # HANGING_INDENT = 18pt from the paragraph's own left margin;
            # a matching tab stop at that same offset means the tab
            # between the bullet glyph and the text lands exactly at the
            # hang point, so wrapped continuation lines land under the
            # text, not under the bullet.
            base_left = src_fmt.left_indent or Pt(0)
            hang = Pt(18)
            new_p.paragraph_format.left_indent = base_left + hang
            new_p.paragraph_format.first_line_indent = -hang
            tab_stops = new_p.paragraph_format.tab_stops
            tab_stops.add_tab_stop(base_left + hang, WD_TAB_ALIGNMENT.LEFT)
            run = new_p.add_run(f"•\t{text}")
            if src_run is not None:
                run.font.name = src_run.font.name
                run.font.size = src_run.font.size
                run.bold = src_run.bold
                if src_run.font.color and src_run.font.color.type:
                    run.font.color.rgb = src_run.font.color.rgb
        elif src_run is not None:
            for r in new_p.runs:
                r.font.name = src_run.font.name
                r.font.size = src_run.font.size
                r.bold = src_run.bold
                if src_run.font.color and src_run.font.color.type:
                    r.font.color.rgb = src_run.font.color.rgb
        inserted.append(new_p)

    if inserted and double_last_gap and base_space_after is not None:
        inserted[-1].paragraph_format.space_after = Pt(base_space_after.pt * 2)

    # For a block of lines that used to be ONE paragraph (e.g. a multi-
    # line address, or an address+contact-line block sharing a single
    # {{ADDRESS}}\n{{CONTACT_LINE}} placeholder) -- that original
    # paragraph's space_after was meant to apply once, after the whole
    # block, not after every individual line within it. Applying it to
    # every inserted line (the default above) visibly double/triple-
    # spaces what should read as one tight block. Zero out every line
    # except the last, which keeps the original spacing so the block as
    # a whole is still visually separated from whatever follows it.
    if inserted and tight_except_last:
        for p in inserted[:-1]:
            p.paragraph_format.space_after = Pt(0)
        inserted[-1].paragraph_format.space_after = base_space_after

    placeholder_para._p.getparent().remove(placeholder_para._p)
    return inserted


def _build_address_lines(address_line_1, address_line_2, city, state, zipcode) -> list[str]:
    lines = []
    # Join address lines 1 and 2 on a single line, comma-separated,
    # rather than stacking them — reads better when line 2 is short
    # (e.g. "4934 Locust Street, Unit 4" instead of two separate lines).
    addr_parts = [p for p in [address_line_1, address_line_2] if p]
    if addr_parts:
        lines.append(", ".join(addr_parts))
    if city and state:
        line = f"{city}, {state}"
        if zipcode:
            line += f" {zipcode}"
        lines.append(line)
    elif city or state:
        # Partial info -- show whatever's actually there instead of
        # either "None, None" or silently dropping a real value.
        partial = ", ".join(x for x in [city, state] if x)
        if zipcode:
            partial += f" {zipcode}"
        lines.append(partial)
    return lines


def _build_cover_letter_from_template(
    template_path: str, body_paragraphs: list[str], out_path: str,
    applicant_name: str, applicant_email: str | None, applicant_phone: str | None,
    company: str, role_title: str, dry_run: bool,
    applicant_linkedin: str | None = None, applicant_github: str | None = None,
    applicant_address_line_1: str | None = None, applicant_address_line_2: str | None = None,
    applicant_city: str | None = None, applicant_state: str | None = None,
    applicant_zipcode: str | None = None,
    resume_variant: str | None = None,
) -> None:
    import docx

    doc = docx.Document(template_path)
    contact_line = _build_contact_line(applicant_email, applicant_phone, applicant_linkedin, applicant_github)
    address_lines = _build_address_lines(
        applicant_address_line_1, applicant_address_line_2,
        applicant_city, applicant_state, applicant_zipcode,
    )
    replacements = {
        "{{APPLICANT_NAME}}": applicant_name,
        "{{CONTACT_LINE}}": contact_line,
        "{{ROLE_TITLE}}": role_title,
        "{{COMPANY}}": company,
    }

    # Date formatting — the template may use {{DATE}} or the more
    # descriptive {{DATE (formatted like: August 8th, 2026)}} form.
    # Handle both by checking paragraph text for either pattern.
    import datetime
    now = datetime.datetime.now()
    day = now.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    formatted_date = now.strftime(f"%B {day}{suffix}, %Y")

    # Time-of-day salutation
    hour = now.hour
    if hour < 12:
        greeting = "Good Morning"
    elif hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    # Resolve the {{Backend | Fullstack}} variant placeholder in the
    # template header. Maps to "Backend" or "Full-Stack" based on the
    # resume variant that was routed for this application.
    variant_display = "Full-Stack" if resume_variant == "Full Stack" else (resume_variant or "Backend")
    for para in iter_all_paragraphs(doc):
        if "{{Backend | Fullstack}}" in para.text or "{{Backend | Full-Stack}}" in para.text:
            for r in para.runs:
                r.text = r.text.replace("{{Backend | Fullstack}}", variant_display)
                r.text = r.text.replace("{{Backend | Full-Stack}}", variant_display)

    body_placeholder = None
    address_placeholder = None
    contact_placeholder = None
    for para in iter_all_paragraphs(doc):
        if "{{BODY}}" in para.text:
            body_placeholder = para
            continue

        # Date placeholder — may be {{DATE}} or the descriptive
        # {{DATE (formatted like: ...)}} form. Replace the entire
        # paragraph text rather than substring-matching, since the
        # descriptive form has parentheses that make partial replacement
        # fragile across runs.
        if "{{DATE" in para.text:
            for r in para.runs:
                r.text = ""
            if para.runs:
                para.runs[0].text = formatted_date
            continue

        # Salutation placeholder — {{Good (Morning | Afternoon | Evening)}}
        # followed by company/addressee. Replace the whole paragraph.
        if "{{Good" in para.text or "Good (Morning" in para.text:
            for r in para.runs:
                r.text = ""
            if para.runs:
                para.runs[0].text = f"{greeting} {company} Recruiting Team,"
            continue

        # Address/contact — the new template has these hardcoded (with
        # hyperlinks in an invisible table), so {{ADDRESS}} and
        # {{CONTACT_LINE}} may not be present. Still check for them to
        # support older templates cleanly.
        matched_placeholder = False
        if "{{ADDRESS}}" in para.text:
            address_placeholder = para
            matched_placeholder = True
        if "{{CONTACT_LINE}}" in para.text:
            contact_placeholder = para
            matched_placeholder = True
        if matched_placeholder:
            continue
        for placeholder, value in replacements.items():
            docx_replace_text(para, placeholder, value)

    if address_placeholder is not None:
        # Real templates commonly put {{ADDRESS}}\n{{CONTACT_LINE}} in ONE
        # paragraph (a line break, not a paragraph break) -- if so,
        # contact_placeholder IS address_placeholder, and expanding the
        # address alone would delete {{CONTACT_LINE}} right along with
        # it (this was happening silently -- contact_line was computed
        # correctly above and then just never inserted anywhere).
        # tight_except_last fixes the matching over-spaced-address bug
        # in the same pass: that paragraph's spacing was meant to apply
        # once after the whole block, not after every individual line.
        combined_lines = list(address_lines)
        if contact_placeholder is address_placeholder and contact_line:
            combined_lines += contact_line.split("\n")
        _expand_placeholder_paragraph(address_placeholder, combined_lines, tight_except_last=True)

    if contact_placeholder is not None and contact_placeholder is not address_placeholder:
        # Template has {{CONTACT_LINE}} as its own separate paragraph --
        # still needs _expand_placeholder_paragraph rather than a plain
        # docx_replace_text substitution, since contact_line can itself
        # be multiple lines (email/phone on one line, LinkedIn/GitHub
        # each on their own -- see _build_contact_line).
        if contact_line:
            _expand_placeholder_paragraph(contact_placeholder, contact_line.split("\n"), tight_except_last=True)
        else:
            contact_placeholder._p.getparent().remove(contact_placeholder._p)

    if body_placeholder is not None:
        _expand_placeholder_paragraph(body_placeholder, body_paragraphs, double_last_gap=True, detect_bullets=True)
    else:
        print("  WARNING: cover letter template has no {{BODY}} placeholder — "
              "body content wasn't inserted.", file=sys.stderr)

    if not dry_run:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(out_path)
    else:
        print(f"[dry-run] would write cover letter docx (from template) -> {out_path}", file=sys.stderr)


def _build_cover_letter_from_scratch(
    body_paragraphs: list[str], out_path: str, applicant_name: str,
    applicant_email: str | None, applicant_phone: str | None,
    company: str, role_title: str, dry_run: bool,
    applicant_linkedin: str | None = None, applicant_github: str | None = None,
    applicant_address_line_1: str | None = None, applicant_address_line_2: str | None = None,
    applicant_city: str | None = None, applicant_state: str | None = None,
    applicant_zipcode: str | None = None,
) -> None:
    import docx
    from docx.shared import Pt

    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    header = doc.add_paragraph()
    header.add_run(applicant_name).bold = True
    for line in _build_address_lines(applicant_address_line_1, applicant_address_line_2,
                                      applicant_city, applicant_state, applicant_zipcode):
        doc.add_paragraph(line)
    contact_line = _build_contact_line(applicant_email, applicant_phone, applicant_linkedin, applicant_github)
    if contact_line:
        doc.add_paragraph(contact_line)

    doc.add_paragraph(time.strftime("%B %d, %Y"))
    doc.add_paragraph()
    doc.add_paragraph(f"Re: {role_title} at {company}")
    doc.add_paragraph()
    doc.add_paragraph("Dear Hiring Manager,")
    doc.add_paragraph()

    for para_text in body_paragraphs:
        doc.add_paragraph(para_text)
        doc.add_paragraph()

    doc.add_paragraph("Sincerely,")
    doc.add_paragraph(applicant_name)

    if not dry_run:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(out_path)
    else:
        print(f"[dry-run] would write cover letter docx (from scratch) -> {out_path}", file=sys.stderr)


CLOSING_SENTENCE = (
    "I would welcome the opportunity to discuss how my background can contribute "
    "to your team, and I look forward to the possibility of speaking further."
)


def build_cover_letter_docx(
    body_paragraphs: list[str], out_path: str, applicant_name: str,
    applicant_email: str | None, applicant_phone: str | None,
    company: str, role_title: str, dry_run: bool,
    cover_letter_template: str | None = None,
    applicant_linkedin: str | None = None, applicant_github: str | None = None,
    applicant_address_line_1: str | None = None, applicant_address_line_2: str | None = None,
    applicant_city: str | None = None, applicant_state: str | None = None,
    applicant_zipcode: str | None = None,
    baseline_mode: bool = False,
    resume_variant: str | None = None,
) -> None:
    # In from-scratch mode (no baseline), append a guaranteed closing
    # sentence so the letter doesn't end abruptly. In baseline-swap mode
    # the closing is already part of the candidate's own text (and gets
    # rewritten per-company by stage 5's rule 11), so don't duplicate it.
    if not baseline_mode:
        body_paragraphs = list(body_paragraphs) + [CLOSING_SENTENCE]

    address_kwargs = dict(
        applicant_address_line_1=applicant_address_line_1, applicant_address_line_2=applicant_address_line_2,
        applicant_city=applicant_city, applicant_state=applicant_state, applicant_zipcode=applicant_zipcode,
    )
    if cover_letter_template:
        _build_cover_letter_from_template(
            cover_letter_template, body_paragraphs, out_path, applicant_name,
            applicant_email, applicant_phone, company, role_title, dry_run,
            applicant_linkedin=applicant_linkedin, applicant_github=applicant_github,
            resume_variant=resume_variant,
            **address_kwargs,
        )
    else:
        _build_cover_letter_from_scratch(
            body_paragraphs, out_path, applicant_name,
            applicant_email, applicant_phone, company, role_title, dry_run,
            applicant_linkedin=applicant_linkedin, applicant_github=applicant_github,
            **address_kwargs,
        )


# ---------------------------------------------------------------------------
# NEW: interactive full-text cover letter review
# ---------------------------------------------------------------------------

def _open_in_notepad_and_wait(text: str, draft_path: Path) -> str:
    """Writes `text` to a predictable, visible file (NOT a randomly-named
    OS temp file -- that was hard to locate and vanished the moment
    Notepad closed, which defeated the whole point of offering a file-
    based edit path over inline copy-paste), opens it in Notepad, and
    BLOCKS until Notepad is closed (subprocess.run's default behavior)
    -- that close is the "done editing" signal, rather than polling the
    file's mtime, which is more fragile (autosave, editors that keep a
    lock, ambiguity about which save was the "final" one). Returns the
    file's contents after Notepad exits, whatever they ended up being.
    The file is left on disk afterward (not deleted) -- see its own
    docstring note in interactive_cover_letter_review() for why."""
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(text, encoding="utf-8")

    print(f"  Opening {draft_path} in Notepad -- edit, save, then close the window to continue...")
    subprocess.run(["notepad.exe", str(draft_path)])
    return draft_path.read_text(encoding="utf-8")


def _ai_revise_cover_letter(baseline_text: str, current_text: str, feedback: str, provider: str | None) -> str:
    """Sends the original baseline, the current (possibly already-edited)
    text, and your typed feedback to the Anthropic API; asks for a
    revision that addresses the feedback while staying as close as
    possible to the current text and the original's voice/structure.
    Uses the same call_llm_structured convention as the rest of the
    pipeline (forced structured output, not raw-JSON parsing)."""
    from pydantic import BaseModel

    class CoverLetterRevision(BaseModel):
        revised_text: str

    system = load_prompt("stages/stage09_assemble_materials/cover_letter_revision_system.txt")
    user = (
        f"ORIGINAL:\n{baseline_text}\n\n"
        f"CURRENT:\n{current_text}\n\n"
        f"FEEDBACK:\n{feedback}\n\n"
        f"Return the complete revised cover letter body."
    )
    result = call_llm_structured(
        system=system, user=user, response_model=CoverLetterRevision,
        tier="standard", provider=provider,
    )
    return result.revised_text


def interactive_cover_letter_review(body_paragraphs: list[str], provider: str | None, out_dir: Path) -> list[str] | None:
    """Full-text review loop over the assembled cover letter, run once
    right before the docx is built. Returns the finalized list of body
    paragraphs, or None if the user quit without accepting (caller should
    treat that as an abort, not proceed to write files).

    (a)ccept the current text as-is
    (m)anual  -- edit in Notepad, loop back with whatever was saved. The
                 file lives at <out_dir>/cover_letter_draft.txt -- a
                 predictable, visible path (not a random OS temp file),
                 so you can find/re-open it yourself if needed, and it's
                 already pre-filled with the current text -- no copy-
                 paste needed, just edit the one or two lines you want
                 changed and save.
    (f)eedback -- describe a change in words; an Anthropic API call revises
                 the current text accordingly; loop back with the result
    (q)uit    -- abort without building the cover letter
    """
    baseline_text = "\n\n".join(body_paragraphs)
    current_text = baseline_text
    draft_path = out_dir / "cover_letter_draft.txt"

    while True:
        print("\n" + "=" * 70)
        print("COVER LETTER -- full text review")
        print("=" * 70)
        print(current_text)
        print("=" * 70)
        choice = input("\n  (a)ccept / (m)anual edit / (f)eedback for AI edit / (q)uit: ").strip().lower()

        if choice in ("a", "accept"):
            return [p for p in current_text.split("\n\n") if p.strip()]

        elif choice in ("m", "manual"):
            current_text = _open_in_notepad_and_wait(current_text, draft_path)
            # loop back to re-show the (possibly edited) text

        elif choice in ("f", "feedback"):
            feedback = input("  Describe what to change: ").strip()
            if not feedback:
                print("  (empty feedback, nothing sent)")
                continue
            print(f"  Sending to {provider} API...")
            try:
                current_text = _ai_revise_cover_letter(baseline_text, current_text, feedback, provider)
            except Exception as e:
                print(f"  [ERROR] AI revision failed: {e}", file=sys.stderr)
            # loop back to re-show the (possibly revised) text

        elif choice in ("q", "quit"):
            return None

        else:
            print("  Not a recognized choice -- pick a, m, f, or q.")


def build_followup_steps_txt(jd_input: dict, company: str, role_title: str,
                              keyword_gaps_unsupported: list[str] | None = None) -> str | None:
    """Builds a plain-text follow-up-outreach checklist from jd_input.json's
    warm_contact/source_url fields (stage 0's output, read by run() from
    the same out_dir), plus -- if present -- the JD-required keywords
    stage 4 found no supporting claim for at all (see
    core/prose.classify_keyword_gaps). Those are deliberately NOT
    fed back into edit generation (there's nothing genuine to build an
    edit from), so this file is where that signal actually goes: either
    you have real, undocumented experience worth adding to your resume
    for future applications, or the JD is asking for something you
    genuinely don't have -- both are useful to know, neither is this
    pipeline's decision to make on your behalf.

    Returns None only if there's NOTHING actionable to say at all --
    no warm_contact, no source_url, AND no unsupported keywords --
    rather than writing a mostly-empty file every time."""
    contact = jd_input.get("warm_contact") or {}
    name = contact.get("name")
    method = contact.get("contact_method")
    email = contact.get("email")
    position = contact.get("position")
    source_url = jd_input.get("source_url")
    keyword_gaps_unsupported = keyword_gaps_unsupported or []

    if not any([name, method, email, position, source_url, keyword_gaps_unsupported]):
        return None

    lines = [f"Follow-up plan — {role_title} at {company}", "=" * 60, ""]

    if keyword_gaps_unsupported:
        lines += [
            "JD-required terms with no supporting claim found in your resume:",
            f"  {', '.join(keyword_gaps_unsupported)}",
            "If you have real, undocumented experience with any of these, it's worth",
            "adding to your actual resume/config for every future application --",
            "not just patched into this one letter. If you genuinely don't have them,",
            "that's worth knowing too before you invest more time on this posting.",
            "",
        ]

    if source_url:
        lines += [f"Posting: {source_url}", ""]

    if name or method or email or position:
        lines += ["Point of contact (from the posting):"]
        if name:
            lines.append(f"  Name:     {name}")
        if position:
            lines.append(f"  Position: {position}")
        if method:
            lines.append(f"  Reach via: {method}")
        if email:
            lines.append(f"  Email:    {email}")
        lines.append("")
        lines += [
            "Suggested sequence:",
            "  1. Submit the application (resume + cover letter in this folder).",
            f"  2. Within 24–48 hours, reach out to {name or 'the contact above'} "
            f"{f'via {method}' if method else 'directly'} — a cold submission alone "
            "is much easier to overlook than one followed by a direct, specific note.",
            "  3. Keep it short: reference the exact role, one concrete reason you're "
            "a fit, and that you've just applied. Don't restate the whole cover letter.",
            "",
            "Draft opener (edit before sending — this is a starting point, not a script):",
            # Prose again, same reasoning as the cover letter's <ROLE_NAME>
            # swap -- see simplify_role_title_for_prose().
            f'  "Hi {name.split()[0] if name else "[name]"}, I just applied for the '
            f'{simplify_role_title_for_prose(role_title)} role at {company} and wanted to '
            f'reach out directly. [one sentence on why this specific role/team]. Happy to '
            f'share more if useful — thanks for your time."',
        ]
    else:
        # source_url present but no contact info -- still worth a nudge
        # rather than silently doing nothing, just without a name to reach.
        lines += [
            "No named point of contact was found in the posting. Worth 2-3 minutes",
            f"checking {company}'s LinkedIn page or the job post itself for who's",
            "hiring for this role before treating this as a fully cold application.",
        ]

    return "\n".join(lines) + "\n"


def run(resume_template, review_decisions_path, verified_edits_path,
        out_dir, company, role_title, applicant_name, applicant_email,
        applicant_phone, dry_run, cover_letter_template=None,
        applicant_linkedin=None, applicant_github=None,
        applicant_address_line_1=None, applicant_address_line_2=None,
        applicant_city=None, applicant_state=None, applicant_zipcode=None,
        provider=None, skip_cover_letter_review=False,
        cover_letter_baseline=None, resume_variant=None):
    # Graceful fallback: if a cover letter template path was given
    # (explicitly, or via the new default) but doesn't actually exist on
    # disk, don't crash inside python-docx with a confusing error --
    # warn plainly and fall back to the generic from-scratch build,
    # same as if none had been requested at all.
    if cover_letter_template and not Path(cover_letter_template).exists():
        print(f"WARNING: --cover-letter-template not found at {cover_letter_template!r} "
              f"-- falling back to the generic from-scratch cover letter build.", file=sys.stderr)
        cover_letter_template = None

    decisions = json.loads(Path(review_decisions_path).read_text(encoding="utf-8"))
    verified = json.loads(Path(verified_edits_path).read_text(encoding="utf-8"))
    all_edits = {e["edit_id"]: e for e in verified["passed"] + verified["flagged"]}

    # verification_report.json (stage 6's own output, sitting alongside
    # verified_edits.json) carries the real discarded_count -- read it
    # for the audit log rather than hardcoding 0. Missing/unreadable
    # report shouldn't block assembly, just leaves this field honest
    # about not knowing rather than silently wrong.
    discarded_stage6 = 0
    report_path = Path(verified_edits_path).parent / "verification_report.json"
    if report_path.exists():
        try:
            discarded_stage6 = json.loads(report_path.read_text(encoding="utf-8")).get("discarded_count", 0)
        except Exception:
            pass

    # jd_input.json (stage 0's output, same out_dir) carries warm_contact/
    # source_url if the extension found them -- read it for the follow-up
    # steps file below. Missing/unreadable shouldn't block assembly.
    jd_input = {}
    jd_input_path = Path(out_dir) / "jd_input.json"
    if jd_input_path.exists():
        try:
            jd_input = json.loads(jd_input_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    approved_resume_edits = {}
    approved_cover_edits = []
    for d in decisions:
        if d["decision"] == "rejected":
            continue
        edit = all_edits.get(d["edit_id"])
        if not edit:
            continue
        final_text = d.get("final_text") or edit["suggested_text"]
        merged = {**edit, "final_text": final_text}
        if edit["target"] == "resume":
            approved_resume_edits[d["edit_id"]] = merged
        else:
            approved_cover_edits.append(merged)

    out = Path(out_dir)

    # Sanitize inputs for safe file naming
    safe_company = "".join(c for c in company if c.isalnum() or c in " -_").strip()
    safe_role = "".join(c for c in role_title if c.isalnum() or c in " -_").strip()
    safe_name = "".join(c for c in applicant_name if c.isalnum() or c in " -_").strip()

    # Truncate company/role dynamically if the FULL PATH would exceed
    # Windows' ~260-char limit -- real incident: a long app_id folder
    # name plus a long role title ("Staff Engineer - Distributed
    # Systems - Flag Delivery", "Principal Backend & Infrastructure
    # Engineer-Full Time (US /LATAM)") pushed two real generated paths
    # to 258 and 293 characters, which Word's COM automation surfaced
    # as "String is longer than 255 characters" and a bare
    # 'NoneType' object has no attribute 'SaveAs' -- neither error
    # names the actual cause, so this went undiagnosed until traced
    # back from the real paths. A fixed-length cap can't work here
    # since the budget depends on how deep this specific app's folder
    # already is, so it's computed fresh against the real directory
    # each time rather than guessed at.
    PATH_SAFETY_MARGIN = 240  # stay meaningfully under 260, not right at the edge
    longest_suffix = f" - {safe_name} - Cover Letter.docx"  # longest of the five variants
    dir_len = len(str(out / "generated_materials")) + 1  # +1 for the path separator
    budget = PATH_SAFETY_MARGIN - dir_len - len(longest_suffix) - len(" -  - ")  # separators between company/role
    if budget < 20:
        budget = 20  # floor -- always leave SOMETHING recognizable rather than an empty name
    if len(safe_company) + len(safe_role) > budget:
        # Company name is the more identity-critical, usually-shorter
        # field -- preserve it in FULL whenever the budget allows, and
        # let role_title absorb the cut first. Only fall back to also
        # truncating company if role_title alone can't be cut enough
        # (e.g. an unusually long company name). Proportional splitting
        # was the first attempt here, but it cut a short, recognizable
        # name like "LaunchDarkly" down to "LaunchDar…" even though the
        # role title was clearly the actual long part -- company
        # identity is worth keeping intact over role-title completeness.
        role_budget = budget - len(safe_company)
        if role_budget >= 15:
            safe_role = safe_role[:role_budget - 1].rstrip() + "…" if len(safe_role) > role_budget else safe_role
        else:
            # Even sacrificing role_title entirely isn't enough --
            # company itself must be long. Split what's left.
            company_budget = max(10, budget - 15)
            safe_company = safe_company[:company_budget - 1].rstrip() + "…" if len(safe_company) > company_budget else safe_company
            remaining = budget - len(safe_company)
            safe_role = safe_role[:max(10, remaining) - 1].rstrip() + "…" if len(safe_role) > max(10, remaining) else safe_role

    # Define all output file paths
    resume_docx_name = f"{safe_company} - {safe_role} - {safe_name} - Resume.docx"
    resume_pdf_name = f"{safe_company} - {safe_role} - {safe_name} - Resume.pdf"
    cover_docx_name = f"{safe_company} - {safe_role} - {safe_name} - Cover Letter.docx"
    cover_pdf_name = f"{safe_company} - {safe_role} - {safe_name} - Cover Letter.pdf"
    followup_txt_name = f"{safe_company} - {safe_role} - {safe_name} - Follow-Up Steps.txt"

    # Real generated documents go in their own subfolder, not mixed in
    # with the dozen-plus audit-trail JSON files that also live in
    # applications/<app_id>/ (jd_input.json, company_brief.json,
    # edit_brief.json, candidate_edits.json, verified_edits.json,
    # verification_report.json, review_decisions.json, routing.json,
    # edit_log.jsonl) -- makes it trivial to find the actual output
    # among everything else in the folder. Not created in --dry-run
    # (nothing real is being written anyway).
    materials_dir = out / "generated_materials"
    if not dry_run:
        materials_dir.mkdir(parents=True, exist_ok=True)

    resume_docx_path = materials_dir / resume_docx_name
    resume_pdf_path = materials_dir / resume_pdf_name
    cover_docx_path = materials_dir / cover_docx_name
    cover_pdf_path = materials_dir / cover_pdf_name
    followup_txt_path = materials_dir / followup_txt_name

    n_applied, unmatched = apply_edits_to_docx(
        resume_template, approved_resume_edits, str(resume_docx_path), dry_run,
    )

    # Cover letter: start from the candidate's own baseline text and
    # apply approved edits as targeted in-place swaps -- same mechanics
    # as the resume path, same verbatim-match-and-replace logic. The
    # letter's narrative structure, tense, and voice survive by default;
    # only the parts that earned an edit for THIS job change.
    if cover_letter_baseline:
        baseline_text = Path(cover_letter_baseline).read_text(encoding="utf-8").replace("\r\n", "\n")
    else:
        baseline_text = ""

    # Apply cover-letter swaps against the baseline
    tailored_body = baseline_text
    cl_applied = 0
    cl_unmatched = []
    
    for edit in approved_cover_edits:
        # Strip both sides before matching -- a real case showed the
        # model's original_text field including the trailing blank-line
        # paragraph separator as part of what it considered "the text",
        # while final_text didn't restore an equivalent one. The replace
        # then correctly swapped exactly what matched (separator
        # included), fusing that paragraph directly onto the next one
        # with zero space between them. Stripping means a swap can only
        # ever touch the paragraph's own content, never the whitespace
        # that separates it from its neighbors.
        original = edit.get("original_text", "").strip()
        replacement = edit["final_text"].strip()

        if not original:
            continue

        match = find_edit_match(tailored_body, original)
        if match:
            start, end = match
            tailored_body = tailored_body[:start] + replacement + tailored_body[end:]
            cl_applied += 1
        else:
            cl_unmatched.append(edit["edit_id"])

    if cl_unmatched:
        print(f"  WARNING: {len(cl_unmatched)} cover letter edit(s) did not match the "
              f"baseline text (original_text not found verbatim): {cl_unmatched}",
              file=sys.stderr)

    # Extract just the body for the template (strip header/salutation/sign-off)
    body_lines = tailored_body.split("\n")
    body_start = None
    body_end = None
    for idx, line in enumerate(body_lines):
        if (line.strip().startswith("Dear ") or line.strip().startswith("Good ")
                or line.strip().startswith("{{Good")):
            body_start = idx + 1
        if line.strip() in ("Best,", "Sincerely,", "Regards,", "Thank you,"):
            body_end = idx
            break
    if body_start is None:
        body_start = 0
    if body_end is None:
        body_end = len(body_lines)
    while body_start < body_end and not body_lines[body_start].strip():
        body_start += 1
    while body_end > body_start and not body_lines[body_end - 1].strip():
        body_end -= 1

    # Split into paragraphs on blank lines (preserving bullet structure)
    raw_body = "\n".join(body_lines[body_start:body_end])
    # Replace the baseline's own placeholders with this application's
    # real company/role — these are in the candidate's original text
    # (e.g. "<COMPANY_NAME>", "<ROLE_NAME>", "the Core Technology team")
    # and survive the swap process since stage 5 only touches specific
    # passages, not the whole letter.
    raw_body = raw_body.replace("<COMPANY_NAME>", company)
    # Prose (a sentence, not a subject/header line) -- simplified so a
    # title like "Staff Software Engineer, Backend (Search)" doesn't
    # read as a run-on mid-sentence. The Re: line a few paragraphs up
    # uses the verbatim role_title instead; see
    # simplify_role_title_for_prose()'s own docstring for why the two
    # spots are treated differently.
    raw_body = raw_body.replace("<ROLE_NAME>", simplify_role_title_for_prose(role_title))
    cover_body_paragraphs = [p.strip() for p in raw_body.split("\n\n") if p.strip()]

    # Interactive review is for the old from-scratch path; skip it for
    # baseline-swap mode since stage 8 already reviewed each individual
    # swap. The skip_cover_letter_review flag from batch_assemble still
    # applies as a safety net regardless.
    if cover_body_paragraphs and not skip_cover_letter_review and not cover_letter_baseline:
        reviewed = interactive_cover_letter_review(cover_body_paragraphs, provider, out)
        if reviewed is None:
            print("\nCover letter review was quit without accepting -- aborting before "
                  "any files are written. Re-run stage 9 when ready.", file=sys.stderr)
            return
        cover_body_paragraphs = reviewed

    build_cover_letter_docx(
        body_paragraphs=cover_body_paragraphs,
        out_path=str(cover_docx_path),
        applicant_name=applicant_name, applicant_email=applicant_email,
        applicant_phone=applicant_phone, company=company, role_title=role_title,
        dry_run=dry_run, cover_letter_template=cover_letter_template,
        applicant_linkedin=applicant_linkedin, applicant_github=applicant_github,
        applicant_address_line_1=applicant_address_line_1, applicant_address_line_2=applicant_address_line_2,
        applicant_city=applicant_city, applicant_state=applicant_state, applicant_zipcode=applicant_zipcode,
        baseline_mode=bool(cover_letter_baseline),
        resume_variant=resume_variant,
    )

    # PDF Conversion Execution
    if not dry_run:
        try:
            from docx2pdf import convert

            print("\nSweeping for background Word processes to prevent COM lockups...")
            _kill_word_processes()

            print("Converting documents to PDF...")
            convert(str(resume_docx_path), str(resume_pdf_path))
            convert(str(cover_docx_path), str(cover_pdf_path))
            print("PDFs generated successfully.")
        except ImportError:
            print("\nWARNING: 'docx2pdf' is not installed. To enable PDF export, run: pip install docx2pdf", file=sys.stderr)
        except Exception as e:
            print(f"\nWARNING: PDF conversion failed: {e}", file=sys.stderr)

    keyword_gaps_unsupported = []
    edit_brief_path = out / "edit_brief.json"
    if edit_brief_path.is_file():
        try:
            keyword_gaps_unsupported = json.loads(edit_brief_path.read_text(encoding="utf-8")).get(
                "keyword_gaps_unsupported", [])
        except Exception:
            pass  # older apps prepared before this field existed -- just skip it, not fatal

    followup_text = build_followup_steps_txt(jd_input, company, role_title, keyword_gaps_unsupported)
    if followup_text:
        if dry_run:
            print(f"[dry-run] would write follow-up steps -> {followup_txt_path}", file=sys.stderr)
        else:
            followup_txt_path.write_text(followup_text, encoding="utf-8")

    if provider == "claude":
        _model_desc = f"generation={CLAUDE_MODELS['reasoning']}, verification/revisions={CLAUDE_MODELS['standard']}"
    elif provider == "gemini":
        _model_desc = f"generation={GEMINI_MODELS['reasoning']}, verification/revisions={GEMINI_MODELS['standard']}"
    else:  # lmstudio -- one local model serves every tier, no cheap/standard/reasoning split
        _model_desc = "local model via LM Studio (see logs for exact model id)"
    entry = AssemblyLogEntry(
        application_id=out.name,
        company=company,
        role_title=role_title,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        edits_approved=sum(d["decision"] in ("approved", "edited") for d in decisions),
        edits_rejected=sum(d["decision"] == "rejected" for d in decisions),
        edits_discarded_stage6=discarded_stage6,
        model_used=f"{provider}: {_model_desc}",
    )
    append_jsonl(out / "edit_log.jsonl", entry, dry_run)
    log_event("stage9_assemble", {
        "resume_edits_applied": n_applied, "resume_edits_unmatched": len(unmatched), "dry_run": dry_run,
    })

    print(f"\nApplied {n_applied}/{len(approved_resume_edits)} resume edits "
          f"({len(unmatched)} unmatched — check the warnings above if >0).")
    print(f"Cover letter saved -> {cover_docx_path.name}")
    print(f"Resume saved -> {resume_docx_path.name}")
    if followup_text:
        print(f"Follow-up steps saved -> {followup_txt_path.name}")
    if not dry_run:
        print(f"Final outputs located at -> {out.resolve()}")

    if unmatched:
        print("\nUnmatched edits usually mean the original_text in the ledger doesn't "
              "match the template verbatim (extra whitespace, smart quotes, etc.) — "
              "worth a diff between claims_ledger.json and the template text.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume-template", required=True)
    ap.add_argument("--review-decisions", required=True)
    ap.add_argument("--verified-edits", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--company", required=True)
    ap.add_argument("--role-title", required=True)
    ap.add_argument("--applicant-name", required=True)
    ap.add_argument("--applicant-email", default=None)
    ap.add_argument("--applicant-phone", default=None)
    ap.add_argument("--applicant-linkedin", default=None)
    ap.add_argument("--applicant-github", default=None)
    ap.add_argument("--applicant-address-line-1", default=None)
    ap.add_argument("--applicant-address-line-2", default=None)
    ap.add_argument("--applicant-city", default=None)
    ap.add_argument("--applicant-state", default=None)
    ap.add_argument("--applicant-zipcode", default=None)
    ap.add_argument("--cover-letter-template",
                     default="config/cover_letter_template/cover_letter_template.docx",
                     help="Docx with {{APPLICANT_NAME}}/{{CONTACT_LINE}}/{{DATE}}/"
                          "{{ROLE_TITLE}}/{{COMPANY}}/{{BODY}}/{{ADDRESS}} placeholders. "
                          "Defaults to your standard template at config/cover_letter_template/ "
                          "-- pass --cover-letter-template \"\" (empty string) to explicitly force "
                          "the generic from-scratch build instead. Falls back to from-scratch "
                          "automatically (with a warning) if the default path doesn't exist.")
    ap.add_argument("--provider", default=None, choices=["lmstudio", "claude", "gemini"],
                     help="Used only for AI-assisted cover letter revisions. Defaults to "
                          "$CVTAILOR_LLM_PROVIDER, or 'lmstudio' (local, no cloud spend) if that's unset.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-cover-letter-review", action="store_true",
                     help="Skip the interactive full-text cover letter review loop -- "
                          "use for unattended/batch runs (py_pipeline_assemble.py always "
                          "passes this). Leave off for interactive single-JD use, where "
                          "it's the last human checkpoint before the docx is built.")
    ap.add_argument("--resume-variant", default=None,
                     help="Backend or Full Stack -- resolves the variant placeholder in the template header.")
    ap.add_argument("--cover-letter-baseline",
                     help="Path to the candidate's existing cover letter sample (.txt). "
                          "When provided, cover letter edits are applied as targeted swaps "
                          "against this text rather than assembling from scratch.")
    args = ap.parse_args()
    if args.provider is None:
        args.provider = default_provider()
    run(args.resume_template, args.review_decisions, args.verified_edits,
        args.out_dir, args.company, args.role_title, args.applicant_name,
        args.applicant_email, args.applicant_phone, args.dry_run,
        cover_letter_template=args.cover_letter_template,
        applicant_linkedin=args.applicant_linkedin, applicant_github=args.applicant_github,
        applicant_address_line_1=args.applicant_address_line_1,
        applicant_address_line_2=args.applicant_address_line_2,
        applicant_city=args.applicant_city, applicant_state=args.applicant_state,
        applicant_zipcode=args.applicant_zipcode,
        provider=args.provider,
        skip_cover_letter_review=args.skip_cover_letter_review,
        cover_letter_baseline=args.cover_letter_baseline,
        resume_variant=args.resume_variant,
    )