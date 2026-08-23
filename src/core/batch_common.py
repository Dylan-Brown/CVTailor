#!/usr/bin/env python3
"""
batch_common.py -- shared orchestration helpers for run_pipeline.py and
the src/checkpoints/, src/controls/ scripts: config/ folder conventions,
resume-variant routing, the applications/-as-queue-and-drop-zone model.
"""

from __future__ import annotations
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from py_run_stage import sh, stage_script

JOBS_ROOT = Path(__file__).resolve().parent.parent.parent  # src/core/ -> src/ -> jobs/
CONFIG_ROOT = JOBS_ROOT / "config"
RESUME_DIR = CONFIG_ROOT / "resume_pdfs"
RESUME_TEMPLATE_DIR = CONFIG_ROOT / "resume_template"
RESUME_VARIANTS_DIR = CONFIG_ROOT / "resume_variants"
COVER_LETTER_TEMPLATE_DIR = CONFIG_ROOT / "cover_letter_template"  # optional — see
                                                                    # discover_single_file_optional
COVER_LETTER_SAMPLE_DIR = CONFIG_ROOT / "cover_letter_sample"
APPLICANT_INFO_PATH = CONFIG_ROOT / "applicant_info.json"

APPLICATIONS_ROOT = JOBS_ROOT / "applications"
SHARED_DIR = APPLICATIONS_ROOT / "_shared"  # stage 2's output, reused across every JD
ARCHIVED_DIR = APPLICATIONS_ROOT / "archived"  # permanent -- applications you're fully
                                                # done with, packed up as one folder
                                                # (jd folder + its generated_materials/),
                                                # never touched by py_pipeline_reset.py
STAGES_DIR = JOBS_ROOT / "src" / "stages"

# Every name directly under applications/ that's bookkeeping/archival or
# your own manual organizing, never a live JD folder -- every script
# that lists applications/ must skip ALL of these, or a folder like
# applications/archive/ (py_post_pipeline_store_applied.py's destination) gets silently
# walked as if it were itself one application (a bogus review, a fake
# definition_of_done_report.json written INTO applications/archive/,
# etc). Centralized here after three separate hand-copied versions of
# this exclusion set had each drifted to miss "archive" (the real,
# in-use folder name -- "archived" is ARCHIVED_DIR above, a distinct,
# currently-unused destination) and none of them knew about "revisit".
# Any new folder you manually create directly under applications/ for
# your own organizing needs to be added here too, or every stage that
# auto-discovers pending work will misread it as a real application.
NON_APPLICATION_DIR_NAMES = {"_shared", "archive", "archived", "revisit"}

# Marks a JD as fully assembled (real resume/cover-letter output
# exists) -- visible at a glance in the applications/<app_id>/ folder
# itself, no separate tracking file to drift out of sync with reality.
PROCESSED_JD_NAME = "jd_input.processed.json"
UNPROCESSED_JD_NAME = "jd_input.json"


def app_dirs() -> list[Path]:
    """Every live application folder under applications/ -- see
    NON_APPLICATION_DIR_NAMES for what's excluded and why."""
    if not APPLICATIONS_ROOT.is_dir():
        return []
    return sorted(
        p for p in APPLICATIONS_ROOT.iterdir()
        if p.is_dir() and p.name not in NON_APPLICATION_DIR_NAMES
    )


def find_jd_input(app_dir: Path) -> Path | None:
    """jd_input.json before assembly, jd_input.processed.json after --
    checks both since the file gets renamed on successful assembly.
    Returns None if neither exists (stage 0 was never run for this
    folder, which shouldn't normally happen but is worth handling
    rather than crashing on)."""
    processed = app_dir / PROCESSED_JD_NAME
    if processed.is_file():
        return processed
    unprocessed = app_dir / UNPROCESSED_JD_NAME
    return unprocessed if unprocessed.is_file() else None


def is_processed(app_dir: Path) -> bool:
    return (app_dir / PROCESSED_JD_NAME).is_file()


def mark_processed(app_dir: Path) -> None:
    """Renames jd_input.json -> jd_input.processed.json. Called once,
    right after py_stage9_assemble.py has produced real output for
    this application -- see py_pipeline_assemble.py. No-op if already
    renamed (idempotent, safe to call from --force re-runs)."""
    src = app_dir / UNPROCESSED_JD_NAME
    if src.is_file():
        src.rename(app_dir / PROCESSED_JD_NAME)


def is_example_file(path: Path) -> bool:
    """True for CVTailor's example/placeholder-file convention:
    <name>.example.<ext> (e.g. backend.example.json, anthropic_key.example.txt)
    -- these ship in git as templates to copy from, never as real input
    for the pipeline to discover or route to. Matched by the second-to-
    last dot-segment, not a name substring, so a real file that happens
    to contain "example" elsewhere in its name (e.g. "example_role.json")
    isn't mistakenly excluded."""
    return path.suffixes[-2:-1] == [".example"]


def discover_single_file(directory: Path, extensions: tuple[str, ...], label: str) -> Path:
    """Finds exactly one file with a matching extension in `directory`,
    skipping *.example.<ext> template files (see is_example_file).
    Raises with a clear, specific message if there's zero or more than
    one — ambiguity here should stop the batch immediately rather than
    silently picking one."""
    directory.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions and not is_example_file(p)
    )
    if len(candidates) == 0:
        raise FileNotFoundError(
            f"No {label} found in {directory}/ (expected a file with extension "
            f"{'/'.join(extensions)})."
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"Expected exactly one {label} in {directory}/, found {len(candidates)}: "
            f"{[c.name for c in candidates]}. Remove the extras — CVTailor won't guess "
            f"which one you meant."
        )
    return candidates[0]


def discover_single_file_optional(directory: Path, extensions: tuple[str, ...], label: str) -> Path | None:
    """Like discover_single_file, but returns None instead of raising when
    zero files are found — for config inputs that are genuinely optional
    (e.g. cover_letter_template/). Still raises on ambiguity (2+ files),
    since silently guessing wrong there is worse than not having the
    feature at all. Also skips *.example.<ext> template files (see
    is_example_file)."""
    directory.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions and not is_example_file(p)
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"Expected at most one {label} in {directory}/, found {len(candidates)}: "
            f"{[c.name for c in candidates]}. Remove the extras."
        )
    return candidates[0]


def load_applicant_info() -> dict:
    if not APPLICANT_INFO_PATH.is_file():
        raise FileNotFoundError(
            f"{APPLICANT_INFO_PATH} not found. Create it with at least:\n"
            '  {"name": "Your Name", "email": "you@example.com", "phone": "215-555-0100"}\n'
            "(email and phone are optional; name is required)"
        )
    data = json.loads(APPLICANT_INFO_PATH.read_text(encoding="utf-8"))
    if not data.get("name"):
        raise ValueError(f"{APPLICANT_INFO_PATH} needs a non-empty 'name' field.")
    return data


def safe_id_from_filename(path: Path) -> str:
    """The JD's own filename (minus extension) becomes the canonical
    id for that application — used for applications/<id>/. Only
    strips characters that are actually invalid in Windows filenames;
    otherwise preserves whatever naming convention you already used,
    since it came from a real filename to begin with."""
    stem = path.stem
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", stem).strip()
    return cleaned or "unnamed"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- Resume variant detection + routing ----------
#
# Moved here from src/stages/py_stage01_score_jd_input.py so run_pipeline.py can use
# the exact same classifier for routing that stage1 uses for
# suggesting -- one source of truth, no risk of the two drifting apart.
# Deterministic keyword check, NOT an LLM call -- cheap, and doesn't
# need to be an LLM call for a question this well-defined.
#
# Which resumes exist, what routes a JD to each one, and which template
# each one assembles into all live in config/resume_variants/*.json --
# never hardcoded here. That's the point: this file used to assume
# exactly two variants named "Backend" and "Full Stack", detected via
# a hardcoded frontend-keyword list and filename substring matching.
# Anyone who clones this repo with a different set of resumes (a third
# "AI Engineer" variant, a single generalist resume, whatever) couldn't
# use it without editing this module. Now a variant is just a JSON file
# -- see config/resume_variants/*.example.json for the shape.

@dataclass
class ResumeVariant:
    label: str
    resume_path: Path
    template_path: Path | None   # None -- no per-variant template, see discover_resume_template_variants()
    keywords: list[str]          # lowercased; empty is valid (e.g. the default variant)
    is_default: bool             # the fallback when no variant's keywords match
    source: Path                 # which config file this came from, for error messages


def load_resume_variants() -> dict[str, ResumeVariant]:
    """Reads every config/resume_variants/*.json (skipping *.example.json
    -- those are shipped templates to copy from, not live config) and
    returns {label: ResumeVariant}. This is the single source of truth
    for which resumes exist, which template each one assembles into,
    and which keywords route a JD to it."""
    RESUME_VARIANTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p for p in RESUME_VARIANTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".json" and not is_example_file(p)
    )
    if not files:
        raise FileNotFoundError(
            f"No resume variant config found in {RESUME_VARIANTS_DIR}/ -- copy "
            f"job_title.example.json there to a real .json file (e.g. backend.json), point "
            f"its \"resume\" field at your actual resume in {RESUME_DIR}/, and repeat -- once "
            f"per copy, with its own filename and label -- for each resume you want the "
            f"pipeline to route between."
        )

    variants: dict[str, ResumeVariant] = {}
    default_label: str | None = None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e

        label = data.get("label")
        resume_name = data.get("resume")
        if not label or not resume_name:
            raise ValueError(
                f"{path} is missing required field(s) -- needs at least \"label\" and "
                f"\"resume\" (the resume's filename inside {RESUME_DIR}/)."
            )
        if label in variants:
            raise RuntimeError(
                f"Two resume variant configs both use label {label!r}: "
                f"{variants[label].source.name} and {path.name}. Rename one so the "
                f"variant is unambiguous."
            )

        resume_path = RESUME_DIR / resume_name
        if not resume_path.is_file():
            raise FileNotFoundError(f"{path} points \"resume\" at {resume_path}, which doesn't exist.")

        template_name = data.get("resume_template")
        template_path: Path | None = None
        if template_name:
            template_path = RESUME_TEMPLATE_DIR / template_name
            if not template_path.is_file():
                raise FileNotFoundError(
                    f"{path} points \"resume_template\" at {template_path}, which doesn't exist."
                )

        is_default = bool(data.get("default", False))
        if is_default:
            if default_label is not None:
                raise RuntimeError(
                    f"Two resume variant configs are both marked \"default\": true -- "
                    f"{default_label!r} and {label!r}. Exactly one variant must be the "
                    f"default (used when a JD matches no variant's keywords)."
                )
            default_label = label

        variants[label] = ResumeVariant(
            label=label,
            resume_path=resume_path,
            template_path=template_path,
            keywords=[str(k).strip().lower() for k in data.get("keywords", []) if str(k).strip()],
            is_default=is_default,
            source=path,
        )

    if default_label is None:
        raise RuntimeError(
            f"No resume variant config in {RESUME_VARIANTS_DIR}/ is marked \"default\": true -- "
            f"exactly one must be, as the fallback when a JD matches no variant's keywords "
            f"(usually your most general resume, e.g. Backend)."
        )

    return variants


def _find_signal_hits(texts: list[str], keywords: list[str]) -> list[str]:
    """Returns which of `keywords` (already lowercased) actually appear
    across the given texts, case-insensitive, deduplicated by input order."""
    combined = " ".join(t.lower() for t in texts if t)
    return [kw for kw in keywords if kw in combined]


def suggest_resume_variant(jd) -> tuple[str, str]:
    """Suggests which resume variant fits a JD by checking each non-
    default variant's keywords against the JD text -- NOT by
    classifying the job title, which is an unreliable signal for this.
    Checks requirements_raw + responsibilities_raw first (the fields
    harvester_adapter.py documents as primary/authoritative), falling
    back to full_description_text only if both are empty, and to the
    config-designated default variant if nothing matches anywhere.
    When more than one variant's keywords hit, the variant with the
    most distinct keyword hits wins. Returns (suggestion, reasoning).
    `jd` is a schemas.JDInput or anything with the same attribute
    names."""
    variants = load_resume_variants()
    default_variant = next(v for v in variants.values() if v.is_default)
    scored_variants = [v for v in variants.values() if not v.is_default and v.keywords]

    def best_hit(texts: list[str]) -> tuple[str, list[str]] | None:
        by_label = {v.label: _find_signal_hits(texts, v.keywords) for v in scored_variants}
        by_label = {label: hits for label, hits in by_label.items() if hits}
        if not by_label:
            return None
        best_label = max(by_label, key=lambda l: len(by_label[l]))
        return best_label, by_label[best_label]

    primary_texts = list(jd.requirements_raw) + list(jd.responsibilities_raw)
    hit = best_hit(primary_texts)
    if hit:
        label, hits = hit
        return label, f"Signals in requirements/responsibilities: {', '.join(hits)}"

    if not primary_texts:
        hit = best_hit([jd.full_description_text])
        if hit:
            label, hits = hit
            return label, (f"No structured requirements/responsibilities available; "
                            f"signals found in full description text: {', '.join(hits)} "
                            f"(weaker signal -- verify manually)")
        return default_variant.label, (f"No signals found anywhere in the JD (checked full "
                                        f"description text only) -- using default variant "
                                        f"{default_variant.label!r}")

    hit = best_hit(jd.nice_to_have_raw)
    if hit:
        label, hits = hit
        return default_variant.label, (f"No signals in requirements/responsibilities; {label} "
                                        f"signals mentioned only under nice-to-have ({', '.join(hits)}) "
                                        f"-- not core to the role, using default variant "
                                        f"{default_variant.label!r}")

    return default_variant.label, (f"No signals found in requirements, responsibilities, or "
                                    f"nice-to-have -- using default variant {default_variant.label!r}")


def slugify_variant(label: str) -> str:
    return re.sub(r"[^\w]+", "_", label.strip().lower()).strip("_") or "default"


def discover_resume_variants() -> dict[str, Path]:
    """Returns {variant_label: resume_path} for every resume variant
    configured in config/resume_variants/*.json -- see
    load_resume_variants(). Kept as a thin wrapper so callers that only
    need paths (ensure_variant_ingest, py_pipeline_precheck.py) don't
    need to know about the full ResumeVariant config."""
    return {v.label: v.resume_path for v in load_resume_variants().values()}


def ensure_variant_ingest(force: bool = False, dry_run: bool = False, provider: str = "lmstudio") -> tuple[dict[str, Path], Path, Path]:
    """Runs stage 2 once per resume variant (not once per JD, and not
    just once overall) -- claims_ledger content genuinely differs per
    resume, but style_profile is voice-only and shouldn't meaningfully
    vary by which resume you're using, so it's computed once and
    shared. Re-runs a variant automatically if that resume's content
    (or the shared cover letter sample) changed since last time, or if
    forced. Returns ({variant_label: ledger_path}, shared_style_path,
    cover_letter_sample_path)."""
    variants = discover_resume_variants()
    cover_letter_path = discover_single_file(COVER_LETTER_SAMPLE_DIR, (".txt",), "cover letter sample")

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    variants_dir = SHARED_DIR / "variants"
    hash_file = SHARED_DIR / ".source_hashes.json"
    all_hashes = json.loads(hash_file.read_text()) if hash_file.is_file() else {}

    ledger_paths: dict[str, Path] = {}
    canonical_style_path = SHARED_DIR / "style_profile.json"
    hashes_changed = False

    for label, resume_path in variants.items():
        slug = slugify_variant(label)
        variant_dir = variants_dir / slug
        ledger_path = variant_dir / "claims_ledger.json"
        variant_style_path = variant_dir / "style_profile.json"

        current_hash = {
            "resume": file_hash(resume_path), "cover_letter": file_hash(cover_letter_path),
            "provider": provider,
        }
        needs_run = force or not ledger_path.is_file() or all_hashes.get(slug) != current_hash

        if needs_run:
            print(f"Ingesting {resume_path.name} (variant: {label}) + {cover_letter_path.name}...")
            cmd = [sys.executable, stage_script("py_stage02_ingest_cv_claims.py"),
                   "--resume", str(resume_path),
                   "--cover-letter-sample", str(cover_letter_path),
                   "--out-dir", str(variant_dir),
                   "--provider", provider]
            if dry_run:
                cmd.append("--dry-run")
            sh(cmd)
            if not dry_run:
                all_hashes[slug] = current_hash
                hashes_changed = True
        else:
            print(f"Reusing cached {ledger_path} (variant: {label}, unchanged).")

        ledger_paths[label] = ledger_path
        # First variant's style profile becomes the canonical shared
        # one -- see docstring. Only copies once; later variants don't
        # overwrite it, avoiding a spurious "different every run" look
        # from LLM non-determinism on a value that isn't supposed to
        # vary by resume anyway.
        if not canonical_style_path.is_file() and variant_style_path.is_file():
            shutil.copy(variant_style_path, canonical_style_path)

    if hashes_changed:
        hash_file.write_text(json.dumps(all_hashes, indent=2))

    return ledger_paths, canonical_style_path, cover_letter_path


def pick_ledger_for_jd(jd, ledger_paths: dict[str, Path]) -> tuple[Path, str, str]:
    """Routes a JD to the right variant's claims ledger using
    suggest_resume_variant(). Falls back to the config-designated
    default variant (see load_resume_variants()) with a clear warning
    if the suggestion somehow doesn't match any ledger actually on hand
    -- shouldn't normally happen since suggest_resume_variant() only
    ever suggests variants it loaded from the same config, but this
    keeps the caller from crashing if ledger_paths was built from a
    stale or filtered subset. Returns (ledger_path, variant_used,
    reasoning)."""
    suggestion, reasoning = suggest_resume_variant(jd)
    if suggestion in ledger_paths:
        return ledger_paths[suggestion], suggestion, reasoning
    default_label = next(v.label for v in load_resume_variants().values() if v.is_default)
    fallback = default_label if default_label in ledger_paths else sorted(ledger_paths)[0]
    reasoning = (f"suggested {suggestion!r} but no ledger for that variant is available "
                 f"(have: {sorted(ledger_paths)}) -- using {fallback!r} instead. {reasoning}")
    return ledger_paths[fallback], fallback, reasoning


def discover_resume_template_variants() -> dict[str, Path]:
    """Returns {variant_label: template_path} for every resume variant
    that names a "resume_template" in its config/resume_variants/*.json
    (see load_resume_variants()) -- the actual document stage 9 edits
    in place, separate from the resume text used for claims-ledger
    extraction. If no variant names a template at all, falls back to
    the single file in config/resume_template/ (the common case if your
    document structure doesn't need to differ per variant, only the
    resume TEXT does), returned as {"*": template_path} meaning "use
    for every variant". Naming a template for some variants but not
    others is treated as a config error -- that's ambiguous, not a
    sensible partial default."""
    variants = load_resume_variants()
    explicit = {v.label: v.template_path for v in variants.values() if v.template_path is not None}
    missing = [v.label for v in variants.values() if v.template_path is None]

    if explicit and missing:
        raise RuntimeError(
            f"\"resume_template\" is set for {sorted(explicit)} but missing for {sorted(missing)} "
            f"across config/resume_variants/*.json -- specify it for every variant, or omit it "
            f"everywhere to share one template across all variants."
        )
    if explicit:
        return explicit

    template = discover_single_file(RESUME_TEMPLATE_DIR, (".docx",), "resume template")
    return {"*": template}


def pick_template_for_variant(variant_used: str, templates: dict[str, Path]) -> Path:
    """Routes to the matching resume template, mirroring
    pick_ledger_for_jd()'s fallback behavior. "*" means a single
    shared template used regardless of variant (see
    discover_resume_template_variants())."""
    if "*" in templates:
        return templates["*"]
    if variant_used in templates:
        return templates[variant_used]
    fallback = sorted(templates)[0]
    return templates[fallback]