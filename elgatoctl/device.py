"""HTTP control-plane client for Elgato lights (TCP 9123, JSON over HTTP/1.1).

Implements the device API documented in
``Elgato-Control-Center-System-Design.md`` section 8:

    GET/PUT  /elgato/lights            power, brightness, temperature, hue ...
    GET/PUT  /elgato/lights/settings   power-on behaviour, fade durations ...
    GET/PUT  /elgato/accessory-info    identity, firmware, display name
    POST     /elgato/identify, /elgato/restart, /elgato/factory-reset

Protocol rules encoded below:

- Requests carry ``Content-Type: application/json`` and must **never**
  carry an ``Origin`` header (devices answer 403 to block web pages).
- PUT accepts partial documents; unspecified fields keep their current
  value. A PUT returns the new state, which we return to callers.
- Temperature is expressed in mireds (143-344). See units.py.
- Devices may answer 200 while silently ignoring out-of-range writes,
  so :meth:`ElgatoDevice.set_state` re-reads and verifies by default.
- IPv6 answers use literal bracketed hosts (RFC 3986) automatically
  handled by socket.create_connection.
"""

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

USER_AGENT = "elgatoctl-linux/1.0"
DEFAULT_TIMEOUT = 4.0

# Endpoints --------------------------------------------------------------- #
INFO_PATH = "/elgato/accessory-info"
LIGHTS_PATH = "/elgato/lights"
SETTINGS_PATH = "/elgato/lights/settings"
IDENTIFY_PATH = "/elgato/identify"
RESTART_PATH = "/elgato/restart"
FACTORY_RESET_PATH = "/elgato/factory-reset"


class ElgatoError(Exception):
    """Base error for the Elgato control client."""


class DeviceUnreachable(ElgatoError):
    """The device did not answer within the timeout."""


class CommandError(ElgatoError):
    """The device answered with an HTTP error status."""


class VerifyError(ElgatoError):
    """A write did not take effect on the device (silent-rejection case)."""


class NoLightsError(ElgatoError):
    """The device reports numberOfLights == 0."""


class ElgatoDevice:
    """Client for one accessory reachable at ``host:port``.

    Attributes mirror the DNS-SD / accessory-info identity of the device
    so callers can pass a discovery.DiscoveredDevice straight in.
    """

    def __init__(
        self,
        host: str,
        port: int = 9123,
        name: str = "",
        model: str = "",
        mac: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.name = name or host
        self.model = model
        self.mac = mac
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        expect_body: bool = True,
    ) -> Optional[Dict[str, Any]]:
        url = "http://%s:%d%s" % (self.host, self.port, path)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            # NOTE: deliberately no 'Origin' header -- devices reject it (403).

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = err.read().decode("utf-8", "replace").strip()[:200]
            except Exception:
                pass
            raise CommandError(
                "%s: %s %s -> HTTP %d %s" % (self.name, method, path, err.code, detail)
            )
        except (urllib.error.URLError, socket.timeout, OSError) as err:
            raise DeviceUnreachable("%s: %s %s failed (%s)" % (self.name, method, path, err))

        if not expect_body or not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as err:
            raise ElgatoError("%s: %s returned invalid JSON: %s" % (self.name, path, err))

    # ------------------------------------------------------------------ #
    # identity
    # ------------------------------------------------------------------ #

    def get_info(self) -> Dict[str, Any]:
        """GET /elgato/accessory-info (firmware >= 1.0.3 only)."""
        return self._request("GET", INFO_PATH) or {}

    def set_display_name(self, display_name: str) -> Dict[str, Any]:
        """Rename the light; the name is stored on the device (fw >= 188)."""
        return self._request("PUT", INFO_PATH, {"displayName": display_name}) or {}

    # ------------------------------------------------------------------ #
    # light state
    # ------------------------------------------------------------------ #

    def get_state(self) -> Dict[str, Any]:
        """GET /elgato/lights -> {"numberOfLights": 1, "lights": [ ... ]}."""
        return self._request("GET", LIGHTS_PATH) or {}

    def _put_state(self, light: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"numberOfLights": 1, "lights": [light]}
        return self._request("PUT", LIGHTS_PATH, payload) or {}

    def set_state(
        self,
        on: Optional[bool] = None,
        brightness: Optional[int] = None,
        temperature: Optional[int] = None,
        hue: Optional[float] = None,
        saturation: Optional[float] = None,
        verify: bool = True,
    ) -> Dict[str, Any]:
        """Write one or more properties to lights[0] and return the new state.

        Only supplied arguments are transmitted (partial PUT) -- the
        device keeps every field the request omits. White-capable lamps
        take `temperature` (mireds); color-capable lamps (Light Strip)
        take `hue` (0-360) and `saturation` (0-100). When `verify` is
        set, the returned state is compared against the request because
        devices confirm invalid writes with HTTP 200 while keeping the
        old value (design doc section 7.2).
        """
        write: Dict[str, Any] = {}
        if on is not None:
            write["on"] = 1 if on else 0
        if brightness is not None:
            if not 0 <= brightness <= 100:
                raise ValueError("brightness must be 0-100, got %s" % brightness)
            write["brightness"] = int(brightness)
        if temperature is not None:
            write["temperature"] = int(temperature)
        if hue is not None:
            write["hue"] = float(hue)
        if saturation is not None:
            write["saturation"] = float(saturation)

        if not write:
            return self.get_state()

        result = self._put_state(write)
        lights = result.get("lights") or []
        if not lights:
            raise NoLightsError("%s reports no controllable lights" % self.name)

        if verify:
            for key, expected in write.items():
                actual = lights[0].get(key)
                if actual != expected:
                    raise VerifyError(
                        "%s did not apply %s=%s (still %r); the device may have "
                        "silently rejected the value" % (self.name, key, expected, actual)
                    )
        return result

    def set_on(self, on: bool, verify: bool = True) -> Dict[str, Any]:
        return self.set_state(on=on, verify=verify)

    def set_brightness(self, brightness: int, verify: bool = True) -> Dict[str, Any]:
        return self.set_state(brightness=brightness, verify=verify)

    def set_temperature(self, temperature: int, verify: bool = True) -> Dict[str, Any]:
        return self.set_state(temperature=temperature, verify=verify)

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    def get_settings(self) -> Dict[str, Any]:
        """GET /elgato/lights/settings."""
        return self._request("GET", SETTINGS_PATH) or {}

    def patch_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """PUT a partial /elgato/lights/settings document."""
        if not changes:
            return self.get_settings()
        return self._request("PUT", SETTINGS_PATH, changes) or {}

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def identify(self) -> None:
        """POST /elgato/identify -- the light flashes."""
        self._request("POST", IDENTIFY_PATH, expect_body=False)

    def restart(self) -> None:
        """POST /elgato/restart -- reboot the accessory."""
        self._request("POST", RESTART_PATH, expect_body=False)

    def factory_reset(self) -> None:
        """POST /elgato/factory-reset -- erase settings and reboot."""
        self._request("POST", FACTORY_RESET_PATH, expect_body=False)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def first_light(self) -> Dict[str, Any]:
        """Return the first element of the lights array (fresh GET)."""
        state = self.get_state()
        lights: List[Dict[str, Any]] = state.get("lights") or []
        if not lights:
            raise NoLightsError("%s reports no controllable lights" % self.name)
        return lights[0]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "ElgatoDevice(host=%r, port=%r, name=%r)" % (self.host, self.port, self.name)
