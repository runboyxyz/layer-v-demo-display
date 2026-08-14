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

    @patch("app.verification.smtplib.SMTP")
    @patch("app.verification.secrets.randbelow", return_value=123456)
    def test_first_code_is_sent_to_configured_recipient_without_viewer_email(
        self, _random, smtp
    ):
        self.smtp_file.write_text('{"host":"smtp.example","port":25,"from":"demo@example.com"}')
        gate = verification.VerificationGate(clock=lambda: 1000)
        gate.begin("viewer@example.com")
        gate.ensure_code()
        client = smtp.return_value.__enter__.return_value
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "viewer@example.com")
        gate.ensure_code()
        self.assertEqual(client.send_message.call_count, 1)

    @patch("app.verification.smtplib.SMTP")
    def test_invitation_contains_activation_and_display_buttons(self, smtp):
        self.smtp_file.write_text('{"host":"smtp.example","port":25,"from":"demo@example.com"}')
        gate = verification.VerificationGate(clock=lambda: 1000)
        gate.begin("viewer@example.com")
        gate.send_invitation(
            "https://activate.example/q_demo",
            "https://demo.qurl.site/display/synthetic-token",
        )
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message["To"], "viewer@example.com")
        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("https://activate.example/q_demo", plain)
        self.assertIn("https://demo.qurl.site/display/synthetic-token", plain)
        self.assertIn("Activate LayerV Access", html)
        self.assertIn("Open Demo Display", html)

    def test_invitation_rejects_non_https_links(self):
        self.smtp_file.write_text("{}")
        gate = verification.VerificationGate(clock=lambda: 1000)
        gate.begin("viewer@example.com")
        with self.assertRaises(verification.VerificationError):
            gate.send_invitation(
                "javascript:alert(1)",
                "https://demo.qurl.site/display/synthetic-token",
            )


if __name__ == "__main__":
    unittest.main()
