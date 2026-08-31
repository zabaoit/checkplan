import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from payment_link import format_currency_amount

def test_currency_formatting():
    assert format_currency_amount(500000, "VND") == "500.000 ₫", f"Got: {format_currency_amount(500000, 'VND')}"
    assert format_currency_amount(0, "VND") == "0 ₫", f"Got: {format_currency_amount(0, 'VND')}"
    assert format_currency_amount(2000, "USD") == "$20", f"Got: {format_currency_amount(2000, 'USD')}"
    assert format_currency_amount(20, "USD") == "$20", f"Got: {format_currency_amount(20, 'USD')}"
    assert format_currency_amount(330000, "IDR") == "330.000 IDR", f"Got: {format_currency_amount(330000, 'IDR')}"
    print("[PASS] All currency formatting tests passed.")

if __name__ == "__main__":
    test_currency_formatting()
