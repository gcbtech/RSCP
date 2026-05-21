import unittest
import sys
import os
from unittest.mock import patch

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.routes.pos.core import round_money, calculate_tax, calculate_percentage

class TestRoundingFix(unittest.TestCase):
    def test_calculate_percentage_basic(self):
        """Verify that calculate_percentage calculates percentage using Decimal and ROUND_HALF_UP."""
        # 6.00 * 7.25% should evaluate to exactly 0.44
        self.assertEqual(calculate_percentage(6.00, 7.25), 0.44)
        
        # 26.00 * 7.25% should evaluate to 1.89
        # (26.00 * 0.0725 = 1.885, which should round up to 1.89)
        self.assertEqual(calculate_percentage(26.00, 7.25), 1.89)
        
        # 50.00 * 7.25% should evaluate to 3.63
        # (50.00 * 0.0725 = 3.625, which should round up to 3.63)
        self.assertEqual(calculate_percentage(50.00, 7.25), 3.63)

    @patch('app.routes.pos.core.get_tax_rate', return_value=0.0725)
    def test_tax_and_discount_parity(self, mock_get_tax_rate):
        """Verify that tax and cash discount calculations match exactly, eliminating the extra penny."""
        rate = 7.25  # 7.25% tax and cash discount
        
        # Let's test all subtotals from $1.00 to $100.00 that previously failed
        test_subtotals = [6.00, 26.00, 50.00, 54.00, 66.00, 70.00, 74.00, 94.00, 98.00]
        
        for subtotal in test_subtotals:
            tax = calculate_tax(subtotal)
            discount = calculate_percentage(subtotal, rate)
            
            # The tax and percentage discount calculations must be identical
            self.assertEqual(tax, discount, f"Mismatched tax ({tax}) and discount ({discount}) on subtotal {subtotal}")
            
            total_before_discount = round_money(subtotal + tax)
            cash_total = round_money(total_before_discount - discount)
            
            # With equal tax and cash discount, the cash total must equal the subtotal
            self.assertEqual(cash_total, subtotal, f"Discrepancy found! Subtotal: {subtotal}, Cash Total: {cash_total}")

    @patch('app.routes.pos.core.get_tax_rate', return_value=0.0725)
    def test_all_subtotals_up_to_100(self, mock_get_tax_rate):
        """Exhaustively verify all subtotals from $0.01 to $100.00 have zero penny discrepancy."""
        rate = 7.25
        for i in range(1, 10001):
            subtotal = i / 100.0
            tax = calculate_tax(subtotal)
            discount = calculate_percentage(subtotal, rate)
            
            self.assertEqual(tax, discount, f"Mismatch at {subtotal}")
            
            total_before_discount = round_money(subtotal + tax)
            cash_total = round_money(total_before_discount - discount)
            
            self.assertEqual(cash_total, subtotal, f"Mismatch at {subtotal}")

if __name__ == '__main__':
    unittest.main()
