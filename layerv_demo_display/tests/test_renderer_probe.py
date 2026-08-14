import unittest

from app.renderer_probe import (
    allowed_request,
    external_auth_script,
    navigation_error_code,
    run_probe,
)
from app.configuration import Settings


class RendererProbeTests(unittest.TestCase):
    def test_navigation_is_restricted_to_home_assistant_origin(self):
        origin = "http://homeassistant"
        self.assertTrue(allowed_request(origin + "/demo-home/home", origin))
        self.assertTrue(allowed_request("ws://homeassistant/api/websocket", origin))
        self.assertTrue(allowed_request("blob:http://homeassistant/id", origin))
        for url in (
            "https://evil.example/",
            "wss://evil.example/api/websocket",
            "http://user@homeassistant/",
            "file:///etc/passwd",
            "javascript:alert(1)",
        ):
            with self.subTest(url=url):
                self.assertFalse(allowed_request(url, origin))

    def test_bridge_contains_no_credential(self):
        script = external_auth_script()
        self.assertIn("__demoDisplayToken", script)
        self.assertNotIn("SUPERVISOR_TOKEN", script)
        self.assertNotIn("externalAppV2", script)
        self.assertNotIn("externalBus", script)

    def test_missing_token_does_not_launch_browser(self):
        result = run_probe(Settings(), "")
        self.assertEqual(result.outcome, "failed")
        self.assertIsNone(result.frame)

    def test_navigation_diagnostic_exposes_only_error_code(self):
        error = Exception("Page.goto: net::ERR_CONNECTION_REFUSED at http://secret/")
        self.assertEqual(navigation_error_code(error), "ERR_CONNECTION_REFUSED")
        self.assertEqual(navigation_error_code(Exception("other detail")), "ERR_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
