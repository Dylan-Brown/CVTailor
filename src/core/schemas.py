#!/usr/bin/env python3
"""
schemas.py — the I/O contracts every stage reads and writes.

Every stage script is a standalone CLI that reads one or more of these
as JSON and writes one or more of these as JSON. That's the whole
orchestration model: stages don't call each other, they never import
each other, they just agree on these shapes. This is deliberate — it's
what lets run_pipeline.py / py_run_stage.py stay thin subprocess runners, and it's
what lets you swap in Windmill/n8n later without touching stage logic.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ---------- Stage 0: structured JD intake (from the Chrome extension) ----------

class WarmContact(BaseModel):
    """Best-effort point-of-contact info pulled from the JD page itself
    (poster, recruiter, hiring manager). Every field is optional and
    frequently empty -- means "not stated," not "extraction failed." """
    name: Optional[str] = None
    contact_method: Optional[str] = None  # e.g. "LinkedIn message", "Email"
    email: Optional[str] = None
    position: Optional[str] = None


class JDInput(BaseModel):
    """Contract for the Chrome extension's output. `full_description_text`
    is the only hard requirement -- everything else is best-effort
    enrichment the LLM can use when available."""
    source_url: Optional[str] = None  # stamped deterministically by the extension from chrome.tabs -- see harvester_adapter.py
    scraped_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    company: str
    role_title: str
    location: Optional[str] = None
    remote_type: Literal["remote", "hybrid", "onsite", "unspecified"] = "unspecified"
    employment_type: Optional[str] = None  # "full-time", "contract", etc.
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[Literal["year", "hour", "month"]] = None
    full_description_text: str  # required — the raw/plain-text JD body
    requirements_raw: list[str] = Field(
        default_factory=list,
        description="Bullet points if the page had a clearly delimited "
                    "requirements/qualifications list — the must-have bar. "
                    "Fine to leave empty — stage 4 will still derive "
                    "requirements from full_description_text via LLM if "
                    "this is empty.",
    )
    responsibilities_raw: list[str] = Field(
        default_factory=list,
        description="Bullet points describing what the role actually "
                    "involves day to day, if the page had a clearly "
                    "delimited responsibilities/duties list. Weighted "
                    "equally with requirements_raw in gap analysis — what "
                    "a role involves is just as much a fit signal as its "
                    "stated requirements bar.",
    )
    nice_to_have_raw: list[str] = Field(
        default_factory=list,
        description="Bullet points for preferred-but-not-required "
                    "qualifications, if the page had a clearly delimited "
                    "list. Lower priority than requirements_raw/"
                    "responsibilities_raw in gap analysis.",
    )
    extraction_method: Literal["json_ld", "dom_heuristic", "manual_paste"] = "dom_heuristic"
    extension_version: Optional[str] = None
    warm_contact: Optional[WarmContact] = None
    # wall-clock ms for the extension's own scrape+inference span
    # (see background.js runExtraction()); None if not captured via the extension.
    extraction_runtime_ms: Optional[float] = None
    # ISO timestamp the file-watcher trigger row was created; only set
    # for JDs that arrived via the intake-drop path. Field name must
    # match config/file_watcher.json's metrics.intake_timestamp_field.
    intake_enqueued_at: Optional[str] = None
    intake_trigger_id: Optional[int] = None  # matching trigger row's id; same field-name rule




class Claim(BaseModel):
    """One atomic, verifiable fact from your actual resume. This is the
    ledger — nothing downstream is allowed to assert something that
    doesn't trace back to a claim_id in here."""
    claim_id: str  # e.g. "C001"
    section: str  # "experience:capital_one:senior_engineer"
    text: str  # the original bullet, verbatim
    category: Literal["responsibility", "achievement", "skill", "metric", "education", "credential"]
    has_metric: bool = False


class ClaimsLedger(BaseModel):
    source_file: str
    # min_length=20 is a sanity floor against severe under-extraction --
    # a real senior resume should extract 30-50+ claims.
    claims: list[Claim] = Field(min_length=20)


class StyleProfile(BaseModel):
    """Voice fingerprint extracted from your sample cover letter.
    Content-free by design — we're capturing how you write, not what
    you said, so this can't leak old-job specifics into a new letter."""
    avg_sentence_length: float
    tone_descriptors: list[str]  # e.g. ["direct", "understated", "systems-oriented"]
    structural_notes: list[str]  # e.g. ["opens with a concrete example, not a summary line"]
    signature_phrases: list[str] = Field(default_factory=list)


class StyleLeakItem(BaseModel):
    field: Literal["tone_descriptors", "structural_notes", "signature_phrases"]
    exact_value: str  # verbatim — must match a real list entry in the
                       # extracted profile character-for-character, so
                       # it can be located and removed programmatically
                       # rather than requiring a human to find it
    reason: str


class StyleLeakCheck(BaseModel):
    """Verdict from a second pass checking whether the style extraction
    stayed content-free -- any real, checkable fact leaking through
    would reach generation with zero traceability. Runs once per batch."""
    has_leakage: bool
    items: list[StyleLeakItem] = Field(default_factory=list)
    reasoning: str


# ---------- Stage 3: Company / role research ----------

class ResearchFinding(BaseModel):
    claim: str
    source_url: Optional[str] = None
    relevance: Literal["role_specific", "team_specific", "company_general"]


class CompanyBrief(BaseModel):
    company: str
    role_title: str
    stack_signals: list[str] = Field(default_factory=list)
    culture_notes: list[ResearchFinding] = Field(default_factory=list)
    recent_news: list[ResearchFinding] = Field(default_factory=list)
    compensation_notes: list[ResearchFinding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)  # things search didn't resolve


# ---------- Stage 4: Gap / fit analysis ----------

class JDRequirement(BaseModel):
    text: str
    priority: Literal["required", "preferred"]
    matched_claim_ids: list[str] = Field(default_factory=list)


class EditBrief(BaseModel):
    requirements: list[JDRequirement]
    # ge/le enforced for real -- a bare float once let a model's 60.0
    # (meant as 60%) through as a fraction, printing as "6000%".
    coverage_score: float = Field(ge=0.0, le=1.0)  # matched required / total required
    gaps: list[str]  # requirements with zero matched claims
    overindexed_sections: list[str] = Field(default_factory=list)
    # actionable: a real claim supports the term but it's not in an edit yet.
    # unsupported: no claim mentions it -- surfaced to the candidate, not generation.
    keyword_gaps_actionable: dict[str, list[str]] = Field(default_factory=dict)
    keyword_gaps_unsupported: list[str] = Field(default_factory=list)


# ---------- Stage 5: Constrained generation ----------

class CandidateEdit(BaseModel):
    edit_id: str
    target: Literal["resume", "cover_letter"]
    section: str
    original_text: Optional[str] = None  # None for cover letter (no "original")
    suggested_text: str
    edit_type: Literal["reword", "reorder", "reweight", "new_sentence"]
    claim_ids_referenced: list[str] = Field(
        default_factory=list,
        description="Which ledger claims this edit draws on. Empty list "
                    "is a red flag and should be auto-discarded in stage 6.",
    )


# ---------- Stage 6: Verification (NEW — the fact-check gate) ----------

class VerificationResult(BaseModel):
    edit_id: str
    verdict: Literal["supported", "embellished", "fabricated"]
    unsupported_spans: list[str] = Field(default_factory=list)
    reasoning: str


class VerificationReport(BaseModel):
    results: list[VerificationResult]
    discarded_count: int
    flagged_count: int
    passed_count: int


# ---------- Stage 7: Adversarial utility critic ----------

class CritiqueResult(BaseModel):
    """Stage 6 asks 'is this true?'; this asks 'is this useful for THIS
    job?' Deliberately biased toward `cut` -- an edit has to earn its
    place, not merely avoid being wrong."""
    edit_id: str
    verdict: Literal["keep", "cut"]
    reasoning: str


class CritiqueReport(BaseModel):
    results: list[CritiqueResult]
    kept_count: int
    cut_count: int


# ---------- Stage 8: Human review ----------

class ReviewDecision(BaseModel):
    edit_id: str
    decision: Literal["approved", "rejected", "edited"]
    final_text: Optional[str] = None  # set if decision == "edited"


# ---------- Stage 9: Output assembly ----------

class AssemblyLogEntry(BaseModel):
    application_id: str
    company: str
    role_title: str
    timestamp: str
    edits_approved: int
    edits_rejected: int
    edits_discarded_stage6: int
    model_used: str