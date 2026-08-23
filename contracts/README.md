# CVTailor Data Contracts

This directory defines the exact JSON data structure that the Job Data Harvester (Chrome extension) must produce in order to be accepted by Stage 0 of the CVTailor pipeline. 

It acts as the strict boundary between untrusted web scraping and the trusted local pipeline.

## The Files

*   **`jd_input.schema.json`**: The machine-readable JSON schema defining all acceptable fields, data types, and fallback behaviors. 
*   **`jd_input.example.json`**: A sample payload demonstrating a perfectly formed, fully enriched job description extraction.

## The Contract Rules

While the schema includes many fields for rich data extraction (like `salary_max`, `remote_type`, and `nice_to_have_raw`), the pipeline is designed to be highly fault-tolerant. 

**There are only three strictly required fields:**
1.  `company`
2.  `role_title`
3.  `full_description_text`

Everything else is considered "best-effort enrichment". If the extension can only successfully scrape the raw body text (`full_description_text`), that is enough for the pipeline to function. Pre-parsed arrays (like `requirements_raw`) simply make the LLM's job easier and cheaper. 

*(Developer Tip: When building or modifying the extension scraper, look for `<script type="application/ld+json">` tags with `"@type": "JobPosting"` on the webpage first—this maps almost directly to this schema and is far more reliable than DOM scraping!)*

## Maintaining the Schema

**Do not hand-edit `jd_input.schema.json`.** 

The schema is automatically generated from the Pydantic models defined in the core pipeline code. If you update the Python models and the schema drifts, regenerate these files by running:

```bash
python src/util/py_export_jd_input_schema.py
```