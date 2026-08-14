import unittest
from unittest.mock import patch

from app.configuration import Settings
from app.server import (
    ADMIN_CSP,
    COMMON_HEADERS,
    VIEWER_CSP,
    display_parts,
    drop_runtime_identity,
    status_html,
    status_payload,
    set_admin_notice,
    take_admin_notice,
    trusted_ingress,
    viewer_html,
    viewer_csp,
)


class ServerTests(unittest.TestCase):
    def test_only_expected_ingress_proxy_is_trusted(self):
        path = "/api/hassio_ingress/example/"
        self.assertTrue(trusted_ingress("172.30.32.2", path))
        self.assertFalse(trusted_ingress("127.0.0.1", path))
        self.assertFalse(trusted_ingress("172.30.32.2", "/display/token"))
        self.assertFalse(trusted_ingress("172.30.32.2", path + "\nspoof"))

    def test_public_route_accepts_only_display_and_verification_routes(self):
        self.assertEqual(display_parts("/display/token"), ("token", "view"))
        self.assertEqual(display_parts("/display/token/frame"), ("token", "frame"))
        self.assertEqual(
            display_parts("/display/token/verify/request"),
            ("token", "verify_request"),
        )
        for path in ("/display", "/display/token/admin", "/api/status", "/display//frame"):
            self.assertIsNone(display_parts(path))

    def test_status_is_inactive_until_session_starts(self):
        payload = status_payload(Settings())
        self.assertFalse(payload["chromium_running"])
        self.assertEqual(payload["renderer"], "stopped")

    def test_status_page_has_session_start_but_no_token_when_inactive(self):
        page = status_html(Settings(), "test").decode()
        self.assertIn("Demo Session: Not running", page)
        self.assertIn("Start Demo Session", page)
        self.assertNotIn("/display/", page)

    def test_viewer_is_pixels_only_and_refreshes_latest_frame(self):
        page = viewer_html(3).decode()
        self.assertIn("LIVE • READ ONLY", page)
        self.assertIn("/frame?t=", page)
        self.assertIn("setInterval(refresh,3000)", page)
        self.assertNotIn("homeassistant", page.lower())
        self.assertNotIn("iframe", page.lower())
        self.assertIn("script-src 'sha256-", viewer_csp(3))

    def test_security_headers_separate_admin_and_viewer(self):
        self.assertEqual(COMMON_HEADERS["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", ADMIN_CSP)
        self.assertIn("frame-ancestors 'none'", VIEWER_CSP)
        self.assertIn("default-src 'none'", VIEWER_CSP)

    def test_admin_notice_is_one_time_and_bounded(self):
        set_admin_notice("x" * 600)
        self.assertEqual(take_admin_notice(), "x" * 500)
        self.assertEqual(take_admin_notice(), "")

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
