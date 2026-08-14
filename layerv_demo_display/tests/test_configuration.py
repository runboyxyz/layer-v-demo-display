import unittest
from pathlib import Path

from app.configuration import ConfigurationError, parse_settings, validate_dashboard_path


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        settings = parse_settings({})
        self.assertEqual(settings.viewport, (1920, 1080))
        self.assertEqual(settings.capture_interval, 2)

    def test_accepts_demo_dashboard(self):
        self.assertEqual(validate_dashboard_path("/demo-home/home"), "/demo-home/home")

    def test_rejects_external_and_ambiguous_targets(self):
        invalid = (
            "https://evil.example/dashboard", "//evil.example/dashboard",
            "javascript:alert(1)", "file:///etc/passwd", "/demo/../admin",
            "/demo\\admin", "/demo?target=evil", "/demo#fragment",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                validate_dashboard_path(value)

    def test_rejects_unsupported_values(self):
        for options in (
            {"resolution": "800x600"}, {"capture_interval": 0},
            {"default_session_duration": 61}, {"hide_ha_header": "yes"},
        ):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                parse_settings(options)

    def test_connector_is_made_executable_in_final_image(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
        self.assertIn("chmod 0755 /run.sh /usr/local/bin/qurl-connector", dockerfile)
        self.assertIn("test -x /usr/local/bin/qurl-connector", dockerfile)


if __name__ == "__main__":
    unittest.main()
