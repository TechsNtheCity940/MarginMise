# Quick Install

Extract the ZIP. The `restaurant-cost-controller` folder is an installable Hermes profile distribution.

## Windows PowerShell

```powershell
hermes profile install "C:\FULL\PATH\TO\restaurant-cost-controller" --alias
restaurant-cost-controller setup
restaurant-cost-controller chat
```

## Linux, macOS, or WSL

```bash
hermes profile install "/full/path/to/restaurant-cost-controller" --alias
restaurant-cost-controller setup
restaurant-cost-controller chat
```

You may instead paste `prompts/CREATE_PROFILE_WITH_HERMES.txt` into your existing Hermes profile.

## First production steps

1. Edit the business configuration.
2. Upload or convert the workbook template to Google Sheets.
3. Connect the invoice folders.
4. Run the sample invoice.
5. Verify Review Queue behavior.
6. Process real invoices only after the dry test.

Do not store API keys, passwords, or OAuth tokens in this package.
