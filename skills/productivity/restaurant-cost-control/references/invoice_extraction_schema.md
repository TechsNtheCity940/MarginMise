# Canonical Invoice Extraction Schema

```json
{
  "vendor": "Example Food Distributor",
  "invoice_number": "INV-10025",
  "invoice_date": "2026-07-13",
  "subtotal": 402.85,
  "fees": 8.00,
  "tax": 0.00,
  "credits": 5.00,
  "total": 405.85,
  "currency": "USD",
  "source_file": "invoice_INV-10025.pdf",
  "source_link": "",
  "items": [
    {
      "sku": "CB4010",
      "description": "CHX BRST BNLS SKNLS 4/10 LB",
      "quantity": 3,
      "unit": "case",
      "unit_price": 79.95,
      "line_total": 239.85,
      "confidence": 0.98
    }
  ]
}
```

Use ISO dates, positive credit values, unchanged original descriptions, and confidence values from 0 to 1. Leave unavailable fields blank instead of inventing them.
