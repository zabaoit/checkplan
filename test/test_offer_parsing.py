import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from payment_link import parse_trial_and_amount, format_currency_amount

def test_parse_trial_and_amount():
    # Full price 475k / 522.5k with promo object echoed back
    fake_full_price_checkout = {
        "amount_total": 475000,
        "currency": "vnd",
        "promo_campaign": {"promo_campaign_id": "plus-1-month-free"}
    }
    amount, curr, is_trial = parse_trial_and_amount(fake_full_price_checkout)
    assert is_trial is False, f"Expected is_trial=False for amount={amount}, got {is_trial}"
    assert amount == 475000

    # True 0d trial
    fake_trial = {
        "total_summary": {"due": 0, "currency": "vnd"}
    }
    amount, curr, is_trial = parse_trial_and_amount(fake_trial)
    assert is_trial is True, f"Expected is_trial=True, got {is_trial}"
    assert amount == 0

    # 50% discount 261.250d
    fake_discount = {
        "total_summary": {"due": 261250, "subtotal": 522500, "discount": 261250, "currency": "vnd"}
    }
    amount, curr, is_trial = parse_trial_and_amount(fake_discount)
    assert is_trial is False
    assert amount == 261250

    print("[PASS] parse_trial_and_amount logic verified correctly.")

if __name__ == "__main__":
    test_parse_trial_and_amount()
