#!/usr/bin/env python3
"""elgatoctl - control Elgato Key Lights from Linux.

A clean-room, dependency-free rewrite of the Elgato Control Center
control plane for Linux. Discovers accessories via mDNS (``_elg._tcp``)
and drives their HTTP/JSON API on TCP 9123. Requires Python >= 3.7.

Quick start:

    elgatoctl discover
    elgatoctl status
    elgatoctl set "Elgato Key Light F3DF" --on on --brightness 40
    elgatoctl set desk --temperature 4200k
    elgatoctl alias set desk "Elgato Key Light F3DF"

Groups: matching several devices at once - run `elgatoctl group show`.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

try:
    from elgatoctl import discovery, device, parallel, units
except ImportError:  # running as a plain script/checkout, not installed
    import discovery  # type: ignore
    import device  # type: ignore
    import parallel  # type: ignore
    import units  # type: ignore

PROG = "elgatoctl"

# Exit codes (also documented in README) ----------------------------------
EXIT_OK = 0
EXIT_USAGE = 2            # argparse default for usage errors
EXIT_NO_DEVICES = 3
EXIT_DEVICE_ERROR = 4

# Exit code 5 = a write did not stick (device confirmed 200 but ignored it)
EXIT_VERIFY_FAILED = 5

# System theme integration -------------------------------------------------
# Control Center on Windows follows the OS setting; on Linux we respect the
# NO_COLOR convention (https://no-color.org) plus a --color switch instead.


# --------------------------------------------------------------------------
# Configuration (~/.config/elgatoctl/named-lights.json)
# --------------------------------------------------------------------------

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "elgatoctl"
)
NAMED_FILE = os.path.join(CONFIG_DIR, "named-lights.json")


def load_named_lights() -> Dict[str, Any]:
    try:
        with open(NAMED_FILE, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return {"names": {}}
    names = doc.get("names")
    return {"names": names if isinstance(names, dict) else {}}


def save_named_lights(store: Dict[str, Any]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(NAMED_FILE, "w", encoding="utf-8") as handle:
        json.dump(store, handle, indent=2, sort_keys=True)


def device_key(mac: str, ip: str) -> str:
    """Stable storage key: MAC when known, otherwise IP."""
    return (mac or "").strip().upper() or ip


# --------------------------------------------------------------------------
# Session (single discovery pass per invocation)
# --------------------------------------------------------------------------

class Session:
    """Discovers devices lazily; one mDNS pass per command invocation."""

    def __init__(self, timeout: float, interface: Optional[str]) -> None:
        self.timeout = timeout
        self.interface = interface
        self._found: Optional[List[Any]] = None

    def devices(self) -> List[Any]:
        if self._found is None:
            self._found = parallel.query_devices(
                discovery.discover(self.timeout, self.interface)
            )
        return self._found


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

_use_color: Optional[bool] = None


def _decide_color() -> None:
    global _use_color
    if _use_color is None:
        _use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def set_color_mode(enabled: bool) -> None:
    """Force colors on/off (called from the global --color/--no-color flag)."""
    global _use_color
    _use_color = enabled


def C(code: str, text: str) -> str:
    _decide_color()
    if not _use_color:
        return text
    return "\x1b[%sm%s\x1b[0m" % (code, text)


def warn(message: str) -> None:
    print(C("33", "warning:"), message, file=sys.stderr)


def fail(message: str) -> None:
    print(C("1;31", "error:"), message, file=sys.stderr)


class CommandError(Exception):
    """Control-flow error carrying the process exit code."""

    def __init__(self, message: str, code: int = EXIT_DEVICE_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def format_device_line(device: Any) -> str:
    bits = [C("1", device.name)]
    if device.model:
        bits.append(device.model)
    bits.append("%s:%d" % (device.ip, device.port))
    if device.mac:
        bits.append("mac " + device.mac)
    return "  ".join(bits)


def temperature_display(mired_value: int) -> str:
    return "%dK (mired %d)" % (units.mired_to_kelvin(int(mired_value)), int(mired_value))


def render_state_rows(device_name: str, lights: List[Dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"device": device_name, "lights": lights}, indent=2))
        return
    for index, light in enumerate(lights):
        power = "on" if light.get("on") else "off"
        row = [
            "power %s" % C("1", power),
            "brightness %s%%" % light.get("brightness"),
            "temperature %s" % temperature_display(int(light.get("temperature", units.MIRED_MIN))),
        ]
        if "hue" in light:
            row.insert(2, "hue %s" % light.get("hue"))
        if "saturation" in light:
            row.insert(3, "saturation %s" % light.get("saturation"))
        prefix = device_name
        if len(lights) > 1:
            prefix = "lights[%d] " % index + device_name
        print("%s: %s" % (prefix, "  ".join(row)))


def render_settings(doc: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return
    for key in sorted(doc):
        value = doc[key]
        if isinstance(value, dict):
            print("%s:" % key)
            for sub_key in sorted(value):
                print("  %s = %s" % (sub_key, json.dumps(value[sub_key], sort_keys=True)))
        else:
            print("%s = %s" % (key, value))


# --------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------

def resolve_targets(session: Session, patterns: Optional[List[str]]) -> List[Any]:
    """Map CLI patterns onto matching accessories.

    A pattern matches when it equals (case-insensitive) the mDNS instance
    name, MAC address, IP, mDNS host name, or a user-assigned alias from
    named-lights.json. With no patterns every known device is selected:
    paired (aliased) devices always load from storage even when offline;
    otherwise all devices found by discovery are used.
    """
    store = load_named_lights()

    def as_client(found: Any) -> Any:
        return device.ElgatoDevice(
            host=found.ip, port=found.port,
            name=found.name, model=found.model, mac=found.mac,
        )

    if not patterns:
        chosen: List[Any] = []
        if store["names"]:
            for key, meta in store["names"].items():
                chosen.append(
                    device.ElgatoDevice(
                        host=meta.get("ip") or key,
                        port=int(meta.get("port", 9123)),
                        name=meta.get("name", key),
                    )
                )
            return chosen
        found = session.devices()
        if not found:
            raise CommandError(
                "no Elgato lights found; run '%s discover' to check the network" % PROG,
                EXIT_NO_DEVICES,
            )
        return [as_client(d) for d in found]

    lowered = [p.lower() for p in patterns]
    alias_index = {meta.get("name", "").lower(): (key, meta) for key, meta in store["names"].items()}

    chosen = []
    seen_hosts = set()
    for pattern in lowered:
        matched = False
        for found in session.devices():
            aliases = {
                found.name.lower(),
                found.mac.lower(),
                found.ip,
                found.ip + ":" + str(found.port),
                (found.host or "").lower(),
            }
            if pattern in aliases:
                matched = True
                client = as_client(found)
                if client.host not in seen_hosts:
                    seen_hosts.add(client.host)
                    chosen.append(client)
        if pattern in alias_index:
            alias_key, alias_meta = alias_index[pattern]
            if matched:
                warn("pattern %r matched both network and saved alias; using the discovered device" % pattern)
            else:
                host = alias_meta.get("ip") or alias_key
                if host not in seen_hosts:
                    seen_hosts.add(host)
                    chosen.append(
                        device.ElgatoDevice(
                            host=host,
                            port=int(alias_meta.get("port", 9123)),
                            name=alias_meta.get("name", pattern),
                        )
                    )
            matched = True
        if not matched:
            # Literal-IP fallback: "192.168.1.50" or "192.168.1.50:9123"
            # reaches paired devices that are not currently discoverable
            # (mDNS blocked, powered off, different segment + route).
            endpoint = _parse_ip_endpoint(pattern)
            if endpoint is not None:
                host, port = endpoint
                chosen.append(
                    device.ElgatoDevice(
                        host=host, port=port, name=pattern
                    )
                )
                matched = True
        if not matched:
            raise CommandError("no device matches %r" % pattern, EXIT_DEVICE_ERROR)
    return chosen


def _parse_ip_endpoint(pattern: str) -> Optional[Any]:
    """Return (host, port) for a dotted-quad address, else None."""
    parts = pattern.split(":")
    if 1 <= len(parts) <= 2 and pattern[0].isdigit():
        host = parts[0]
        octets = host.split(".")
        looks_like_ip = (
            len(octets) == 4
            and all(o.isdigit() and 0 <= int(o) <= 255 for o in octets)
        )
        if looks_like_ip:
            port = 9123
            if len(parts) == 2 and parts[1].isdigit():
                port = int(parts[1])
            return host, port
    return None


def require_single(session: Session, patterns: Optional[List[str]]) -> Any:
    targets = resolve_targets(session, patterns)
    if len(targets) != 1:
        raise CommandError(
            "this command needs exactly one matching device (got %d)" % len(targets),
            EXIT_USAGE,
        )
    return targets[0]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_discover(args: Any) -> None:
    found = session_from(args).devices()
    if not found:
        raise CommandError(
            "nothing found. Check: same L2 segment, multicast enabled on the "
            "AP (Multicast/mDNS/IGMP snooping; Broadcast filtering OFF), and "
            "UDP 5353 allowed by the firewall both ways.",
            EXIT_NO_DEVICES,
        )
    for found_device in found:
        print(format_device_line(found_device))


def cmd_status(args: Any) -> None:
    session = session_from(args)
    targets = resolve_targets(session, args.targets)

    def fetch(client: Any) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        try:
            info = client.get_info()
        except device.CommandError as err:
            # accessory-info appeared in firmware 1.0.3; older units 404.
            if "404" not in str(err):
                raise
        state = client.get_state()
        return {
            "name": info.get("displayName") or info.get("productName") or client.name,
            "ip": client.host,
            "model": info.get("productName", "") or client.model or "?",
            "firmware": info.get("firmwareVersion", "unknown"),
            "lights": state.get("lights", []),
        }

    results, failures = parallel.act_on_devices(targets, fetch, args.parallel)
    for client, entry in results:
        if args.json:
            print(json.dumps(entry, indent=2))
        else:
            render_state_rows(entry["name"], entry["lights"], False)
            print("  firmware %s  model %s" % (entry["firmware"], entry["model"]))
    if failures:
        for client, err in failures:
            fail("%s: %s" % (client.name, err))
        raise CommandError("%d device(s) could not be queried" % len(failures))


def cmd_info(args: Any) -> None:
    client = require_single(session_from(args), args.targets)
    print(json.dumps(client.get_info(), indent=2, sort_keys=True))


def cmd_on_off(args: Any) -> None:
    targets = resolve_targets(session_from(args), args.targets)
    value_on = args.forced_state == "on"

    def setter(client: Any) -> Dict[str, Any]:
        return client.set_state(on=value_on)

    results, failures = parallel.act_on_devices(targets, setter, args.parallel)
    for client, state in results:
        light = (state.get("lights") or [{}])[0]
        rendered = C("1;32" if light.get("on") else "2", "on" if light.get("on") else "off")
        print("%-32s %s" % (client.name, rendered))
    report_failures(failures)


def cmd_set(args: Any) -> None:
    session = session_from(args)
    targets = resolve_targets(session, args.targets)

    on_value, hue_value, sat_value = None, None, None
    if args.on is not None:
        on_value = args.on == "on"
    if args.color is not None:
        hue_value, sat_value = _parse_color(args.color)

    if not has_changes(args):
        raise CommandError("nothing to change; use --on, --brightness, --temperature and/or --color", EXIT_USAGE)

    temperature = None
    if args.temperature is not None:
        try:
            temperature = units.parse_temperature(args.temperature)
        except ValueError as err:
            raise CommandError(str(err), EXIT_USAGE)

    brightness = args.brightness
    if brightness is not None and not 0 <= brightness <= 100:
        raise CommandError("--brightness must be 0-100", EXIT_USAGE)

    def setter(client: Any) -> Dict[str, Any]:
        return client.set_state(
            on=on_value,
            brightness=brightness,
            temperature=temperature,
            hue=hue_value,
            saturation=sat_value,
        )

    results, failures = parallel.act_on_devices(targets, setter, args.parallel)
    for client, state in results:
        light = (state.get("lights") or [{}])[0]
        parts = ["on=%d" % light.get("on", 0), "brightness=%s%%" % light.get("brightness")]
        if "temperature" in light:
            parts.append("temperature=%s" % temperature_display(int(light["temperature"])))
        if "hue" in light:
            parts.append("hue=%s saturation=%s" % (light.get("hue"), light.get("saturation")))
        print("%-32s %s" % (client.name, "  ".join(parts)))
    report_failures(failures)


def has_changes(args: Any) -> bool:
    return (
        args.on is not None
        or args.brightness is not None
        or args.temperature is not None
        or getattr(args, "color", None) is not None
    )


def _parse_color(text: str) -> Any:
    """Parse 'HUE:SAT' (e.g. '210:80') into floats; both halves required."""
    token = text.strip()
    if ":" not in token:
        raise CommandError("--color takes HUE:SAT, e.g. --color 210:80", EXIT_USAGE)
    hue_text, _, sat_text = token.partition(":")
    try:
        hue = float(hue_text)
        sat = float(sat_text)
    except ValueError:
        raise CommandError("--color values must be numeric (HUE:SAT)", EXIT_USAGE)
    if not 0 <= hue <= 360:
        raise CommandError("--color hue must be 0-360", EXIT_USAGE)
    if not 0 <= sat <= 100:
        raise CommandError("--color saturation must be 0-100", EXIT_USAGE)
    return hue, sat


def report_failures(failures: List[Any]) -> None:
    if not failures:
        return
    printable = []
    any_verify = False
    for client, err in failures:
        any_verify = any_verify or isinstance(err, device.VerifyError)
        printable.append("%s: %s" % (client.name, err))
        fail(printable[-1])
    raise CommandError(
        "%d device(s) failed\n    %s"
        % (len(failures), "\n    ".join(printable)),
        EXIT_VERIFY_FAILED if any_verify else EXIT_DEVICE_ERROR,
    )


def cmd_group(args: Any) -> None:
    """Ad-hoc grouping: address several devices with one invocation."""
    if not args.targets:
        print("usage: %s group PATTERN [PATTERN...]" % PROG)
        return
    targets = resolve_targets(session_from(args), args.targets)
    print("group of %d: %s" % (len(targets), ", ".join(t.name for t in targets)))


def cmd_settings(args: Any) -> None:
    client = require_single(session_from(args), args.targets)
    render_settings(client.get_settings(), args.json)


def cmd_set_settings(args: Any) -> None:
    session = session_from(args)
    # Accept KEY=VALUE pairs and device patterns in any order: entries
    # containing '=' are settings, everything else is a device pattern.
    assignments: List[str] = []
    patterns: List[str] = []
    for entry in args.extras:
        if "=" in entry:
            assignments.append(entry)
        else:
            patterns.append(entry)

    changes: Dict[str, Any] = {}
    for assignment in assignments:
        key, _, value = assignment.partition("=")
        changes[key.strip()] = units.parse_scalar(value)
    if not changes:
        raise CommandError(
            "nothing to set; usage: %s set-settings KEY=VALUE [KEY=VALUE...] [DEVICE...]"
            % PROG,
            EXIT_USAGE,
        )
    client = require_single(session, patterns)
    updated = client.patch_settings(changes)
    print("settings updated on %s" % client.name)
    render_settings(updated, args.json)


def cmd_name(args: Any) -> None:
    client = require_single(session_from(args), args.targets)
    client.set_display_name(args.new_name)
    print("display name stored on the device: %r" % args.new_name)


def cmd_identify(args: Any) -> None:
    targets = resolve_targets(session_from(args), args.targets)
    for client in targets:
        client.identify()
        print("%s: flashing" % client.name)


def cmd_firmware(args: Any) -> None:
    targets = resolve_targets(session_from(args), args.targets)

    def fetch(client: Any) -> Dict[str, Any]:
        info = client.get_info()
        return {
            "name": client.name,
            "version": info.get("firmwareVersion", "?"),
            "build": info.get("firmwareBuildNumber", "?"),
        }

    results, failures = parallel.act_on_devices(targets, fetch, args.parallel)
    for _client, entry in results:
        print("%-32s firmware %s (build %s)" % (entry["name"], entry["version"], entry["build"]))
    report_failures(failures)


def cmd_reboot(args: Any) -> None:
    for client in resolve_targets(session_from(args), args.targets):
        client.restart()
        print("%s: restart requested (light blinks twice while booting, ~15 s)" % client.name)


def cmd_factory_reset(args: Any) -> None:
    if not args.yes:
        raise CommandError(
            "refusing to factory-reset without --yes; this erases Wi-Fi "
            "credentials and all device settings",
            EXIT_USAGE,
        )
    client = require_single(session_from(args), args.targets)
    client.factory_reset()
    print("%s: factory reset sent; the light boots into pairing mode" % client.name)


# --------------------------------------------------------------------------
# Alias management (~/.config/elgatoctl/named-lights.json)
# --------------------------------------------------------------------------

def cmd_alias(args: Any) -> None:
    action = getattr(args, "action", None) or "list"  # bare `alias` lists
    if action == "list":
        store = load_named_lights()
        if not store["names"]:
            print("no aliases saved (use '%s alias set NAME DEVICE')" % PROG)
            return
        for key, meta in sorted(store["names"].items(), key=lambda kv: kv[1].get("name", "")):
            print(
                "%-14s -> %s  (%s:%s, mac %s)"
                % (meta.get("name", "?"), key, meta.get("ip", "?"), meta.get("port", 9123), meta.get("mac", "?"))
            )
        return

    if args.action == "set":
        store = load_named_lights()
        found = session_from(args).devices()
        match = None
        for candidate in found:
            if args.device.lower() in (
                candidate.name.lower(),
                candidate.mac.lower(),
                candidate.ip,
                candidate.ip + ":" + str(candidate.port),
                (candidate.host or "").lower(),
            ):
                match = candidate
                break
        if match is None:
            # Offline fallback: identifier may be a literal "IP[:PORT]" for
            # a paired light that is not answering mDNS right now. Probe it
            # directly (parity with the manual-IP pairing feature on
            # Windows, design doc section 9.3).
            endpoint = _parse_ip_endpoint(args.device.lower())
            if endpoint is not None:
                host, port = endpoint
                probe = device.ElgatoDevice(host=host, port=port, name=args.device)
                try:
                    info = probe.get_info()
                except device.ElgatoError:
                    info = {}
                if info:
                    match = device.ElgatoDevice(
                        host=host,
                        port=port,
                        name=info.get("displayName") or info.get("productName") or args.device,
                        model=info.get("productName", ""),
                        mac=info.get("macAddress", ""),
                    )
        if match is None:
            raise CommandError(
                "device %r not found on the network; run '%s discover'"
                % (args.device, PROG),
                EXIT_DEVICE_ERROR,
            )
        # Normalise attributes across sources: discovery results carry the
        # authoritative .ip address, offline probes (ElgatoDevice) only
        # have .host. DNS names are NOT stored -- IPs are (DHCP changes
        # affect .local names, and some hosts cannot resolve them).
        match_ip = getattr(match, "ip", None) or getattr(match, "host")
        match_ip = match_ip.rstrip(".") if isinstance(match_ip, str) else match_ip
        store["names"][device_key(match.mac, match_ip)] = {
            "name": args.short_name,
            "ip": match_ip,
            "port": match.port,
            "mac": match.mac,
            "model": match.model,
        }
        save_named_lights(store)
        print(
            "saved alias %r -> %s (%s:%d, mac %s)"
            % (args.short_name, match.name, match_ip, match.port, match.mac or "?")
        )
        return

    if args.action == "remove":
        store = load_named_lights()
        target_key = None
        for key, meta in store["names"].items():
            if meta.get("name", "").lower() == args.short_name.lower():
                target_key = key
                break
        if target_key is None:
            raise CommandError("alias %r not found" % args.short_name, EXIT_DEVICE_ERROR)
        del store["names"][target_key]
        save_named_lights(store)
        print("removed alias %r" % args.short_name)
        return

    raise CommandError("unknown alias action %r" % args.action, EXIT_USAGE)


# --------------------------------------------------------------------------
# Argument parser and entry point
# --------------------------------------------------------------------------

class ColorAction(argparse.Action):
    """--color / --no-color tri-state flag."""

    def __call__(self, parser, namespace, values, option_string=None):
        namespace.color_mode = option_string == "--color"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Control Elgato Key Lights and Light Strips from Linux "
        "(mDNS discovery + HTTP API; no external dependencies).",
        epilog="examples:\n"
        "  %(prog)s discover\n"
        "  %(prog)s status\n"
        "  %(prog)s set desk --brightness 40 --temperature 5000k\n"
        "  %(prog)s set --on off                # all paired lights\n"
        "  %(prog)s alias set desk \"Elgato Key Light F3DF\"\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout", type=float, default=2.5, metavar="SEC",
        help="mDNS discovery window in seconds (default: 2.5)",
    )
    parser.add_argument(
        "--interface", metavar="IP",
        help="local interface address to use for mDNS (multihomed hosts)",
    )
    parser.add_argument(
        "--parallel/--sequential", dest="parallel", default=True,
        help="command several devices concurrently (default: parallel)",
    )
    parser.add_argument(
        "--color/--no-color", dest="color_mode", action=ColorAction, nargs=0, default=None,
        help="force ANSI colors on or off (default: auto per NO_COLOR/isatty)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--yes", action="store_true", help="answer yes to destructive prompts")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("discover", help="list accessories visible on the network")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("status", help="power/brightness/temperature per light")
    p.add_argument("targets", nargs="*", metavar="PATTERN", help="device name, alias, MAC or IP")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("info", help="full accessory-info document for one device")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("on", help="power lights on")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_on_off, forced_state="on")

    p = sub.add_parser("off", help="power lights off")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_on_off, forced_state="off")

    p = sub.add_parser("set", help="change brightness/temperature/on state")
    p.add_argument("--on", choices=["on", "off"], default=None, help="power state")
    p.add_argument("--brightness", type=int, metavar="0-100")
    p.add_argument(
        "--temperature", metavar="K|M",
        help="color temperature: Kelvin (e.g. 5000k) or mireds with m suffix (e.g. 200m)",
    )
    p.add_argument(
        "--color", metavar="HUE:SAT",
        help="Light Strip / Ring Light colour: hue 0-360, saturation 0-100 (e.g. 210:80)",
    )
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("group", help="show which devices a multi-device pattern matches")
    p.add_argument("targets", nargs="+", metavar="PATTERN")
    p.set_defaults(func=cmd_group)

    p = sub.add_parser("settings", help="show /elgato/lights/settings for one device")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("set-settings", help="patch settings, e.g. powerOnBehavior=2")
    p.add_argument("extras", nargs="+", metavar="KEY=VALUE or PATTERN",
                   help="KEY=VALUE pairs and device patterns in any order")
    p.set_defaults(func=cmd_set_settings)

    p = sub.add_parser("name", help="rename a device (stored on the light, fw >= 1.0.3 build 188)")
    p.add_argument("new_name")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser("identify", help="flash the matched lights")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("firmware", help="show firmware version/build")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_firmware)

    p = sub.add_parser("reboot", help="restart one or more lights")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("factory-reset", help="erase device settings (needs --yes)")
    p.add_argument("targets", nargs="*", metavar="PATTERN")
    p.set_defaults(func=cmd_factory_reset)

    p = sub.add_parser("alias", help="save short names for devices")
    p.set_defaults(action="list")  # bare `elgatoctl alias` lists aliases
    sub_alias = p.add_subparsers(dest="action", metavar="ACTION")
    pa = sub_alias.add_parser("set")
    pa.add_argument("short_name")
    pa.add_argument("device", help="current device name, MAC or IP")
    pa.set_defaults(action="set")
    pa = sub_alias.add_parser("remove")
    pa.add_argument("short_name")
    pa.set_defaults(action="remove")
    pa = sub_alias.add_parser("list")
    pa.set_defaults(action="list")
    p.set_defaults(func=cmd_alias)

    return parser


def session_from(args: Any) -> Session:
    return Session(timeout=args.timeout, interface=args.interface)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.color_mode is not None:
        set_color_mode(args.color_mode)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    try:
        args.func(args)
        return EXIT_OK
    except device.DeviceUnreachable as err:
        fail(str(err))
        return EXIT_DEVICE_ERROR
    except device.CommandError as err:
        fail(str(err))
        return EXIT_DEVICE_ERROR
    except device.VerifyError as err:
        fail(str(err))
        return EXIT_VERIFY_FAILED
    except device.ElgatoError as err:
        fail(str(err))
        return EXIT_DEVICE_ERROR
    except CommandError as err:
        fail(str(err))
        return err.code
    except KeyboardInterrupt:
        fail("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
