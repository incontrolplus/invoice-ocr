## 2026-09-04T21:15:12Z
You are Survey Agent 1 (teamwork_preview_spec_miner).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1
Authoritative Requirements file: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md

You MUST read /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md before starting work.

Task:
Conduct an in-depth specification mining and requirements survey for the production-ready Bulgarian Invoice OCR & Document Understanding pipeline.
Investigate and document:
1. All explicit requirements (R1 through R6) and acceptance criteria in ORIGINAL_REQUEST.md.
2. Bulgarian statutory invoice rules:
   - Metadata: Invoice number (typically 10 digits), date issued, tax event date (дата на данъчно събитие), place of issue.
   - Parties: Supplier (Доставчик) vs Recipient (Получател), name, address, MOL (МОЛ).
   - Identifiers: EIK/BULSTAT (ЕИК/БУЛСТАТ - 9 digits or 13 digits with standard modulo-11 checksum validation algorithm), VAT ID (ЗДДС - BG prefix followed by 9 or 10 digits).
   - Payment details: Bank name, Bulgarian IBAN format (BG + 2 check digits + 4-char bank code + 4-char branch/sort code + 8-char account number, total 22 characters), BIC/SWIFT.
   - Financial & VAT: Tax base (данъчна основа), VAT rate (20% standard, 9% reduced, 0%, exempt), VAT amount (начислен ДДС), total amount due (сума за плащане / общо). Tolerances (0.01/0.02).
   - Euro-Transition (2026): Rule for dates >= 2026-01-01 (warning if BGN, no auto-convert to EUR), rule for dates >= 2026-08-08 (flag BGN as primary payable currency warning). Dual-display and amount-in-words (словом) cross-check without modifying extracted numbers.
3. Strict 3-layer architecture required: (1) Raw OCR Evidence with bounding boxes, (2) Normalized Data with Decimal and explicit currency objects { "amount": ..., "currency": ... }, (3) Validation Results (is_valid, errors, warnings).
4. CLI options (--input-dir, --output-dir, --debug, --debug-dir) and stdout/stderr separation rules.

Output:
Write a comprehensive handoff report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1/handoff.md
Update progress.md as you work. When finished, send a message to orchestrator.
