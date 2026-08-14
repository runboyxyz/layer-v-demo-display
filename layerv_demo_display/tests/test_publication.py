import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import publication


class PublicationTests(unittest.TestCase):
    def test_connector_diagnostics_redact_urls_and_long_identifiers(self):
        message = publication.safe_connector_message(
            "failed https://private.example/path token abcdefghijklmnopqrstuvwxyz012345"
        )
        self.assertEqual(message, "failed [url] token [identifier]")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = patch.multiple(
            publication,
            SECRET_FILE=root / "secrets" / "key",
            INSTALLATION_FILE=root / "state" / "installation-id",
            CONNECTOR_CONFIG=root / "connector" / "qurl-proxy.yaml",
            CONNECTOR_STATE=root / "state",
            CONNECTOR_LOGS=root / "logs",
        )
        self.paths.start()
        for path in (
            publication.SECRET_FILE.parent,
            publication.CONNECTOR_CONFIG.parent,
            publication.CONNECTOR_STATE,
            publication.CONNECTOR_LOGS,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.paths.stop()
        self.directory.cleanup()

    @patch("app.publication.subprocess.run")
    def test_registration_passes_key_by_file_not_argument(self, run):
        def register(*args, **kwargs):
            publication.CONNECTOR_CONFIG.write_text("resource_id: r_demo\n")
            return Mock(returncode=0, stderr="")

        run.side_effect = register
        publisher = publication.LayerVPublisher()
        with patch.object(publisher, "start_connector"):
            publisher.connect("synthetic-layer-v-key")
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("synthetic-layer-v-key", command)
        self.assertEqual(environment["QURL_API_KEY_FILE"], str(publication.SECRET_FILE))
        self.assertEqual(publication.SECRET_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            publication.INSTALLATION_FILE.parent,
            publication.CONNECTOR_STATE,
        )

    @patch("app.publication.subprocess.run")
    def test_timeout_recovers_route_written_before_late_bootstrap_stall(self, run):
        def partial_registration(*args, **kwargs):
            publication.CONNECTOR_CONFIG.write_text("resource_id: r_recovered\n")
            raise publication.subprocess.TimeoutExpired(args[0], 60)

        run.side_effect = partial_registration
        publisher = publication.LayerVPublisher()
        with patch.object(publisher, "start_connector") as start:
            publisher.connect("synthetic-layer-v-key")
        self.assertTrue(publisher.configured)
        start.assert_called_once()

    def test_publish_returns_only_qurl_site_plus_display_path(self):
        publication.SECRET_FILE.write_text("synthetic-key")
        publication.CONNECTOR_CONFIG.write_text("resource_id: r_demo\n")
        publisher = publication.LayerVPublisher()
        response = {"data": {
            "qurl_site": "https://demo.qurl.site",
            "qurl_link": "https://activate.example/q_one",
            "qurl_id": "q_one",
        }}
        with patch.object(publisher, "start_connector"), patch.object(
            publisher, "_request", return_value=response
        ):
            result = publisher.publish("display-secret", 30)
        self.assertEqual(result, "https://demo.qurl.site/display/display-secret")
        self.assertEqual(publisher.activation_url, "https://activate.example/q_one")
        self.assertNotIn("homeassistant", result)

    def test_publisher_secures_storage_after_supervisor_bootstrap(self):
        secret_directory = publication.SECRET_FILE.parent
        secret_directory.chmod(0o755)
        publication.secure_storage_modes()
        self.assertEqual(secret_directory.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
