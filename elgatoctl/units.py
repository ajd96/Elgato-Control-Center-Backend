"""Unit conversions and clamping helpers for Elgato lighting values.

Elgato devices express color temperature in *mireds*
(micro-reciprocal degrees): ``mireds = 1_000_000 / Kelvin``.

The device accepts 143 mireds (~7000 K, cool) through 344 mireds
(~2900 K, warm). Larger mired values are warmer. See
Elgato-Control-Center-System-Design.md, section 7.2.
"""

from typing import Union

MIRED_MIN = 143
MIRED_MAX = 344

KELVIN_WARMEST = 2900
KELVIN_COLDEST = 7000


def clamp(value: float, lo: float, hi: float) -> float:
    """Return `value` limited to the inclusive range [lo, hi]."""
    return max(float(lo), min(float(hi), float(value)))


def kelvin_to_mired(kelvin: float) -> int:
    """Convert Kelvin to raw device temperature, clamping to the valid band.

    Example: kelvin_to_mired(7000) -> 143, kelvin_to_mired(2900) -> 344.
    """
    kelvin = clamp(kelvin, KELVIN_WARMEST, KELVIN_COLDEST)
    mired = int(round(1_000_000 / kelvin))
    return int(clamp(mired, MIRED_MIN, MIRED_MAX))


def mired_to_kelvin(mired: int) -> int:
    """Convert a raw device temperature back to Kelvin (display only)."""
    return int(round(1_000_000 / clamp(mired, MIRED_MIN, MIRED_MAX)))


def parse_temperature(text: str) -> int:
    """Parse a user-supplied temperature into device mireds.

    Accepted forms:

    - ``"5000"`` or ``"5000k"``  -> treated as Kelvin
    - ``"230m"`` or ``"230mired"`` -> treated as raw mireds
    - Empty/invalid input raises ValueError.
    """
    token = text.strip().lower()
    if not token:
        raise ValueError("empty temperature value")

    if token.endswith("mired"):
        raw, unit = token[: -len("mired")], "mired"
    elif token.endswith("m"):
        raw, unit = token[:-1], "mired"
    elif token.endswith("k"):
        raw, unit = token[:-1], "kelvin"
    else:
        raw, unit = token, "kelvin"

    try:
        number = float(raw)
    except ValueError:
        raise ValueError(
            "invalid temperature %r; use Kelvin (e.g. '4000k') "
            "or mireds with an 'm' suffix (e.g. '250m')" % text
        )

    if unit == "kelvin":
        return kelvin_to_mired(number)

    if not float(number).is_integer():
        raise ValueError("mired values must be integers, got %r" % text)
    return int(clamp(number, MIRED_MIN, MIRED_MAX))


def parse_scalar(text: str) -> Union[int, float, bool, str]:
    """Convert a settings string such as 'powerOnBehavior=2' RHS to a scalar."""
    lowered = text.strip().lower()
    if lowered == "true":
        return 1
    if lowered == "false":
        return 0
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
