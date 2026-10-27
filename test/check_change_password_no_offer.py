import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from change_password import ChangePasswordError
from web.change_password_manager import ChangePasswordJob, ChangePasswordJobManager


class TestChangePasswordNoOffer(unittest.TestCase):
    def test_error_data_payload(self):
        err = ChangePasswordError("KHÔNG CÓ OFFER (Không có offer trial nào)", data={"plan": "FREE", "has_gpay": True})
        self.assertEqual(err.data["plan"], "FREE")
        self.assertTrue(err.data["has_gpay"])

    def test_job_preserves_session_data_on_no_offer(self):
        job = ChangePasswordJob(
            id="test-1",
            email="test@gmail.com",
            current_password="old",
            new_password="new",
            check_plan_only=True
        )
        self.assertIsNone(job.session_data)
        
        # Simulate manager catching ChangePasswordError with data
        exc = ChangePasswordError("KHÔNG CÓ OFFER (Trial không khả dụng)", data={"checkout_url": "https://pay.stripe.com/test", "offer_details": {"checkout_url": "https://pay.stripe.com/test", "has_gpay": True}})
        if getattr(exc, "data", None):
            job.session_data = exc.data
            job.error = str(exc)
            job.status = "error"
            
        data = job.to_dict()
        self.assertEqual(data["checkout_url"], "https://pay.stripe.com/test")
        self.assertEqual(data["error"], "KHÔNG CÓ OFFER (Trial không khả dụng)")
        self.assertEqual(data["status"], "error")
        full_data = job.to_dict_full()
        self.assertEqual(full_data["session_data"]["checkout_url"], "https://pay.stripe.com/test")


if __name__ == "__main__":
    unittest.main()
