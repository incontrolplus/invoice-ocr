## 2026-09-04T21:22:10Z

You are the E2E Test Suite Author (teamwork_preview_test_writer).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_test_writer_e2e
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Prior survey reports to read:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for testing.

Task:
Build the complete, opaque-box E2E Test Suite per the Dual Track specifications:
1. Create /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/TEST_INFRA.md following the standard template in PROJECT.md:
   - Test philosophy (opaque-box, requirement-driven)
   - Feature inventory mapping to test tiers
   - Test architecture & test runner invocation
   - Real-world application scenarios
   - Coverage thresholds
2. Build comprehensive test cases across all 4 tiers in `tests/e2e/`:
   - Tier 1: Feature Coverage (>=5 test cases per feature across R1-R6)
   - Tier 2: Boundary & Corner Cases (>=5 test cases per feature covering empty inputs, corrupt files, max values, decimal rounding extremes, Mod-11/Mod-97 checksum variations, date boundaries, Euro 2026-01-01 and 2026-08-08 transitions)
   - Tier 3: Cross-Feature Combinations (pairwise interactions: multi-page + table + dual currency; OCR noise + Mod-11 check; batch CLI + debug flags)
   - Tier 4: Real-World Scenarios (including testing the 3 Kapina acceptance files at `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/` under strict read-only mode, checking line items count, tax base, VAT, total, currency, validation status, and verifying 0 files modified on external volume).
3. Create an E2E test runner script (`run_e2e_tests.py` or pytest configuration) that executes the suite, outputs clear progress/results, and returns exit code 0 when all tests pass.
4. When test suite creation is complete and verified, publish /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/TEST_READY.md with the full coverage summary and test runner command.
5. Write your handoff report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_test_writer_e2e/handoff.md
   and notify the orchestrator.
