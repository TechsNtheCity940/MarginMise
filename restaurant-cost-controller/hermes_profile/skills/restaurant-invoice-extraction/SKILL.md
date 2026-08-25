---
name: restaurant-invoice-extraction
description: Extract and structure restaurant invoice documents using raw-text and canonical-JSON artifacts.
version: 2.4.0
author: Restaurant Cost Controller
license: Private Use
metadata:
  hermes:
    tags: [Restaurant, Invoice, PDF, OCR, Extraction]
---

# Restaurant Invoice Extraction

## Mission

Produce auditable raw text and canonical invoice JSON. Do not write to the ledger.

## Text supplied in the prompt

When PDF text is supplied directly, parse it into canonical JSON and return JSON only.

## Local document job

When SOURCE_FILE, RAW_TEXT_OUTPUT, and CANONICAL_JSON_OUTPUT are supplied:

1. Confirm the local source exists.
2. Use terminal tools and the bundled `ocr-and-documents` skill.
3. For a text PDF, extract the existing text layer with PyMuPDF.
4. For an image-only PDF or photograph, use marker-pdf or another document tool available through the skill.
5. Write complete raw text or Markdown to RAW_TEXT_OUTPUT.
6. Write one canonical JSON object to CANONICAL_JSON_OUTPUT.
7. Verify both files before replying `DONE`.
8. Do not assume a local path is an attached image.
9. Do not return a blank invoice merely because the layout is unfamiliar.

## Canonical fields

Header: vendor, invoice_number, invoice_date, subtotal, fees, tax, credits, total, currency, document_type, layout_recognized, extraction_confidence, extraction_notes.

Each item: sku, description, category, quantity, unit, unit_price, line_total, confidence.

Preserve printed descriptions, leave unreadable values empty, treat credits as positive amounts, and verify arithmetic.

## Management data boundary

Do not modify physical inventory counts, monthly closes, or manager-reviewed order quantities. Those records belong to the deterministic application ledger.
