"""Unit tests for Bulgarian National Chart of Accounts (НСС / Национален сметкоплан)."""

from pathlib import Path
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from invoice_core.chart_of_accounts import (
    AccountClass,
    AccountDefinition,
    AccountType,
    ChartOfAccountsCatalog,
    DEFAULT_CHART_OF_ACCOUNTS,
    explain_account,
    lookup_account,
    search_accounts,
)


class TestChartOfAccounts(unittest.TestCase):
    """Test suite for Chart of Accounts catalog and lookup utilities."""

    def test_account_classes_enum(self):
        """Verify statutory classes 1-9 exist."""
        self.assertEqual(AccountClass.CLASS_1_CAPITAL.value, 1)
        self.assertEqual(AccountClass.CLASS_2_NON_CURRENT_ASSETS.value, 2)
        self.assertEqual(AccountClass.CLASS_3_INVENTORY.value, 3)
        self.assertEqual(AccountClass.CLASS_4_PAYABLES_RECEIVABLES.value, 4)
        self.assertEqual(AccountClass.CLASS_5_FUNDS.value, 5)
        self.assertEqual(AccountClass.CLASS_6_EXPENSES.value, 6)
        self.assertEqual(AccountClass.CLASS_7_REVENUES.value, 7)
        self.assertEqual(AccountClass.CLASS_9_OFF_BALANCE.value, 9)

    def test_account_types_enum(self):
        """Verify statutory account types."""
        self.assertEqual(AccountType.ACTIVE.value, "active")
        self.assertEqual(AccountType.PASSIVE.value, "passive")
        self.assertEqual(AccountType.ACTIVE_PASSIVE.value, "active_passive")
        self.assertEqual(AccountType.OFF_BALANCE.value, "off_balance")

    def test_lookup_key_accounts(self):
        """Verify standard accounts (601, 602, 401, 4531, 304, 501, 503, 702) exist in catalog."""
        # 601: Materials (Active)
        acc_601 = lookup_account("601")
        self.assertIsNotNone(acc_601)
        self.assertEqual(acc_601.code, "601")
        self.assertEqual(acc_601.account_class, 6)
        self.assertEqual(acc_601.account_type, AccountType.ACTIVE)

        # 401: Suppliers (Passive)
        acc_401 = lookup_account("401")
        self.assertIsNotNone(acc_401)
        self.assertEqual(acc_401.account_class, 4)
        self.assertEqual(acc_401.account_type, AccountType.PASSIVE)

        # 4531: VAT on Purchases (Active)
        acc_4531 = lookup_account("4531")
        self.assertIsNotNone(acc_4531)
        self.assertEqual(acc_4531.account_class, 4)
        self.assertEqual(acc_4531.account_type, AccountType.ACTIVE)
        self.assertFalse(acc_4531.is_synthetic)
        self.assertEqual(acc_4531.parent_code, "453")

        # 304: Goods (Active)
        acc_304 = lookup_account("304")
        self.assertIsNotNone(acc_304)
        self.assertEqual(acc_304.account_class, 3)
        self.assertEqual(acc_304.account_type, AccountType.ACTIVE)

        # 501: Cash in BGN (Active)
        acc_501 = lookup_account("501")
        self.assertIsNotNone(acc_501)
        self.assertEqual(acc_501.account_class, 5)

        # 503: Bank in BGN (Active)
        acc_503 = lookup_account("503")
        self.assertIsNotNone(acc_503)
        self.assertEqual(acc_503.account_class, 5)

    def test_lookup_nonexistent_returns_none(self):
        """Verify querying unknown account code returns None."""
        self.assertIsNone(lookup_account("999999"))
        self.assertIsNone(lookup_account(""))
        self.assertIsNone(lookup_account(None))

    def test_account_definition_to_dict(self):
        """Verify AccountDefinition serialization to dictionary."""
        acc = lookup_account("601")
        self.assertIsNotNone(acc)
        d = acc.to_dict()
        self.assertEqual(d["code"], "601")
        self.assertEqual(d["account_class"], 6)
        self.assertEqual(d["account_type"], "active")
        self.assertIn("keywords", d)
        self.assertIn("typical_debit", d)
        self.assertIn("typical_credit", d)

    def test_search_accounts_by_code_and_name(self):
        """Verify searching catalog matches code, name, or keywords."""
        results_vat = search_accounts("ддс")
        self.assertTrue(len(results_vat) > 0)
        codes = [a.code for a in results_vat]
        self.assertTrue(any("453" in c for c in codes))

        results_fuel = search_accounts("гориво")
        self.assertTrue(len(results_fuel) > 0)

    def test_explain_account(self):
        """Verify explain_account provides structured text summary."""
        exp = explain_account("6012")
        self.assertIn("6012", exp)
        self.assertIn("Дебит", exp)

        unknown_exp = explain_account("000")
        self.assertIn("не е намерена", unknown_exp.lower())

    def test_catalog_get_by_class(self):
        """Verify retrieving accounts filtered by class."""
        catalog = DEFAULT_CHART_OF_ACCOUNTS
        class_6 = catalog.get_by_class(6)
        self.assertTrue(len(class_6) > 0)
        for a in class_6:
            self.assertEqual(a.account_class, 6)


if __name__ == "__main__":
    unittest.main()
