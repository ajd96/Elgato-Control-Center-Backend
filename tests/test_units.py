"""Unit tests for mired/Kelvin conversions and value parsing."""

import unittest

from elgatoctl import units


class KelvinMiredTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(units.kelvin_to_mired(7000), 143)
        self.assertEqual(units.kelvin_to_mired(2900), 344)

    def test_round_trip(self):
        # note: clamping makes the 2900 K floor round-trip to ~2907 K (1e6/344),
        # and integer mireds make the Kelvin inversion accurate only to the
        # bin width (~ +/- 25 K at bin edges). Mired round trips are exact;
        # the Kelvin side is display-only, hence the loose tolerance.
        for kelvin in range(2900, 7001, 100):
            mired = units.kelvin_to_mired(kelvin)
            back = units.mired_to_kelvin(mired)
            self.assertLessEqual(abs(back - kelvin), 25, msg=str(kelvin))

    def test_clamping(self):
        # below the band warms to 2900K, above cools to 7000K
        self.assertEqual(units.kelvin_to_mired(1500), 344)
        self.assertEqual(units.kelvin_to_mired(30000), 143)
        self.assertEqual(units.mired_to_kelvin(50), units.mired_to_kelvin(units.MIRED_MIN))


class ParseTemperatureTests(unittest.TestCase):
    def test_kelvin_plain(self):
        self.assertEqual(units.parse_temperature("5000"), 200)

    def test_kelvin_suffix(self):
        self.assertEqual(units.parse_temperature("4900k"), 204)
        self.assertEqual(units.parse_temperature("7000K"), 143)

    def test_mired_suffix(self):
        self.assertEqual(units.parse_temperature("230m"), 230)
        self.assertEqual(units.parse_temperature("250mired"), 250)

    def test_rejects_garbage(self):
        for value in ("", "nothing", "----"):
            with self.assertRaises(ValueError):
                units.parse_temperature(value)

    def test_fractional_mireds_rejected(self):
        with self.assertRaises(ValueError):
            units.parse_temperature("1.5m")


class ScalarTests(unittest.TestCase):
    def test_scalar_kinds(self):
        self.assertEqual(units.parse_scalar("2"), 2)
        self.assertEqual(units.parse_scalar("1.5"), 1.5)
        self.assertEqual(units.parse_scalar("true"), 1)   # booleans map to 1/0 int flags
        self.assertEqual(units.parse_scalar("false"), 0)
        self.assertEqual(units.parse_scalar("desktop"), "desktop")


if __name__ == "__main__":
    unittest.main()
