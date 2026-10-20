"""Multi-device fan-out tests for parallel.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from elgatoctl import parallel  # noqa: E402
from elgatoctl.device import ElgatoDevice  # noqa: E402

from tests.fake_light import FakeLightServer  # noqa: E402


class QueryDevicesTests(unittest.TestCase):
    def test_dead_device_is_filtered_out(self):
        alive = FakeLightServer().start()
        self.addCleanup(alive.stop)

        candidates = [
            type("C", (), {"name": "alive", "ip": "127.0.0.1", "port": alive.port,
                           "mac": "", "model": "m"})(),
            type("C", (), {"name": "dead", "ip": "127.0.0.1", "port": 1,
                           "mac": "", "model": "m"})(),
        ]
        result = parallel.query_devices(candidates, timeout=1.0)
        self.assertEqual([c.name for c in result], ["alive"])


class ActOnDevicesTests(unittest.TestCase):
    def make_client(self, port, name):
        return ElgatoDevice(host="127.0.0.1", port=port, name=name)

    def test_failures_collected_not_raised(self):
        first = FakeLightServer().start()
        self.addCleanup(first.stop)
        dead_port = 59980  # almost certainly nothing listens here

        targets = [self.make_client(first.port, "a"), self.make_client(dead_port, "dead")]
        successes, failures = parallel.act_on_devices(
            targets, lambda client: client.get_state(), parallel=True
        )
        self.assertEqual([c.name for c, _ in successes], ["a"])
        self.assertEqual([c.name for c, _ in failures], ["dead"])

    def test_threaded_results_match_input_order(self):
        servers = [FakeLightServer().start() for _ in range(3)]
        for server in servers:
            self.addCleanup(server.stop)
        servers.sort(key=lambda server: server.port)
        targets = [self.make_client(server.port, "s%d" % i) for i, server in enumerate(servers)]
        successes, failures = parallel.act_on_devices(
            targets, lambda client: client.get_info(), parallel=True
        )
        self.assertEqual(failures, [])
        self.assertEqual([c.name for c, _ in successes], ["s0", "s1", "s2"])


if __name__ == "__main__":
    unittest.main()
