"""Client-level tests against the protocol-faithful fake light.

Covers: partial PUT merge, write verification (incl. the silent-rejection
quirk -> VerifyError), the no-Origin rule enforced by fake devices,
identify/restart actions, display-name rename, and settings patching.
"""

import sys
import unittest
from pathlib import Path  # noqa: F401  (ensures package importable from checkout)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from elgatoctl.device import (  # noqa: E402
    CommandError,
    DeviceUnreachable,
    ElgatoDevice,
    VerifyError,
)

from tests.fake_light import FakeLightServer  # noqa: E402


class ConnectedTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeLightServer().start()
        self.client = ElgatoDevice(
            host=self.server.endpoint.split(":")[0],
            port=self.server.port,
            name="fake-light",
        )
        self.addCleanup(self.server.stop)

    def tearDown(self):
        self.server = None


class IdentityTests(ConnectedTestCase):
    def test_get_info(self):
        info = self.client.get_info()
        self.assertEqual(info["productName"], "Elgato Key Light")
        self.assertEqual(info["firmwareVersion"], "1.0.4")
        self.assertEqual(info["features"], ["lights"])

    def test_rename_display_name(self):
        self.client.set_display_name("Desk Light")
        self.assertEqual(self.client.get_info()["displayName"], "Desk Light")
        self.assertEqual(self.server.accessory["displayName"], "Desk Light")


class StateTests(ConnectedTestCase):
    def test_turn_on_partial_merge(self):
        state = self.client.set_state(on=True)
        light = state["lights"][0]
        self.assertEqual(light["on"], 1)
        self.assertEqual(light["brightness"], 25)   # kept
        self.assertEqual(light["temperature"], 230)  # kept

    def test_brightness_and_temperature(self):
        self.client.set_state(on=True, brightness=60)
        state = self.client.set_state(temperature=143)
        light = state["lights"][0]
        self.assertEqual(light["brightness"], 60)
        self.assertEqual(light["temperature"], 143)

    def test_out_of_range_brightness_is_rejected_client_side(self):
        # Fail fast on the host, never putting invalid traffic on the wire.
        with self.assertRaises(ValueError):
            self.client.set_state(brightness=140)
        self.assertEqual(self.server.lights[0]["brightness"], 25)
        self.assertFalse(self.server.requests)

    def test_silent_rejection_of_write_raises_verify_error(self):
        # A misbehaving/defective device ACKs with 200 but drops the value:
        # the client must detect the discrepancy via write verification.
        self.server.defective = True
        with self.assertRaises(VerifyError):
            self.client.set_state(brightness=60)
        self.assertEqual(self.server.lights[0]["brightness"], 25)  # unchanged

    def test_device_serves_403_with_origin_header(self):
        import urllib.request
        import urllib.error
        request = urllib.request.Request(
            "http://127.0.0.1:%d/elgato/lights" % self.server.port,
            data=b"{}",
            headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            method="PUT",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()  # avoid ResourceWarning from unclosed body

    def test_client_never_sends_origin(self):
        self.client.set_state(on=True)
        def _last_request():
            return self.server.last_request()
        origin = _last_request()["origin"]
        self.assertIsNone(origin)

    def test_unreachable_device(self):
        dead = ElgatoDevice(host="127.0.0.1", port=1, name="ghost")
        with self.assertRaises(DeviceUnreachable):
            dead.get_state()


class SettingsTests(ConnectedTestCase):
    def test_get_and_patch(self):
        settings = self.client.get_settings()
        self.assertEqual(settings["powerOnBehavior"], 1)
        updated = self.client.patch_settings({"powerOnBehavior": 2})
        self.assertEqual(updated["powerOnBehavior"], 2)
        self.assertEqual(self.server.settings["powerOnBehavior"], 2)


class ActionTests(ConnectedTestCase):
    def test_identify(self):
        self.client.identify()
        self.assertEqual(self.server.identify_count, 1)

    def test_restart(self):
        self.client.restart()
        self.assertEqual(self.server.restart_count, 1)


class ValidationTests(ConnectedTestCase):
    def test_brightness_validation_client_side(self):
        with self.assertRaises(ValueError):
            self.client.set_state(brightness=101)
        # nothing was transmitted
        self.assertFalse(self.server.requests)


if __name__ == "__main__":
    unittest.main()
