# Validation Rules

## Automatic approval requirements

- Vendor, date, invoice number, and total are present.
- The duplicate key is not already posted.
- Line sum matches subtotal within tolerance.
- Subtotal + fees + tax - credits matches total.
- Every line has description, quantity, unit price, and line total.
- Every line meets the OCR-confidence threshold.
- Every product is matched or explicitly created as a new item.

## Review triggers

Missing SKU, low confidence, arithmetic mismatch, duplicate-looking invoice, description-only match, major price increase, nonpositive quantity, changed pack size, or unavailable source document.

## Defaults

- Arithmetic tolerance: $0.05
- OCR confidence: 0.85
- Price alert: 5%
