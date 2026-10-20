"""End-to-end CLI tests running `elgatoctl.main()` against a fake light.

Session discovery is stubbed so tests are fully hermetic (no real mDNS
traffic, no dependence on whatever lights happen to sit on the sandbox
LAN). The fake device coverage mirrors the protocol quirks documented in
Elgato-Control-Center-System-Design.md section 8.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import elgatoctl.cli as cli  # noqa: E402
import elgatoctl.discovery as discovery  # noqa: E402

from tests.fake_light import FakeLightServer  # noqa: E402


class _StubSession:
    """Replaces cli.Session: returns fixed, pre-built 'discovered' items."""

    def __init__(self, items):
        self._items = items

    def devices(self):
        return list(self._items)


def _discovered_like(ip, port, name, mac="3C:6A:9D:16:12:45", model="Elgato Key Light Air 20LAB9901"):
    return discovery.DiscoveredDevice(
        name=name,
        host="fake-%s.local" % name.replace(" ", "-").lower(),
        ip=ip,
        port=port,
        mac=mac,
        model=model,
        device_type=53,
        manufacturer="Elgato",
        protocol_version="1.0",
        responder_ip="127.0.0.1",
    )


class CliTestCase(unittest.TestCase):
    def setUp(self):
        # isolate config side effects per test
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cli.CONFIG_DIR = tmp.name
        cli.NAMED_FILE = os.path.join(tmp.name, "named-lights.json")

        self.server = FakeLightServer().start()
        self.addCleanup(self.server.stop)
        self.endpoint = self.server.endpoint

        self._patch_session([self.server])

    def _patch_session(self, servers):
        ip, port = self.endpoint.split(":")
        items = [
            _discovered_like(ip, int(port), server.accessory["displayName"])
            for server in servers
        ]
        original = cli.Session
        self.addCleanup(setattr, cli, "Session", original)
        cli.Session = lambda timeout=None, interface=None: _StubSession(items)

    def run_cli(self, argv, expect_code=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, expect_code, msg=err.getvalue())
        return out.getvalue(), err.getvalue()

    # ------------------------------------------------------------------ #

    def test_status_by_literal_ip(self):
        text, _ = self.run_cli(["--json", "status", self.endpoint])
        doc = json.loads(text)
        self.assertIn("lights", doc)
        self.assertEqual(doc["model"], "Elgato Key Light")

    def test_status_by_discovered_name(self):
        text, _ = self.run_cli(["status", "Test Key Light"])
        self.assertIn("Test Key Light", text)
        self.assertIn("power off", text)
        self.assertIn("brightness 25%", text)

    def test_status_no_partial_result_mode(self):
        text, _ = self.run_cli(["status"])  # no patterns -> all known devices
        self.assertIn("Test Key Light", text)

    def test_on_off_flow(self):
        self.run_cli(["set", self.endpoint, "--on", "on", "--brightness", "80"])
        self.assertEqual(self.server.lights[0]["on"], 1)
        self.assertEqual(self.server.lights[0]["brightness"], 80)

        self.run_cli(["off", self.endpoint])
        self.assertEqual(self.server.lights[0]["on"], 0)

        text, _ = self.run_cli(["status", self.endpoint])
        self.assertIn("power off", text)
        self.assertIn("brightness 80%", text)

    def test_on_command_powers_on(self):
        self.run_cli(["on", self.endpoint])
        self.assertEqual(self.server.lights[0]["on"], 1)

    def test_temperature_kelvin_conversion(self):
        self.run_cli(["set", self.endpoint, "--on", "on", "--temperature", "4900k"])
        self.assertEqual(self.server.lights[0]["temperature"], 204)

        self.run_cli(["set", self.endpoint, "--temperature", "210m"])
        self.assertEqual(self.server.lights[0]["temperature"], 210)

    def test_set_settings_round_trip(self):
        text, _ = self.run_cli(
            ["set-settings", "powerOnBehavior=2", self.endpoint]
        )
        self.assertEqual(self.server.settings["powerOnBehavior"], 2)
        self.assertIn("powerOnBehavior = 2", text)

    def test_rename_and_identify(self):
        self.run_cli(["name", "Renamed Light", self.endpoint])
        self.assertEqual(self.server.accessory["displayName"], "Renamed Light")
        self.run_cli(["identify", self.endpoint])
        self.assertEqual(self.server.identify_count, 1)

    def test_firmware_command(self):
        text, _ = self.run_cli(["firmware", self.endpoint])
        self.assertIn("1.0.4", text)
        self.assertIn("238", text)

    def test_brightness_validation_is_client_side(self):
        # Fail fast on the host: never put invalid traffic on the wire.
        text, err = self.run_cli(
            ["set", self.endpoint, "--brightness", "140"],
            expect_code=cli.EXIT_USAGE,
        )
        self.assertIn("--brightness must be 0-100", err)
        self.assertEqual(self.server.lights[0]["brightness"], 25)
        self.assertFalse(self.server.requests)

    def test_no_devices_found_exit_code(self):
        self._patch_session([])  # nothing discoverable and nothing paired
        out, err = self.run_cli(
            ["--timeout", "0.2", "status"], expect_code=cli.EXIT_NO_DEVICES
        )
        self.assertIn("no Elgato lights found", err)

    def test_alias_set_list_remove(self):
        text, _ = self.run_cli(["alias", "set", "desk", self.endpoint])
        self.assertIn("saved alias 'desk'", text)

        text, _ = self.run_cli(["alias", "list"])
        self.assertIn("desk", text)

        # alias-based control
        self.run_cli(["set", "desk", "--on", "on"])
        self.assertEqual(self.server.lights[0]["on"], 1)

        self.run_cli(["alias", "remove", "desk"])
        text, _ = self.run_cli(["alias", "list"])
        self.assertIn("no aliases saved", text)

    def test_alias_from_offline_ip_endpoint(self):
        """alias set IP[:PORT] probes the device directly, even when the
        session stub reports nothing discoverable (manual-IP parity)."""
        text, _ = self.run_cli(["alias", "set", "desk", self.endpoint])
        self.assertIn("saved alias 'desk'", text)
        self.assertIn("Test Key Light", text)  # name read from accessory-info
        self.run_cli(["set", "desk", "--on", "on"])
        self.assertEqual(self.server.lights[0]["on"], 1)

    def test_alias_remove_unknown(self):
        out, err = self.run_cli(
            ["alias", "remove", "ghost"], expect_code=cli.EXIT_DEVICE_ERROR
        )
        self.assertIn("not found", err)

    def test_bare_alias_lists(self):
        text, _ = self.run_cli(["alias"])
        self.assertIn("no aliases saved", text)


class ParserSmokeTests(unittest.TestCase):
    def test_help_mentions_all_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                cli.build_parser().parse_args(["--help"])
            except SystemExit:
                pass
        text = out.getvalue()
        for command in (
            "discover", "status", "info", "on", "off", "set",
            "group", "settings", "set-settings", "name", "identify",
            "firmware", "reboot", "factory-reset", "alias",
        ):
            self.assertIn(command, text)

    def test_on_and_off_are_separate_subcommands(self):
        parser = cli.build_parser()
        args = parser.parse_args(["on", "desk"])
        self.assertEqual(args.forced_state, "on")
        self.assertEqual(args.targets, ["desk"])

        args = parser.parse_args(["off", "desk", "lamp2"])
        self.assertEqual(args.forced_state, "off")

    def test_set_parses_options(self):
        parser = cli.build_parser()
        args = parser.parse_args(["set", "--on", "on", "--brightness", "42", "--temperature", "5000k"])
        self.assertEqual(args.on, "on")
        self.assertEqual(args.brightness, 42)
        self.assertEqual(args.temperature, "5000k")
        self.assertEqual(args.targets, [])

    def test_rejects_empty_assignment(self):
        out_err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(out_err):
            code = cli.main(["set-settings", "nonsense", "desk"])
        self.assertEqual(code, cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
