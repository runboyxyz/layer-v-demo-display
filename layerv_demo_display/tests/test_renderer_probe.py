import unittest

from app.renderer_probe import allowed_request, external_auth_script, run_probe
from app.configuration import Settings


class RendererProbeTests(unittest.TestCase):
    def test_navigation_is_restricted_to_home_assistant_origin(self):
        origin = "http://homeassistant:8123"
        self.assertTrue(allowed_request(origin + "/demo-home/home", origin))
        self.assertTrue(allowed_request("blob:http://homeassistant:8123/id", origin))
        for url in ("https://evil.example/", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertFalse(allowed_request(url, origin))

    def test_bridge_contains_no_credential(self):
        script = external_auth_script()
        self.assertIn("__demoDisplayToken", script)
        self.assertNotIn("SUPERVISOR_TOKEN", script)

    def test_missing_token_does_not_launch_browser(self):
        result = run_probe(Settings(), "")
        self.assertEqual(result.outcome, "failed")
        self.assertIsNone(result.frame)


if __name__ == "__main__":
    unittest.main()
