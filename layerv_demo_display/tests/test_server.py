import unittest
from unittest.mock import patch

from app.configuration import Settings
from app.server import (
    ADMIN_CSP,
    COMMON_HEADERS,
    VIEWER_CSP,
    display_parts,
    status_html,
    status_payload,
    set_admin_notice,
    take_admin_notice,
    trusted_ingress,
    viewer_html,
    viewer_csp,
    verification_html,
    Invitation,
    INVITATIONS,
    invitation_notice,
)
from app.verification import VerificationGate


class ServerTests(unittest.TestCase):
    def test_no_email_notice_does_not_claim_email_was_sent(self):
        notice = invitation_notice("Demo", "", False)
        self.assertEqual(notice, "Demo invitation created.")
        self.assertNotIn("email", notice.lower())
    def test_only_expected_ingress_proxy_is_trusted(self):
        path = "/api/hassio_ingress/example/"
        self.assertTrue(trusted_ingress("172.30.32.2", path))
        self.assertFalse(trusted_ingress("127.0.0.1", path))
        self.assertFalse(trusted_ingress("172.30.32.2", "/display/token"))
        self.assertFalse(trusted_ingress("172.30.32.2", path + "\nspoof"))

    def test_public_route_accepts_only_display_and_verification_routes(self):
        self.assertEqual(display_parts("/display/token"), ("token", "view"))
        self.assertEqual(display_parts("/display/token/frame"), ("token", "frame"))
        self.assertEqual(display_parts("/display/token/stream"), ("token", "stream"))
        self.assertEqual(display_parts("/display/token/video"), ("token", "video"))
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
        self.assertEqual(payload["video_output"], {"width": 960, "height": 540})

    def test_status_page_has_session_start_but_no_token_when_inactive(self):
        page = status_html(Settings(), "test").decode()
        self.assertIn("Demo Session: Not running", page)
        self.assertIn("Start Demo Session", page)
        self.assertNotIn("/display/", page)

    def test_admin_script_supports_separate_activation_and_display_copy(self):
        self.assertIn("[data-copy]", __import__("app.server", fromlist=["ADMIN_SCRIPT"]).ADMIN_SCRIPT)

    def test_viewer_is_pixels_only_and_refreshes_latest_frame(self):
        page = viewer_html(3).decode()
        self.assertIn("LIVE • READ ONLY", page)
        self.assertIn("/stream", page)
        self.assertIn("/frame?t=", page)
        self.assertIn("video.buffered.end", page)
        self.assertIn("setInterval(refresh,3000)", page)
        self.assertNotIn("homeassistant", page.lower())
        self.assertNotIn("iframe", page.lower())
        self.assertIn("script-src 'sha256-", viewer_csp(3))

    def test_video_viewer_uses_protected_video_then_image_fallback(self):
        page = viewer_html(3, video=True).decode()
        self.assertIn("/video", page)
        self.assertIn("new MediaSource", page)
        self.assertIn("source.appendBuffer", page)
        self.assertIn("queue.splice(1)", page)
        self.assertIn("/stream", page)
        self.assertIn("/frame?t=", page)
        self.assertNotIn("homeassistant", page.lower())
        self.assertNotIn("iframe", page.lower())

    def test_security_headers_separate_admin_and_viewer(self):
        self.assertEqual(COMMON_HEADERS["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", ADMIN_CSP)
        self.assertIn("frame-ancestors 'none'", VIEWER_CSP)
        self.assertIn("default-src 'none'", VIEWER_CSP)
        self.assertIn("connect-src 'self'", viewer_csp(3, True))
        self.assertIn("media-src 'self' blob:", viewer_csp(3, True))

    def test_verification_asks_only_for_code_and_supports_resend(self):
        page = verification_html("synthetic-token").decode()
        self.assertIn("Verification code", page)
        self.assertIn("Send a new code", page)
        self.assertNotIn('type="email"', page)

    def test_active_status_has_open_display_and_revoke_controls(self):
        with patch("app.server.SESSION.snapshot") as snapshot, patch(
            "app.server.PUBLISHER"
        ) as publisher:
            snapshot.return_value.active = True
            snapshot.return_value.state = "running"
            snapshot.return_value.token = "synthetic-token"
            snapshot.return_value.expires_at = 2_000_000_000
            snapshot.return_value.last_frame_at = None
            snapshot.return_value.frame_duration = None
            snapshot.return_value.viewers = 0
            snapshot.return_value.consecutive_failures = 0
            publisher.configured = True
            publisher.connected = True
            gate = VerificationGate()
            gate.begin("")
            INVITATIONS["synthetic-token"] = Invitation(
                "invite-one", "synthetic-token", "viewer@example.com", gate,
                "publication-one", "https://activate.example/q_demo",
                "https://demo.qurl.site/display/synthetic-token",
            )
            try:
                page = status_html(Settings(), "test").decode()
            finally:
                INVITATIONS.clear()
        self.assertIn("Open Demo Display", page)
        self.assertIn("Revoke this viewer", page)
        self.assertIn("End Demo Session &amp; Revoke All", page)

    def test_admin_notice_is_one_time_and_bounded(self):
        set_admin_notice("x" * 600)
        self.assertEqual(take_admin_notice(), "x" * 500)
        self.assertEqual(take_admin_notice(), "")

if __name__ == "__main__":
    unittest.main()
