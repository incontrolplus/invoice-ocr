#!/usr/bin/env python3
"""Unified E2E Test Runner for Bulgarian Invoice OCR Pipeline.

Executes tests across all 4 tiers or selected tiers:
  Tier 1: Feature Coverage (R1-R6)
  Tier 2: Boundary & Corner Cases
  Tier 3: Cross-Feature Combinations
  Tier 4: Real-World Scenarios (Kapina acceptance dataset)

Usage:
  python run_e2e_tests.py                 # Run all tiers
  python run_e2e_tests.py --tier 1        # Run only Tier 1
  python run_e2e_tests.py --tier 4        # Run only Tier 4
  python run_e2e_tests.py -v              # Run with verbose output
"""
import argparse
import sys
import time
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TIER_MODULES = {
    1: ("Tier 1: Feature Coverage (R1-R6)", "tests.e2e.test_tier1_features"),
    2: ("Tier 2: Boundary & Corner Cases", "tests.e2e.test_tier2_boundaries"),
    3: ("Tier 3: Cross-Feature Combinations", "tests.e2e.test_tier3_combinations"),
    4: ("Tier 4: Real-World Scenarios (Kapina)", "tests.e2e.test_tier4_realworld"),
}


def run_tier(tier_num: int, verbose: bool = False) -> tuple[int, int, int, int, float, list[str]]:
    """Run a single test tier and return metrics.

    Returns:
        (total, passed, failures, errors, elapsed_seconds, failure_details)
    """
    tier_name, module_name = TIER_MODULES[tier_num]
    print(f"\n{'='*70}")
    print(f"▶ Running {tier_name} ({module_name})")
    print(f"{'='*70}")

    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromName(module_name)
    except Exception as exc:
        print(f"❌ Failed to load test module {module_name}: {exc}")
        return 0, 0, 0, 1, 0.0, [f"Module load error: {exc}"]

    start_time = time.time()
    verbosity = 2 if verbose else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    elapsed = time.time() - start_time

    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    passed = total - failures - errors

    failure_details = []
    for test, trace in result.failures:
        failure_details.append(f"FAIL: {test.id()}\n{trace}")
    for test, trace in result.errors:
        failure_details.append(f"ERROR: {test.id()}\n{trace}")

    return total, passed, failures, errors, elapsed, failure_details


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulgarian Invoice OCR E2E Test Runner")
    parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2, 3, 4],
        help="Execute only tests from specified tier (1, 2, 3, or 4)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Display detailed per-test execution information",
    )
    args = parser.parse_args()

    tiers_to_run = [args.tier] if args.tier else [1, 2, 3, 4]

    summary_rows = []
    all_failure_details = []
    grand_total = 0
    grand_passed = 0
    grand_failures = 0
    grand_errors = 0
    total_elapsed = 0.0

    print("=" * 70)
    print("🇧🇬 BULGARIAN INVOICE OCR — END-TO-END TEST SUITE RUNNER")
    print(f"Target Tiers: {', '.join(str(t) for t in tiers_to_run)}")
    print("=" * 70)

    for tier_num in tiers_to_run:
        tier_desc, _ = TIER_MODULES[tier_num]
        tot, p, f, e, elap, fails = run_tier(tier_num, verbose=args.verbose)
        status = "✅ PASS" if (f == 0 and e == 0 and tot > 0) else "❌ FAIL"
        summary_rows.append((f"Tier {tier_num}", tot, p, f, e, f"{elap:.2f}s", status))

        grand_total += tot
        grand_passed += p
        grand_failures += f
        grand_errors += e
        total_elapsed += elap
        all_failure_details.extend(fails)

    # Print summary table
    print("\n" + "=" * 70)
    print("TEST SUITE EXECUTION SUMMARY")
    print("=" * 70)
    headers = ["Tier", "Total", "Passed", "Failed", "Errors", "Time", "Status"]
    col_widths = [10, 8, 8, 8, 8, 10, 10]

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)

    for row in summary_rows:
        row_str = " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))
        print(row_str)

    print(sep_line)
    overall_status = "✅ ALL PASSED" if (grand_failures == 0 and grand_errors == 0) else "❌ ISSUES DETECTED"
    totals_row = [
        "TOTAL",
        grand_total,
        grand_passed,
        grand_failures,
        grand_errors,
        f"{total_elapsed:.2f}s",
        overall_status,
    ]
    print(" | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(totals_row)))
    print("=" * 70)

    if all_failure_details:
        print(f"\n⚠️  {grand_failures + grand_errors} test issue(s) recorded during execution.")
        print("Note: In Dual Track development, test failures identify implementation gaps")
        print("scheduled for resolution in subsequent milestones (M1 through M6).")

    # Exit code: 0 if all passed, 1 if any failure or error
    return 0 if (grand_failures == 0 and grand_errors == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
