# Templates & Configuration

## Resume Template
**Location:** `config/resume_template/*.docx`
*   **No placeholder tokens:** Your name, contact info, and bullets are real baked-in text.
*   **How it works:** Stage 9 applies edits by verbatim text replacement. It searches for the claim's exact `original_text` and swaps in the `final_text` in place, preserving paragraph fonts, styles, and table structures.
*   **Formatting:** If a claim's text isn't found verbatim (due to whitespace or smart-quote drift), the edit is skipped with a warning. Progressive typography compression can automatically apply if the PDF exceeds page limits.

## Cover Letter Template
**Location:** `config/cover_letter_template/cover_letter_template.docx`
*   **Placeholder-driven:** Unlike the resume, Stage 9 explicitly looks for `{{TOKEN}}` paragraphs.
*   **Tokens Include:**
    *   `{{APPLICANT_NAME}}`, `{{ROLE_TITLE}}`, `{{COMPANY}}`.
    *   `{{ADDRESS}}` / `{{CONTACT_LINE}}`.
    *   `{{DATE}}`.
    *   `{{BODY}}` (The reviewed cover-letter paragraphs. Bullet lines get real hanging-indent formatting).

## General Configuration Files
*   **`applicant_info.json`:** Your contact information injected into generated materials.
*   **`file_watcher.json`:** Configurations for automated polling, LM Studio paths, timeouts, and metrics.
*   **`config/resume_variants/*.json`:** Routing keywords to decide which base resume and template to use.
*   **`config/cover_letter_sample/*.txt`:** A generic cover letter used to extract your writing style.