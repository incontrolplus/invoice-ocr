# BRIEFING — 2026-09-05T00:18:35+03:00

## Mission
Conduct in-depth specification mining and domain requirements survey for Bulgarian Invoice OCR & Document Understanding pipeline.

## 🔒 My Identity
- Archetype: specification_miner
- Roles: domain_expert, specification_miner
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: survey

## 🔒 Key Constraints
- Specification miner role: Discover and document features and domain rules. Do NOT implement anything. Read-only.
- Strict 3-layer architecture discovery: Raw OCR Evidence with bounding boxes, Normalized Data with Decimal & explicit currency objects, Validation Results.
- Bulgarian statutory invoice rules: EIK/BULSTAT checksums, VAT ID, IBAN, dates, Euro-transition rules (2026-01-01, 2026-08-08), MOL, tax base, VAT rates.
- .agents holds only metadata. Write only to own folder (.agents/teamwork_preview_spec_miner_survey_1).

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: not yet

## Task Summary
- **What to build**: In-depth specification mining and domain survey for Bulgarian Invoice OCR pipeline.
- **Success criteria**: Comprehensive handoff report detailing R1-R6, statutory requirements, validation algorithms, architecture, and CLI contracts.
- **Interface contracts**: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
- **Code layout**: .agents/teamwork_preview_spec_miner_survey_1/handoff.md

## Key Decisions Made
- Survey completed: Documented 39 discovered features, 35 edge cases, exact mathematical and statutory algorithms for Bulgarian EIK (9 & 13 digit Modulo 11), IBAN (22-char ISO 7064 Modulo 97-10), VAT ID, Euro transition 2026-01-01 and 2026-08-08 rules, 3-layer architecture, and CLI contracts.
- Documented key implementation gaps in current codebase: missing PyMuPDF, missing PDF support, missing 3-layer JSON output structure, missing checksum algorithms, missing batch CLI arguments.

## Artifact Index
- handoff.md — Comprehensive specification mining report
- progress.md — Liveness heartbeat
- DISPATCH.md — Task assignment history

## Loaded Skills
- None
