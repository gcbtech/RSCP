"""Tax and percentage rounding (integer cents, half-up).

With a 7.25% tax and a 7.25% cash discount the two must cancel exactly:
cash total == subtotal for every subtotal (the "extra penny" bug)."""
import unittest
from unittest.mock import patch

from app.routes.pos.core import calculate_tax, calculate_percentage


class TestRoundingFix(unittest.TestCase):
    def test_calculate_percentage_basic(self):
        """calculate_percentage rounds half-up on exact cents."""
        # $6.00 * 7.25% = 43.5 cents -> 44
        self.assertEqual(calculate_percentage(600, 7.25), 44)
        # $26.00 * 7.25% = 188.5 cents -> 189
        self.assertEqual(calculate_percentage(2600, 7.25), 189)
        # $50.00 * 7.25% = 362.5 cents -> 363
        self.assertEqual(calculate_percentage(5000, 7.25), 363)

    @patch('app.routes.pos.core.get_tax_rate', return_value=0.0725)
    def test_tax_and_discount_parity(self, mock_get_tax_rate):
        """Tax and cash discount at the same rate match exactly (no extra penny)."""
        rate = 7.25
        for subtotal in [600, 2600, 5000, 5400, 6600, 7000, 7400, 9400, 9800]:
            tax = calculate_tax(subtotal)
            discount = calculate_percentage(subtotal, rate)
            self.assertEqual(tax, discount, f"Mismatched tax ({tax}) and discount ({discount}) on {subtotal}")
            self.assertEqual(subtotal + tax - discount, subtotal)

    @patch('app.routes.pos.core.get_tax_rate', return_value=0.0725)
    def test_all_subtotals_up_to_100(self, mock_get_tax_rate):
        """Every subtotal from $0.01 to $100.00 has zero penny discrepancy."""
        rate = 7.25
        for subtotal in range(1, 10001):
            tax = calculate_tax(subtotal)
            discount = calculate_percentage(subtotal, rate)
            self.assertEqual(tax, discount, f"Mismatch at {subtotal}")
            self.assertEqual(subtotal + tax - discount, subtotal, f"Mismatch at {subtotal}")


if __name__ == '__main__':
    unittest.main()
