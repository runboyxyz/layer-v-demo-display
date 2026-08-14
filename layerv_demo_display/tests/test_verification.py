from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import verification


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.smtp_file = Path(self.directory.name) / "smtp.json"
        self.file_patch = patch.object(verification, "SMTP_FILE", self.smtp_file)
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.directory.cleanup()

    def test_email_gate_is_optional_and_grant_is_session_bounded(self):
        self.smtp_file.write_text("{}")
        gate = verification.VerificationGate(clock=lambda: 1000)
        self.assertTrue(gate.authorized(""))
        gate.begin("viewer@example.com")
        self.assertFalse(gate.authorized(""))
        gate._pending = verification.PendingCode(
            verification.sha256(b"123456").digest(), 1100
        )
        grant = gate.confirm("123456", 1200)
        self.assertTrue(gate.authorized(grant))
        gate.end()
        self.assertTrue(gate.authorized(""))

    def test_invalid_smtp_settings_are_not_persisted(self):
        with self.assertRaises(verification.VerificationError):
            verification.configure_smtp({"smtp_host": "", "smtp_from": "bad"})
        self.assertFalse(self.smtp_file.exists())


if __name__ == "__main__":
    unittest.main()
