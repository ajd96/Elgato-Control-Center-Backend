# elgatoctl — Elgato lights on Linux

A clean-room, dependency-free rewrite of the **Elgato Control Center**
control plane for Linux. Discovers Elgato Wi-Fi lighting accessories via
mDNS and drives their HTTP/JSON API directly — no cloud, no third-party
packages, no Elgato software required on the machine.

```
$ elgatoctl discover
Elgato Key Light F3DF        Elgato Key Light 20LAB9901  192.168.1.11:9123  mac 3C:6A:9D:16:12:45

$ elgatoctl set desk --on on --brightness 40 --temperature 4900k
desk                             on=1  brightness=40%  temperature=4902K (mired 217)
```

**Requires:** Python ≥ 3.7 (standard library only).

Protocol ground truth comes from the companion document
[Elgato-Control-Center-System-Design.md](../Elgato-Control-Center-System-Design.md)
(sections referenced below).

---

## Why a rewrite

Elgato publishes Control Center only for Windows/macOS/iOS/Android. What
those clients *actually do* — and what this tool reimplements — is:

1. browse DNS-SD service `_elg._tcp.local` (UDP 5353, multicast 224.0.0.251)
   → design doc *§6.2*;
2. store the accessory IP, then talk plaintext HTTP/1.1 + JSON on
   **TCP 9123** → design doc *§6.1, §8*;
3. poll state at a dense cadence so UIs, Stream Deck, and mobile apps converge
   → design doc *§7.3*;
4. keep names, power-on behavior, and fade durations **on the device itself**
   → design doc *§8.1*.

The device API is fully local: there is no Elgato cloud dependency for
control, and anything on the LAN (Home Assistant, GEM, keylightd, GNOME
extensions, …) talks the same protocol. This project is simply a first-class
Linux client with the same trust model.

---

## Install

**Run from the checkout (no install):**

```sh
python3 -m elgatoctl --help          # from this directory
python3 elgatoctl/cli.py --help      # also works as a plain script
```

**Install for real:**

```sh
pipx install /home/ashleighd/OpenCode/elgato-linux    # or: pip install .
elgatoctl --help
```

`pipx install`/`pip install` exposes a `console_scripts` entry point
(`elgatoctl`) plus the `elgatoctl` module. There are **zero runtime
dependencies** — the mDNS resolver is implemented on raw sockets and the
HTTP client on `urllib`, entirely in the standard library.

---

## Quick start

```sh
elgatoctl discover        # scan the network; prints name/model/address/mac
elgatoctl status          # every known light
elgatoctl alias set desk "Elgato Key Light F3DF"
elgatoctl set desk --on on --brightness 50 --temperature 5000k
elgatoctl set desk --on off    # or: elgatoctl off desk
elgatoctl set desk --on on lamp2      # ad-hoc group: several lights, one write
```

Targets accept any of: mDNS instance name, saved alias, MAC address,
`IP`, or `IP:port` (the last two reach paired lights even when mDNS is
blocked). Improving a light identifier with `--json` flag gets raw device
documents.

---

## Command reference

| Command | Purpose |
|---|---|
| `discover` | mDNS browse for `_elg._tcp` accessories |
| `status [PATTERN…]` | power / brightness / temperature per light (+ firmware, model) |
| `info PATTERN` | full `/elgato/accessory-info` document (`--json` for raw) |
| `on PATTERN…` / `off PATTERN…` | power control |
| `set [PATTERN…]` | `--on on/off`, `--brightness 0-100`, `--temperature KELVIN or M` |
| `set --color HUE:SAT` | Light Strip / color-capable devices (`hue` 0-360 & `saturation` 0-100) |
| `group PATTERN…` | show what a multi-target pattern matches (ad-hoc group) |
| `settings [PATTERN]` | show `/elgato/lights/settings` |
| `set-settings KEY=VALUE … [PATTERN]` | patch settings, e.g. `powerOnBehavior=2 powerOnBrightness=20 powerOnTemperature=230` |
| `name NEWNAME PATTERN` | rename the light — stored on-device, visible to every client (design doc §8.1) |
| `identify [PATTERN…]` | flash the matched lights |
| `firmware [PATTERN…]` | show firmware version / build |
| `reboot [PATTERN…]` | restart (`~15 s boot, double blink`) |
| `factory-reset [PATTERN]` | erase Wi-Fi credentials + settings (`--yes` required) |
| `alias set SHORTNAME IDENTIFIER` | save a short name; IDENTIFIER = name, MAC, IP or IP:port |
| `alias list` / `alias remove SHORTNAME` | manage saved aliases |

**Global flags:** `--timeout SEC` (mDNS window), `--interface IP`
(multihomed hosts), `--parallel/--sequential` (device fan-out threading,
default parallel), `--color/--no-color` (respects `NO_COLOR` otherwise),
`--json`, `--yes`.

### Groups

Grouping is stateless like Control Center's "linked lights": give one
command several targets (aliases, names, or patterns) and each device
receives the same write. `elgatoctl group show` previews a pattern's
membership before you tap it.

---

## Aliases / persistence

`~/.config/elgatoctl/named-lights.json` (honours `XDG_CONFIG_HOME`):

```json
{
  "names": {
    "3C:6A:9D:16:12:45": {
      "name": "desk",
      "ip": "192.168.1.11",
      "port": 9123,
      "mac": "3C:6A:9D:16:12:45",
      "model": "Elgato Key Light Air 20LAB9901"
    }
  }
}
```

Storage keys prefer the MAC so a DHCP address change does not orphan an
alias; the IP is refreshed on any successful contact.

`elgatoctl alias set` also accepts a bare `IP:port` for lights that are
currently invisible to mDNS (mirrors the manual-IP pairing feature of the
Windows app, design doc §9.3) — the identifier is probed with
`accessory-info` before it is saved.

---

## Linux notes

- **No root needed** — the socket binds an ephemeral UDP port and the HTTP
  calls are ordinary unprivileged traffic.
- **Firewall**: allow UDP 5353 in *and* out on the LAN interface; the
  discovery queries use the QU bit so devices answer by unicast.
- **AP routers**: enable Multicast / mDNS / IGMP snooping passthrough, and
  keep *Broadcast filtering* OFF (design doc §6.3).
- **Multi-homed hosts**: pass `--interface <local-IP>` to pin the egress.
- **Service discovery quirks**: if Avahi or a corporate mDNS filter blocks
  the network, IP-based targeting and `alias set IP:port` still work.
- **Key Light Neo / USB cables**: USB-attached lights are not enumerated by
  this CLI (no USB transport yet — see Roadmap).

### Auto switch-off on sleep/shutdown (systemd user hook)

Control Center 1.8 added "automatically switch off accessories"; on a
Linux user session the equivalent is:

`~/.config/systemd/user/elgatoctl-lights-off.service`

```ini
[Unit]
Description=Turn Elgato lights off before sleep/shutdown
DefaultDependencies=no
Before=sleep.target shutdown.target reboot.target halt.target

[Service]
Type=oneshot
ExecStart=/usr/bin/elgatoctl off

[Install]
WantedBy=sleep.target shutdown.target reboot.target halt.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now elgatoctl-lights-off.service
```

Lights stay on during screen lock/fast user switching only if you choose
to wire that yourself (the Windows app keeps them on during lock and fast
user switching, design doc §12.1).

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | CLI usage error (bad flag, no KEY=VALUE, refused destructive op) |
| 3 | no devices found / nothing matches |
| 4 | device error (unreachable, HTTP error, bad JSON) |
| 5 | write did not stick — device answered 200 without applying it
      (the documented *silent-rejection* quirk, design doc §7.2) |
| 130 | interrupted (Ctrl-C) |

---

## Testing

```sh
python3 -m unittest discover -s tests          # full suite, ~16 s, no hardware needed
python3 tests/fake_light.py --port 9123        # demo a protocol-faithful fake light
# in another shell:
python3 -m elgatoctl set 127.0.0.1:9123 --on on --brightness 25
```

`tests/fake_light.py` simulates a real panel's web server, including:

- 403 for any request carrying an `Origin` header (browser-CSRF rule) and a
  test asserting the client never sends one;
- partial-PUT merge semantics;
- out-of-range brightness accepted with HTTP 200 while the stored value does
  not change — which trips the client's write **verification**;
- `/elgato/identify`, `/elgato/restart`,
  `accessory-info` rename persistence.

Test layers: unit (conversions/parsing), wire (synthetic mDNS packet
replicating captured PTR/SRV/TXT/A answers), client (HTTP behaviour), and
CLI (argparse, exit codes, aliases, end-to-end writes against the fake).

---

## Limitations / roadmap

- **Provisioning** (joining a light to Wi-Fi) is out of scope; use the
  in-browser config page the device exposes while it is in setup mode at
  `http://192.168.62.1:9123/` (design doc §9.2), or any phone.
- **Transports**: mDNS + HTTP only; Key Light Neo over USB and Key Light
  Remote over BLE are not implemented.
- **Scenes** for Light Strip Pro are read-only (raw JSON via `info`);
  the `set` command covers power/brightness/temperature/hue/saturation.
- **Discovery scope**: IPv4 link-local segments only.
- Safety option for LANs with hostile neighbors: put lights on an
  isolated VLAN or firewall `TCP 9123` — the device API has no
  authentication at all (design doc §14).

---

## License

MIT. This project is an independent clean-room implementation based on
published protocol documentation; it is not affiliated with nor endorsed
by Elgato Systems/Corsair. "Elgato", "Key Light", "Light Strip" and
"Stream Deck" are trademarks of Elgato Systems GmbH.
