# Job Data Harvester (Chrome Extension)

A lightweight browser extension designed specifically for the CVTailor pipeline. It extracts structured job details directly from web pages and formats them for ingestion by Stage 0 (`py_stage00_normalize_jd_input.py`).

## Installation

1. Open Chrome and navigate to `chrome://extensions/`.
2. Toggle **Developer mode** on (top-right corner).
3. Click **Load unpacked** and select this directory (`extension/chrome/`).

## How It Works

* **Extraction:** Scrapes standard job fields (Title, Company, Responsibilities, Requirements, etc.) into a 13-key schema.
* **Pipeline Delivery:** 
  * If the **Native Messaging Host** is configured, extracted data is sent directly to your active CVTailor queue (`applications/<app_id>/jd_input.json`).
  * Otherwise, it downloads a JSON file to your `Downloads` folder for manual ingestion.