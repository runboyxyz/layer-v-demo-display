import os
import unittest
from unittest.mock import patch

from app import supervisor


class SupervisorTests(unittest.TestCase):
    @patch("app.supervisor.os.umask")
    @patch("app.supervisor.os.setuid")
    @patch("app.supervisor.os.setgid")
    @patch("app.supervisor.os.setgroups")
    def test_demote_drops_groups_and_selects_requested_identity(
        self, setgroups, setgid, setuid, umask
    ):
        supervisor._demote(2201)()
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(supervisor.RUNTIME_GID)
        setuid.assert_called_once_with(2201)
        umask.assert_called_once_with(0o077)

    @patch.dict(os.environ, {
        "SUPERVISOR_TOKEN": "synthetic-ha-token",
        "UNRELATED_SECRET": "must-not-pass",
    }, clear=True)
    def test_connector_environment_excludes_supervisor_and_unrelated_secrets(self):
        connector = supervisor._environment("/tmp/connector", False)
        server = supervisor._environment("/tmp/server", True)
        self.assertNotIn("SUPERVISOR_TOKEN", connector)
        self.assertNotIn("UNRELATED_SECRET", connector)
        self.assertEqual(server["SUPERVISOR_TOKEN"], "synthetic-ha-token")


if __name__ == "__main__":
    unittest.main()
