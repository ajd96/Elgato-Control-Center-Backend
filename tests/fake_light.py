"""A protocol-faithful fake Elgato light for local testing.

This simulates the key behaviours of a Key Light's embedded web server
as documented in ``Elgato-Control-Center-System-Design.md`` section 8:

- HTTP/1.1, JSON bodies, no TLS
- requests carrying an ``Origin`` header are rejected with 403
- PUT merges partial documents; omitted fields keep their value
- out-of-range ``brightness`` is silently acknowledged (HTTP 200) while
  the stored value does not change  ->  triggers client VerifyError
- out-of-range temperature is applied at the clamp echo but NOT stored
- POST /elgato/identify and /elgato/restart return empty bodies
- accessory-info exposes productName, firmware, serial, features

Usage (standalone):

    python3 tests/fake_light.py [--port 9123] [--serial ...]
"""

import argparse
import http.server
import json
import threading
from typing import Any, Dict, List, Optional


class FakeLightServer:
    """Runs a fake device on 127.0.0.1:port (port 0 -> chosen automatically)."""

    def __init__(self, accessory: Optional[Dict[str, Any]] = None) -> None:
        if accessory is None:
            accessory = {}
        self.accessory = {
            "productName": "Elgato Key Light",
            "hardwareBoardType": 53,
            "firmwareBuildNumber": 238,
            "firmwareVersion": "1.0.4",
            "serialNumber": "BW21K1A01548",
            "displayName": "Test Key Light",
            "features": ["lights"],
            "wifi-info": {"ssid": "testsuite", "frequencyMHz": 2437, "rssi": -42},
        }
        self.accessory.update(accessory)

        self.lights: List[Dict[str, Any]] = [
            {"on": 0, "brightness": 25, "temperature": 230}
        ]
        self.settings: Dict[str, Any] = {
            "powerOnBehavior": 1,
            "powerOnBrightness": 20,
            "powerOnTemperature": 230,
            "switchOnDurationMs": 100,
            "switchOffDurationMs": 300,
            "colorChangeDurationMs": 100,
        }
        self.requests: List[Dict[str, Any]] = []
        self.identify_count = 0
        self.restart_count = 0
        # When True, the device acknowledges writes with 200 but does not
        # apply them (exercises the client's VerifyError path for the
        # documented "silent rejection" device misbehaviour).
        self.defective = False

    def merge_light_write(self, index: int, write: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a partial PUT with device firmware semantics.

        Returns the document the device echoes in the PUT response. In
        normal mode the echo mirrors the freshly stored state; when a
        value is rejected (defective mode, or out-of-range input) the
        echo keeps the PREVIOUSLY stored value, which is what lets the
        client's write verification detect the silent rejection.
        """
        updated = dict(self.lights[index])

        if "on" in write:
            updated["on"] = 1 if write["on"] else 0
        if "brightness" in write:
            value = write["brightness"]
            if (
                not self.defective
                and isinstance(value, (int, float))
                and 0 <= value <= 100
            ):
                updated["brightness"] = int(value)
            # else: keep previously stored value -> lands in the echo too
        if "temperature" in write:
            value = write["temperature"]
            if (
                not self.defective
                and isinstance(value, (int, float))
                and MIRED_MIN <= value <= MIRED_MAX
            ):
                updated["temperature"] = int(value)
            # else: out-of-range writes echo the previous stored value
        if "hue" in write:  # color devices
            updated["hue"] = write["hue"]
        if "saturation" in write:
            updated["saturation"] = write["saturation"]

        self.lights[index] = updated
        return dict(updated)

    # -- lifecycle -------------------------------------------------------- #
    @property
    def port(self) -> int:
        server_address = self._http.server_address
        return server_address[1]

    def _start_http(self) -> None:
        """Create and launch the embedded HTTP server (binds 127.0.0.1:0)."""
        self._http = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(self)
        )
        self._thread = threading.Thread(target=self._http.serve_forever)
        self._thread.daemon = True

    def start(self) -> "FakeLightServer":
        self._start_http()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join(timeout=3)

    @property
    def endpoint(self) -> str:
        return "127.0.0.1:%d" % self.port

    # -- helpers for tests ------------------------------------------------ #

    def last_request(self) -> Dict[str, Any]:
        return self.requests[-1]


# --------------------------------------------------------------------------- #
# request merging (device firmware behaviour)
# --------------------------------------------------------------------------- #

MIRED_MIN, MIRED_MAX = 143, 344


def _make_handler(server: FakeLightServer) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -- plumbing ------------------------------------------------- #

        def _json(self, status: int, doc: Any) -> None:
            body = json.dumps(doc).encode("utf-8") if doc is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _empty(self, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_body(self) -> Optional[bytes]:
            length = self.headers.get("Content-Length")
            if not length:
                return b""
            try:
                return self.rfile.read(int(length))
            except ValueError:
                return b""

        def _log(self, body: Optional[bytes]) -> None:
            server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "origin": self.headers.get("Origin"),
                    "body": json.loads(body.decode()) if body else None,
                }
            )

        # -- routing ---------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802
            self._send_get()

        def do_PUT(self) -> None:  # noqa: N802
            self._send_put()

        def do_POST(self) -> None:  # noqa: N802
            self._send_post()

        def _send_get(self) -> None:
            path = self.path.split("?")[0]
            self._read_body()  # GETs should not carry bodies; harmless
            if self.headers.get("Origin"):
                self._log(None)
                self._json(403, {"error": "Origin not allowed"})
                return
            if path == "/elgato/lights":
                self._log(None)
                self._json(200, {"numberOfLights": len(server.lights), "lights": server.lights})
            elif path == "/elgato/lights/settings":
                self._log(None)
                self._json(200, server.settings)
            elif path == "/elgato/accessory-info":
                self._log(None)
                self._json(200, server.accessory)
            else:
                self._log(None)
                self._json(404, {"error": "no such endpoint"})

        def _send_put(self) -> None:
            path = self.path.split("?")[0]
            body = self._read_body() or b"{}"
            self._log(body)
            if self.headers.get("Origin"):
                self._json(403, {"error": "Origin not allowed"})
                return
            try:
                doc = json.loads(body.decode()) if body else {}
            except ValueError:
                self._json(400, {"error": "invalid json"})
                return

            if path == "/elgato/lights":
                lights = doc.get("lights") or []
                if not lights:
                    self._json(400, {"error": "missing lights array"})
                    return
                index = 0
                applied = server.merge_light_write(index, lights[index])
                self._json(
                    200, {"numberOfLights": len(server.lights), "lights": [applied]}
                )
            elif path == "/elgato/lights/settings":
                server.settings.update(doc)
                self._json(200, server.settings)
            elif path == "/elgato/accessory-info":
                if "displayName" in doc:
                    server.accessory["displayName"] = doc["displayName"]
                    self._json(200, server.accessory)
                else:
                    self._json(400, {"error": "displayName required"})
            else:
                self._json(404, {"error": "no such endpoint"})

        def _send_post(self) -> None:
            path = self.path.split("?")[0]
            body = self._read_body()
            self._log(body if body else None)
            if self.headers.get("Origin"):
                self._json(403, {"error": "Origin not allowed"})
                return
            if path == "/elgato/identify":
                server.identify_count += 1
                self._empty(200)
            elif path == "/elgato/restart":
                server.restart_count += 1
                self._empty(200)
            else:
                self._json(404, {"error": "no such endpoint"})

        def log_message(self, *_args) -> None:  # silence request logging
            pass

    return Handler


if __name__ == "__main__":  # pragma: no cover - manual demo server
    arg_parser = argparse.ArgumentParser(description="run a fake Elgato light")
    arg_parser.add_argument("--port", type=int, default=9123)
    arg_parser.add_argument("--name", default="Fake Key Light")
    options = arg_parser.parse_args()

    _server = FakeLightServer()
    _server.accessory["displayName"] = options.name

    demo_http = http.server.ThreadingHTTPServer(("0.0.0.0", options.port), _make_handler(_server))
    print("fake light served on http://<host>:%d/elgato/lights" % options.port)
    try:
        demo_http.serve_forever()
    except KeyboardInterrupt:
        pass

