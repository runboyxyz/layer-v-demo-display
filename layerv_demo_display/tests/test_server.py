import unittest

from app.configuration import Settings
from app.server import SECURITY_HEADERS, status_html, status_payload, trusted_ingress


class ServerTests(unittest.TestCase):
    def test_only_expected_ingress_proxy_is_trusted(self):
        path = "/api/hassio_ingress/example/"
        self.assertTrue(trusted_ingress("172.30.32.2", path))
        self.assertFalse(trusted_ingress("127.0.0.1", path))
        self.assertFalse(trusted_ingress("172.30.32.2", "/display/token"))
        self.assertFalse(trusted_ingress("172.30.32.2", path + "\nspoof"))

    def test_status_is_inactive_and_has_no_renderer(self):
        payload = status_payload(Settings())
        self.assertEqual(payload["session"], "inactive")
        self.assertEqual(payload["renderer"], "not_installed")
        self.assertFalse(payload["chromium_running"])

    def test_status_page_contains_no_session_action(self):
        page = status_html(Settings(), "test").decode()
        self.assertIn("Demo Session: Not running", page)
        self.assertNotIn("Start Demo Session", page)
        self.assertNotIn("Display URL", page)

    def test_security_headers_disable_storage_and_external_content(self):
        self.assertEqual(SECURITY_HEADERS["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", SECURITY_HEADERS["Content-Security-Policy"])
        self.assertEqual(SECURITY_HEADERS["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
