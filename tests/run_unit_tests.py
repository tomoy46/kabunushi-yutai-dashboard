"""Run the deterministic Python preflight suite with outbound traffic disabled."""

import os
import socket
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
UNIT_TEST_FILES = (
    "test_add_companies.py",
    "test_openai_discovery.py",
    "test_merge_discovery_results.py",
    "test_scripts.py",
)


def load_unit_suite():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for pattern in UNIT_TEST_FILES:
        suite.addTests(loader.discover(str(ROOT / "tests"), pattern=pattern))
    return suite


def main():
    outbound_attempts = []
    external_command_attempts = []

    def reject_outbound(*args, **kwargs):
        outbound_attempts.append((args, kwargs))
        raise AssertionError("unit tests must not open network connections")

    def reject_external_command(*args, **kwargs):
        external_command_attempts.append((args, kwargs))
        raise AssertionError("unit tests must not execute external commands")

    class NetworkIsolationTest(unittest.TestCase):
        def test_fixture_suite_made_zero_external_connections_or_commands(self):
            self.assertEqual(outbound_attempts, [])
            self.assertEqual(external_command_attempts, [])

    suite = load_unit_suite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(NetworkIsolationTest))
    clean_environment = {key: value for key, value in os.environ.items() if key not in {
        "OPENAI_API_KEY", "RUN_LIVE_OFFICIAL_SOURCES",
    }}
    with patch.dict(os.environ, clean_environment, clear=True), \
            patch.object(socket, "create_connection", side_effect=reject_outbound), \
            patch.object(socket.socket, "connect", side_effect=reject_outbound), \
            patch.object(socket.socket, "connect_ex", side_effect=reject_outbound), \
            patch.object(subprocess, "Popen", side_effect=reject_external_command):
        result = unittest.TextTestRunner(verbosity=1, buffer=True).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
