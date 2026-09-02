import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from momo_pay import _flatten_form, get_momo_checkout_details
from web.change_password_manager import ChangePasswordJob, ChangePasswordJobManager

def test_momo_flatten_form():
    sample = {
        "payment_method_data": {
            "type": "momo",
            "billing_details": {
                "name": "Test User",
                "email": "test@example.com",
            }
        },
        "return_url": "https://chatgpt.com/",
        "is_test": True,
    }
    flattened = _flatten_form(sample)
    assert flattened["payment_method_data[type]"] == "momo"
    assert flattened["payment_method_data[billing_details][name]"] == "Test User"
    assert flattened["is_test"] == "true"
    print("[PASS] _flatten_form verified.")

def test_job_momo_fields():
    mgr = ChangePasswordJobManager()
    job = ChangePasswordJob(
        id="job_test_123",
        email="test@gmail.com",
        current_password="pass",
    )
    job.session_data = {
        "offer_details": {
            "session_id": "cs_test_123",
            "has_momo": True,
            "checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_test_123",
            "payment_methods": ["stripe", "momo"],
        }
    }
    d = job.to_dict()
    assert d["has_momo"] is True
    assert d["checkout_url"] == "https://chatgpt.com/checkout/openai_llc/cs_test_123"
    print("[PASS] ChangePasswordJob MoMo fields verified.")

if __name__ == "__main__":
    test_momo_flatten_form()
    test_job_momo_fields()
