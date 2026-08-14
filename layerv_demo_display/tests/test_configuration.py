import json
import unittest
from unittest.mock import patch
from pathlib import Path

from app.configuration import ConfigurationError, load_settings, parse_settings, validate_dashboard_path


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        settings = parse_settings({})
        self.assertEqual(settings.viewport, (1920, 1080))
        self.assertEqual(settings.capture_interval, 2)

    @patch.dict("os.environ", {"APP_SETTINGS_JSON": json.dumps({"capture_interval": 4})})
    def test_validated_runtime_settings_avoid_supervisor_owned_file(self):
        self.assertEqual(load_settings().capture_interval, 4)

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

    def test_connector_apparmor_rule_allows_its_executable_mapping(self):
        profile = (Path(__file__).parents[1] / "apparmor.txt").read_text()
        self.assertIn("/usr/local/bin/qurl-connector rix,", profile)
        self.assertNotIn("profile connector {", profile)

    def test_container_defines_separate_server_and_connector_users(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
        self.assertIn("--uid 2200 --gid 2202", dockerfile)
        self.assertIn("--uid 2201 --gid 2202", dockerfile)
        self.assertIn("/data/connector-secrets", dockerfile)

    def test_installation_id_is_not_written_at_data_root(self):
        source = (Path(__file__).parents[1] / "app" / "publication.py").read_text()
        self.assertIn('INSTALLATION_FILE = CONNECTOR_STATE / "installation-id"', source)
        self.assertNotIn('DATA_DIR / "layerv-installation-id"', source)


if __name__ == "__main__":
    unittest.main()
