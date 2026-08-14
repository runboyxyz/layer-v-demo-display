import unittest
from unittest.mock import patch

from app.configuration import Settings
from app.server import (
    SECURITY_HEADERS,
    drop_runtime_identity,
    status_html,
    status_payload,
    trusted_ingress,
)


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
        self.assertEqual(payload["renderer"], "probe_only")
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

    @patch("app.server.os.setuid")
    @patch("app.server.os.setgid")
    @patch("app.server.os.setgroups")
    @patch("app.server.os.getegid", return_value=2200)
    @patch("app.server.os.geteuid", side_effect=(0, 2200))
    def test_root_bootstrap_drops_all_groups_and_identity(
        self, getuid, getgid, setgroups, setgid, setuid
    ):
        drop_runtime_identity(2200, 2200)
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(2200)
        setuid.assert_called_once_with(2200)


if __name__ == "__main__":
    unittest.main()
