# Elgato Control Center (Windows) — System Design Document

| Field | Value |
|---|---|
| Product | Elgato Control Center for Windows (vendor: Elgato, a Corsair brand) |
| Current version analyzed | 1.9.0 (release 23 July 2026) — MSI builds for x64 and ARM64 |
| Document status | Technical summary compiled from vendor documentation and publicly documented protocol captures |
| Date | 2026-10-19 |
| Notes | Elgato does not publish internal source code. Sections marked **[Observed]** are derived from packet captures and reverse-engineered community/firmware documentation; sections marked **[Vendor]** come from Elgato help-center articles and release notes. |

---

## 1. Executive Summary

Elgato Control Center is a desktop system-tray resident application for Windows that discovers, provisions, controls, groups, and firmware-updates Elgato network lighting accessories (Key Light family, Ring Light, Light Strip family, Key Light Mini). Its architecture is a thin control-plane client over a local-area-network device API:

- **Discovery**: Bonjour/mDNS service browsing for `_elg._tcp` (Bonjour stack installed with the app on Windows).
- **Control**: plaintext HTTP/1.1 JSON requests to TCP port 9123 on each accessory's embedded web server.
- **Provisioning**: a pairing wizard that joins accessories to the user's Wi-Fi network (the accessory briefly exposes its own configuration endpoint / AP), including Windows-only manual IP pairing.
- **Integration**: real-time state shared with Stream Deck plugins and mobile/desktop Control Center clients; devices are polled at a fast cadence so all clients converge on the same state.

The design is intentionally server-less: there is no Elgato cloud dependency for control. Every operation is LAN-local, which yields low latency but also means the security posture depends entirely on network trust.

---

## 2. Product Overview and Goals

**Primary goals** (from Windows release notes):

1. Turn lights on/off; adjust brightness (0–100 %) and color temperature (2900–7000 K).
2. Link (group) multiple lights for synchronized control.
3. Pair Wi-Fi accessories to a network from a Windows PC.
4. Update accessory firmware with staged, resumable delivery.
5. Expose accessory state/controls to Stream Deck and other Elgato software.
6. Manage LED-panel-level settings (power-on behavior, transition durations, indicators) and battery behavior (Key Light Mini).

**Out of scope of the app**: audio (Wave Link), capture (Elgato Studio), cameras (Camera Hub) — Control Center is the lighting vertical of Elgato's software suite and maintained as a separate product with separate release trains for Windows, macOS, iOS, and Android.

---

## 3. Supported Device Matrix

| Device | Connectivity | Discovery | Light model | Notable capabilities |
|---|---|---|---|---|
| Key Light (MK.1, legacy) | Wi-Fi 2.4 GHz (802.11 b/g/n, WPA/WPA2) | `_elg._tcp` | white, `temperature` 143–344 mireds | 2800 lm, 45 W |
| Key Light Air | Wi-Fi 2.4 GHz | `_elg._tcp` | white | 2100 lm (1100 lm first gen) |
| Key Light Mini | Wi-Fi + onboard controls | `_elg._tcp` | white | battery info/energy-saving settings, bypass mode |
| Key Light MK.2 | Wi-Fi 2.4/5 GHz + USB + Bluetooth (remote) | `_elg._tcp` | white | Light Remote BLE pairing, auto-brightness |
| Key Light Air MK.2 (1.9) | Wi-Fi + USB-C | `_elg._tcp` | white | touch controls |
| Key Light Neo (1.6+) | **USB** (+ Wi-Fi) | USB enumeration + `_elg._tcp` | white | monitor-mount panel; USB control after wake |
| Ring Light | Wi-Fi | `_elg._tcp` | white, multi-element `lights[]` | panel-level API writes `lights[0]` |
| Light Strip (1.1.4+) | Wi-Fi | `_elg._tcp` | **color** (`hue`, `saturation`) | RGBWW, 2000 lm |
| Eve Light Strip (1.1+) | Wi-Fi (HomeKit-native device) | `_elg._tcp` | color | 1800 lm |
| Light Strip Pro (1.7+) | Wi-Fi | `_elg._tcp` | color + **scenes** | 120 individually addressable LEDs |

**[Observed]** Each device publishes its model in the mDNS TXT record (`md=Elgato Key Light Air 20LAB9901`, device type code in `dt=`), which Control Center uses to select the correct UI (white vs. color vs. scene-capable) and settings schema.

---

## 4. System Requirements, Packaging, and Distribution

**[Vendor]**

| Item | Detail |
|---|---|
| OS | Windows 10 (64-bit) or later; current packages validated for Windows 10 22H2+ / Windows Server 2022+ |
| Architectures | x64 and ARM64 MSIs (ARM64 fixes landed for Copilot+ devices in 1.8.2) |
| Runtime | .NET Framework 4.8/4.8.1 (MSI LaunchCondition `WIX_IS_NETFRAMEWORK_481_OR_LATER_INSTALLED`) — implies a managed desktop application (WPF/WinForms family) |
| Network | Wi-Fi capable PC required for pairing; any LAN-connected PC for control |
| Wi-Fi environment | 802.11 b/g/n; WPA/WPA2 encryption; multicast/mDNS allowed by AP |
| Install path | `C:\Program Files\Elgato\ControlCenter\ControlCenter.exe` |
| Languages | Chinese, Danish, Dutch, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish, Swedish |
| Dark mode | Automatic partial theming from Windows color settings (1.6+) |
| Power behavior | Windows **efficiency mode** when backgrounded (1.6+) to reduce battery/power draw |

**Distribution channels:**

1. Direct MSI from Elgato's CDN, e.g. `https://edge.elgato.com/egc/windows/eccw/1.9/Elgato.ControlCenter_1.9.0.818_x64.msi` (SHA-256 published by Chocolatey).
2. Chocolatey package (`elgato-controlcenter`) which wraps the MSI; note the documented vendor-MSI quirk: the .NET 4.8.1 LaunchCondition is never satisfied by the MSI's own property wiring, so silent installs must pass `WIX_IS_NETFRAMEWORK_481_OR_LATER_INSTALLED=1` explicitly.
3. Also surfaced as an "Add to Stream Deck" download on elgato.com.

---

## 5. High-Level Architecture

```
                       Windows PC
 ┌──────────────────────────────────────────────────────────┐
 │  ControlCenter.exe                                       │
 │                                                          │
 │  ┌────────────┐   ┌──────────────┐   ┌────────────────┐  │
 │  │ Tray shell │◄─►│ Main window  │◄─►│ Settings/prefs │  │
 │  │ (flyout UI)│   │ (device grid)│   │ + Persistence  │  │
 │  └─────┬──────┘   └──────┬───────┘   └────────┬───────┘  │
 │        │                 │                    │          │
 │  ┌─────▼─────────────────▼────────────────────▼───────┐  │
 │  │              Device Manager core                   │  │
 │  │  - inventory (discovered + saved devices)          │  │
 │  │  - groups (linked lights)                          │  │
 │  │  - state cache / poll scheduler                    │  │
 │  │  - command serialization (write coalescing)        │  │
 │  └──┬─────────────┬──────────────┬─────────────┬──────┘  │
 │     │             │              │             │         │
 │  ┌──▼────────┐ ┌──▼───────────┐ ┌▼──────────┐ ┌▼───────┐ │
 │  │ Discovery │ │ HTTP client  │ │ Provisio- │ │ FW     │ │
 │  │ mDNS/ Bon-│ │ (per-device  │ │ ning/Pair │ │ Update │ │
 │  │ jour engine│ │  sessions)  │ │ wizard    │ │ engine │ │
 │  └──┬────────┘ └──┬───────────┘ └┬──────────┘ └┬───────┘ │
 │     │             │              │             │         │
 │  ┌──▼─────────────▼──────────────▼─────────────▼───────┐ │
 │  │        Windows integration layer                   │ │
 │  │  Bonjour stack · Wi-Fi APIs · Location permission  │ │
 │  │  USB HID/serial (Neo) · BLE (Light Remote, MK.2)   │ │
 │  │  Power events (shutdown/sleep/logoff hooks)        │ │
 │  │  IPC/state sharing with Stream Deck plugin         │ │
 │  └─────────────────────────────────────────────────────┘ │
 └──────────────────────────────────────────────────────────┘
            │ mDNS 5353/UDP          │ HTTP TCP 9123        │ USB / BLE
            ▼                        ▼                      ▼
 ┌──────────────────────────────────────────────┐   ┌──────────────┐
 │  LAN (Wi-Fi / same L2 segment)               │   │ USB lighting │
 │  ┌─────────────┐ ┌─────────────┐             │   │ accessories  │
 │  │ Key Light   │ │ Light Strip │  embedded   │   └──────────────┘
 │  │ :9123 (mDNS │ │ :9123       │  HTTP/1.1   │   ┌──────────────┐
 │  │  _elg._tcp) │ │             │  JSON API   │◄──│ BLE remotes  │
 │  └─────────────┘ └─────────────┘             │   └──────────────┘
 └──────────────────────────────────────────────┘
```

### 5.1 Core components **[Observed/Inferred]**

| Component | Responsibility |
|---|---|
| **Tray shell** | Persistent notification-area icon; flyout quick panel (position-aware for all taskbar locations); Ctrl+click toggles all lights; left-click opens the quick window |
| **Discovery engine** | mDNS browsing (`_elg._tcp.local`), deduplication by MAC/id, health transitions (on/off network) |
| **Device manager** | Inventory of paired devices (stored IP addresses), grouping, display-name sync, firmware inventory |
| **Poll scheduler** | Periodic GET of device state so UI, Stream Deck, and mobile clients converge (observed cadences in §7.3) |
| **Provisioning wizard** | Wi-Fi scan/band selection, credential upload to accessory, reconnect handling, manual IP entry |
| **Firmware engine** | Update availability detection (blue badge), staged binary transfer, certificate integrity checks (an expired-certificate failure was fixed in 1.9) |
| **Stream Deck bridge** | Exposes light state/values to Stream Deck plugin actions (connection hardened in 1.1.2; enhanced integration 1.1.1/1.0) |
| **Power-event handler** | "Automatically switch off accessories" on shutdown/sleep/hibernate/logoff (1.8+) |

---

## 6. Network and Communication Design

### 6.1 Protocol stack **[Vendor + Observed]**

| Layer | Protocol | Detail |
|---|---|---|
| Discovery | mDNS/DNS-SD (Bonjour) | Service `_elg._tcp.local`; UDP 5353, multicast 224.0.0.251; Apple `_mfi-config._tcp` also advertised during provisioning |
| Transport (control) | TCP | Device listens on **port 9123** |
| Application (control) | HTTP/1.1 | **No TLS.** JSON bodies (`application/json`); raw bytes for firmware data |
| Payload | JSON | Register-style document (see §8) |

Key behaviors:

- Devices **reject any request carrying an `Origin` header with HTTP 403** — a browser-based CSRF-style protection (web pages cannot command the light).
- After initial pairing, **Control Center stores the device IP** and talks to it directly; mDNS is used for re-discovery/verification rather than every control call.
- EpocCam uses a separate service string `_egstreamwrap._tcp` (not part of lighting control but shipped in the same ecosystem).

### 6.2 Discovery record format **[Observed]**

Example DNS-SD response for a Key Light Air (as returned on `_elg._tcp.local`):

```
PTR  _elg._tcp.local → Elgato\032Key\032Light\032Air\032F3DF._elg._tcp.local
SRV  0 0 9123 elgato-key-light-air-f3df.local          ; priority weight port target
TXT  "mf=Elgato" "dt=200" "id=3C:6A:9D:16:12:45"
     "md=Elgato Key Light Air 20LAB9901" "pv=1.0"
A    elgato-key-light-air-f3df.local → 192.168.1.11
AAAA → fe80::3e6a:9dff:fe16:1245                       ; link-local
```

| TXT key | Meaning |
|---|---|
| `mf` | Manufacturer (Elgato) |
| `dt` | Device-type code (e.g. 200 = Key Light Air; Light Strip etc. differ) |
| `id` | MAC address of the Wi-Fi module (used to match accessories during pairing; Windows flow asks users to read the MAC off the device) |
| `md` | Model string incl. internal board designation (e.g. 20LAB9901) |
| `pv` | Protocol version (1.0) |

macOS/Windows verification command: `dns-sd -B _elg._tcp` (also used by Elgato support documentation), or `dig -p 5353 PTR _elg._tcp.local @224.0.0.251`.

### 6.3 Environmental requirements **[Vendor]**

- Router/AP must permit multicast; Elgato lists feature names to check: Multicast, mDNS, Bonjour, IGMP snooping, RIP, Broadcast. **Broadcast filtering must be disabled.**
- Client and accessory must share an L2 segment (or routed path allowing unicast HTTP to 9123).
- Firewalls must allow mDNS for `_elg._tcp`; Control Center 1.9 warns when **mDNS is disabled by system policy** to explain empty discovery results.
- Windows 11 24H2 Wi-Fi privacy changes: the app needs **Location permission** (Settings → Privacy & security → Location; Location services ON + per-app access for Elgato Control Center) before it can enumerate nearby Wi-Fi networks for pairing.

---

## 7. State Model and Polling Design

### 7.1 Device-side state documents **[Observed]**

`GET /elgato/lights`:

```json
{
  "numberOfLights": 1,
  "lights": [ { "on": 1, "brightness": 50, "temperature": 143 } ]
}
```

`GET /elgato/lights/settings`:

```json
{
  "powerOnBehavior": 1,
  "powerOnBrightness": 20,
  "powerOnTemperature": 230,
  "switchOnDurationMs": 100,
  "switchOffDurationMs": 300,
  "colorChangeDurationMs": 100,
  "battery": {
    "bypass": 0,
    "energySaving": {
      "enable": 0,
      "minimumBatteryLevel": 15.00,
      "disableWifi": 0,
      "adjustBrightness": { "enable": 0, "brightness": 10.00 }
    }
  }
}
```

`powerOnBehavior`: `1` = restore last state, `2` = use `powerOnBrightness`/`powerOnTemperature`. `switchOnDurationMs`/`switchOffDurationMs` = fade in/out; `colorChangeDurationMs` = ramp time for brightness/temperature changes. Battery object applies to Key Light Mini (bypass = run from USB-C, ignore battery).

Color-capable devices use `hue` (0–360, decimal) and `saturation` (0–100) instead of `temperature`; scene-capable strips add a `scene` array. A `durationMs` field allows per-command transition times.

### 7.2 Value semantics

| Field | Range | Notes |
|---|---|---|
| `on` | 0 / 1 | Off/On |
| `brightness` | 0–100 | Percent. UI clamps to 3–100; API accepts 0 (acts as off without changing `on`) |
| `temperature` | 143–344 | **Mireds**: `mireds = 1,000,000 / Kelvin`. 143 ≈ 7000 K (cool), 344 ≈ 2900 K (warm). Larger = warmer |
| `hue` | 0–360 | Color models |
| `saturation` | 0–100 | Color models |

Conversion table (established):

| Kelvin | Mireds | Character |
|---|---|---|
| 2900 | 344 | Warm |
| 3900 | 256 | Warm white |
| 4950 | 202 | Neutral |
| 5950 | 168 | Cool white |
| 7000 | 143 | Daylight |

**Device error-tolerance quirks [Observed]:** out-of-range `temperature` (<143 or >344) is applied at the clamp boundary but the stored GET value does not change; out-of-range `brightness` (e.g. 110) returns **HTTP 200** and silently keeps the previous value — the firmware accepts writes without hard validation. Control Center therefore performs client-side clamping.

### 7.3 Polling cadence **[Observed — packet capture of Control Center traffic]**

| Request | Frequency | Purpose |
|---|---|---|
| `GET /elgato/accessory-info` | once at startup, then ~10 s | identity + freshness |
| `GET /elgato/lights/settings` | once at startup, then ~5 s | settings sync |
| `GET /elgato/lights` | ~1 s | live state for UI dial/slider and Stream Deck |

This dense polling is why Control Center traffic ("DNS queries for `_elg._tcp.local` / `_hap._tcp.local`") is visible on networks; Elgato specifically reduced network packet flooding in 1.7 after user reports.

### 7.4 Grouping semantics

- Multiple lights can be **linked**; the UI then issues the same payload to each member. Panel-level APIs of multi-element devices (Ring Light) are addressed as `lights[0]`; Control Center behaves the same.
- Write-then-verify: clients PUT the full state object (partial PUTs are legal — unspecified fields persist), then polling confidence re-syncs the cache.

---

## 8. Device HTTP API Reference (v1.0, port 9123)

**[Observed — reverse-engineered and mirrored by community firmware projects]**

| Method | Path | Function |
|---|---|---|
| GET | `/elgato/accessory-info` | identity, firmware, Wi-Fi state |
| PUT | `/elgato/accessory-info` | set `displayName` (accepted from firmware build 188+; persists on device so all clients see renames) |
| GET/PUT | `/elgato/lights` | light state |
| GET/PUT | `/elgato/lights/settings` | stored settings (power-on behavior, fades, battery) |
| GET | `/elgato/battery-info` | battery state (Key Light Mini) |
| POST | `/elgato/identify` | flash the light (identification) |
| PUT | `/elgato/wifi-info` | encrypted Wi-Fi credentials upload (GET returns 404) |
| PUT | `/elgato/firmware-update/prepare` | begin update |
| PUT | `/elgato/firmware-update/data` | firmware chunk (202 Accepted while more chunks wanted) |
| POST | `/elgato/firmware-update/execute` | switch boot slot + reboot |
| POST | `/elgato/restart` | reboot |
| POST | `/elgato/factory-reset` | erase settings + reboot |
| PUT | `/elgato/ble/pairing` | begin remote-control pairing (other models) |
| POST | `/elgato/uart` | undocumented maintenance/AT command channel (present on some Realtek-based hardware; treat as privileged) |

### 8.1 `/elgato/accessory-info` example

```json
{
  "productName": "Elgato Key Light Mini",
  "hardwareBoardType": 202,
  "hardwareRevision": 0.1,
  "macAddress": "00:00:00:00:00:00",
  "firmwareBuildNumber": 240,
  "firmwareVersion": "1.0.4",
  "serialNumber": "XXXXXXXXXXXX",
  "displayName": "",
  "features": ["lights"],
  "wifi-info": { "ssid": "MyNetwork", "frequencyMHz": 5000, "rssi": -25 },
  "power-info": { "mode": 2, "maxBrightness": 100 },
  "bt-info":  { "paired": 1 },
  "ledCount": 120
}
```

- `features` acts as a capability registry — a client fetches `accessory-info`, sees `features: ["lights"]`, and knows to use `/elgato/lights` (+ `/elgato/lights/settings`). This is the app's de-facto capability negotiation. `hardwareRevision` exists on devices but is ignored by the Control Center data model.
- `wifi-info.rssi` powers Control Center's **Wi-Fi Signal Strength indicator**; `frequencyMHz` distinguishes 2.4/5 GHz (used by the "How to find Wi-Fi band" guidance and band selection in pairing).
- Example board type codes (observed): 53 = Key Light MK.1, 200 = Key Light Air, 202 = Key Light Mini.

### 8.2 Status codes

| Code | Meaning |
|---|---|
| 200 | success |
| 202 | firmware chunk accepted, send more |
| 400 | malformed request / rejected firmware |
| 403 | request carried `Origin` header (browser hardening) |
| 404 | unknown path or method; also GET `/elgato/wifi-info` |
| 408 | timeout, may retry |
| 503 | service unavailable (e.g. during boot) |

### 8.3 Known API gaps (vs. app behavior) **[Observed]**

- Flash-to-identify used by Control Center during pairing relies on provisioning-mode behavior not fully exposed by the production HTTP API.
- No authenticated sessions; the API is designed for a trusted LAN.

---

## 9. Pairing and Provisioning Workflows (Windows)

### 9.1 Standard in-app wizard (Wi-Fi PC) **[Vendor]**

1. Click **+** / *Add accessory*; accessory boots into provisioning mode (~15 s boot; 3-blink pattern = join failure).
2. Control Center enumerates nearby Wi-Fi — requires Location permission on Windows 11 24H2+ (Section 6.3).
3. Only networks **compatible with the accessory** are offered (e.g. 2.4 GHz-only for MK.1-era lights); band can be forced: Automatic (2.4 GHz default), 2.4 GHz, 5 GHz (MK.2).
4. Credentials are transmitted to the accessory; the device attempts to join and reappears under `_elg._tcp`.
5. "Remember my network" — when adding further accessories, Control Center auto-selects the previously used SSID (1.6+).
6. Identify button flashes the candidate light before finalizing pairing.

Details enforced: SSID must match exactly (case/spaces), WPA/WPA2.

### 9.2 Manual pair via browser (Windows-only documented path) **[Vendor]**

For lights that keep failing discovery: connect the PC directly to the accessory's provisioning radio, then open `http://192.168.62.1:9123/` — the light serves a Wi-Fi-details page (SSID, password, security). On save the device restarts onto the network. Verification: `dns-sd -B _elg._tcp`.

### 9.3 Manual pair via IP address (Windows-only in-app) **[Vendor]**

If mDNS is blocked (e.g. corporate/managed networks), Control Center allows typing the accessory IP directly to register it in the inventory; discovery then operates without multicast.

### 9.4 USB provisioning/control (Key Light Neo, MK.2-era lights)

Neo is controlled over USB (laptop/dock power) — Wi-Fi optional. Engineering notes across releases: USB control restored after standby wake (1.8.2), and USB-connected lights now report status changes in real time (1.9); a fix ensured Windows could sleep normally with a Neo attached over USB (1.9). Key Light MK.2 additionally pairs **Key Light Remote** over Bluetooth, optionally before the light has ever joined Wi-Fi (firmware 1.0.4/199, 1.8/1.8.2 era).

### 9.5 Post-pairing behavior

Control Center stores the accessory IP; renames are written through `PUT /elgato/accessory-info` so the name lives **on the device** and propagates to macOS/iOS/Android/other PCs (firmware ≥ 1.0.3-192). Lights keep their network config across power cycles.

---

## 10. Firmware Update Subsystem

**[Vendor flow + Observed transport]**

1. Control Center compares `firmwareBuildNumber`/`firmwareVersion` with Elgato's update catalog and shows a **blue badge** on updatable devices (1.1.1+) plus optional recommended-update notifications.
2. Update data is streamed as raw chunks to `/elgato/firmware-update/data`, receiving `202` per accepted chunk and `400` on rejection — a two-phase bootloader (prepare → data → execute) with A/B boot-slot switch on `execute` and a final restart.
3. Reliability handling: retries under poor Wi-Fi (1.1.4); certificate-expiry check failures fixed (1.9); the device screen is kept awake during updates on newer builds to prevent interruption.
4. Historic firmware milestones: 1.0.3 (build 192) introduced on-device `displayName` persistence; build 199 (fw 1.0.4) enabled Key Light Remote pairing before Wi-Fi join on MK.2.

A failed update leaves the previous firmware slot bootable, which is why the flow re-verifies accessory-info after each restart.

---

## 11. Integrations

### 11.1 Stream Deck **[Vendor]**

- Stream Deck software ≥ 4.0 + Control Center ≥ 1.0 work together; the Control Center app runs resident in tray precisely to expose state to the Stream Deck plugin.
- Plugin actions: power, brightness (keys/dials), color temperature; grouping respected. Security of the app↔plugin channel was hardened (1.1.2) and reporting correctness improved across 1.1.1–1.9 (e.g. real-time USB status changes).
- Same integration powers "one-touch" lighting from Stream DeckNeo/Stream Deck Mobile surfaces.

### 11.2 Other Elgato apps

- Control Center state is also consumed by Elgato's iOS/Android apps; the HTTP API + on-device settings are the shared source of truth, so any client (including third-party tools: GEM, keylightd, GNOME extensions, ESP32 controllers) can interoperate on the same network.

---

## 12. Persistence and Local Settings (app side)

**[Vendor, where stated]**

| Data | Location/behavior |
|---|---|
| Paired devices (IP, id, model, group membership) | local app storage; re-validated against mDNS/service cache |
| Preferences (tray behavior, auto switch-off, update channel) | Preferences dialog (1.8 adds "Automatically switch off accessories") |
| Window geometry | remembers position; multi-monitor and non-bottom taskbars supported; first-run placement and window open/close performance fixes in 1.8.2 |
| Logs | ring-buffer sized to a few MB (reduced from ~hundreds of MB in 1.1.2) |
| Device naming | stored **on the accessory**, not per-PC |

### 12.1 Automatic switch-off semantics **[Vendor 1.8]**

Enabled in Preferences → *Automatically switch off accessories* (off by default). Lights turn off on: shutdown, sleep, hibernation, logoff. Lights deliberately stay on during: workstation **lock**, **fast user switching** (so someone else logging in doesn't kill your lighting scene). Overridden for hidden accessories.

---

## 13. Windows Platform Integration Details

| Concern | Behavior |
|---|---|
| Process model | Single tray-resident executable (`ControlCenter.exe`); main window is optional; app continues running tray-only on close |
| Efficiency mode | Background execution switches into Windows efficiency mode (1.6+) |
| Wi-Fi enumeration | Uses Windows WLAN service; needs Location grant on 24H2+ (Section 6.3); Wi-Fi adapters hot-plug detection without app restart (1.7) |
| mDNS policy detection | Detects/flags mDNS disabled by system policy (1.9) — important on managed networks |
| ARM64 | Native ARM64 MSI; ARM64 Wi-Fi discovery fixed in 1.8.2 under Copilot+ |
| Installer | MSI with .NET Framework 4.8.1 LaunchCondition (Chocolatey workaround documented in §4) |
| Sleep/idle hygiene | 1.9 fix so attached USB Neo doesn't block Windows sleep |

---

## 14. Security Analysis

**Strengths**
- Rejects browser-origin requests (403 on `Origin` header) — prevents web pages from driving lights cross-origin.
- Wi-Fi credential provisioning uses `/elgato/wifi-info` PUT with encrypted payloads; a configuration page is only reachable in provisioning mode (light's own network, `192.168.62.1`).
- No cloud control plane; LAN-scoped attack surface.

**Weaknesses / accepted risks** **[Observed]**
- Control plane is plaintext HTTP/1.1 on 9123 with **no authentication** — any host on the segment can read state and write commands. Mitigation: segment/ACL the LAN; the vendor defers to network trust (Elgato explicitly instructs allowing mDNS multicast for operation).
- The undocumented `POST /elgato/uart` AT-command endpoint constitutes a privileged maintenance channel on affected hardware; network exposure extends to it.
- Silent acceptance semantics (200 + unchanged state on invalid brightness) mean automation tooling must verify writes.
- Community capture of Control Center traffic has flagged sustained mDNS queries (`_elg._tcp.local`, `_hap._tcp.local`) as chatty behavior; elgato_arcsane (Elgato) confirmed discovery traffic is expected, with 1.7 addressing packet flooding.

---

## 15. Release History (Windows) and Genesis of Features

| Version | Date | Highlights |
|---|---|---|
| 1.0 | initial | Key Light control: power/temp/brightness, grouping, firmware update, Stream Deck pairing |
| 1.0.1 | — | 13 languages; reveal Wi-Fi password; identify-before-pair; Ctrl+tray-click all lights; non-bottom taskbar flyouts |
| 1.1 | — | **Eve Light Strip** support; Key Light fw 1.0.3-192 (on-device rename); update notifications |
| 1.1.1 | — | blue firmware badge; window placement fix; DPI scaling of release notes |
| 1.1.2 | — | harden Stream Deck link; shrank logs from ~100s MB → few MB |
| 1.1.3 | — | perf/bug fixes |
| 1.1.4 | — | **Light Strip**; firmware-update reliability on unstable Wi-Fi |
| 1.2 | — | **Key Light Mini** (battery settings, bypass mode) |
| 1.3 / 1.3.1 / 1.3.2 | — | platform maintenance windows |
| 1.4 / 1.4.1 / 1.4.2 | — | maintenance |
| 1.5 | 4 Jun 2024* | **Key Light MK.2** (5 GHz, Light Remote BLE, auto-brightness fixes) |
| 1.6 | 18 Apr 2024 | **Key Light Neo** (USB); dark mode; efficiency mode; compatible-network filtering; band picker; remember-my-network |
| 1.7 | 4 Jun 2024 | **Light Strip Pro**; Windows 11 2024 update + Wi-Fi privacy readiness; reduced discovery packet flooding; reconnect improvements; hot-plug Wi-Fi adapters; Mini bypass battery-notification fix |
| 1.8 | 2025 | auto switch-off on shutdown/sleep/logoff; new icon; MK.2 fw 1.0.4-199 (pre-Wi-Fi remote pairing) |
| 1.8.2 | 6 Nov 2025 | new desktop icon; crash fixes (post-update, device loss mid-settings); Neo USB after wake; first-run/window perf; **ARM64/Copilot+ Wi-Fi discovery fix** |
| 1.9 | 23 Jul 2026 | **Key Light Air MK.2**; real-time USB status; faster first connection; named setup status messages; LED-panel layout improvements; **mDNS-disabled warning**; Windows sleep fix with USB Neo; expired-certificate update failure fix |

\* Elgato's per-version pages occasionally show updated correction dates; the table reflects release notes order (1.5→1.6→1.7 numbering with 2024 dates as published).

---

## 16. Reference Configuration (Router/Firewall Allowlist)

```text
ALLOW UDP 5353  ff02::fb / 224.0.0.251      # mDNS (Bonjour)
ALLOW service  _elg._tcp                    # Elgato lighting discovery
# AP features: enable Multicast / mDNS / IGMP-snooping passthrough;
# disable Broadcast filtering
ALLOW TCP 9123  client → accessory IP       # control plane (HTTP/JSON)
BLOCK TCP 9123  non-client VLANs            # recommended containment, not vendor default
```

---

## 17. Pointers for Programmatic Interop (commonly implemented)

1. Discover: browse `_elg._tcp.local`; parse `id` (MAC) and `md` (model). Fallback: scan for open TCP 9123 on the LAN.
2. Read identity: `GET http://<ip>:9123/elgato/accessory-info` (requires fw ≥ 1.0.3; older firmware 404s).
3. Read/patch state: `GET/PUT /elgato/lights`; PUT accepts partial objects; returns new state in the same shape.
4. Never send an `Origin` header; do not hard-code `content-length` in hand-built requests (a known postman/capture artifact causing 400s).
5. Kelvin ⇄ mireds: `mireds = 1000000 / Kelvin`, clamp to [143, 344] before sending (2800 K→357 mireds is out of range; 2900 K is the floor).
6. Re-verify after explicit clamp writes — device replies 200 even for rejected values.
7. Reserve IPs via DHCP if controlling from non-mDNS stacks (mobile apps re-discover by mDNS; static integrations do not).

---

## 18. References

**Vendor (Elgato Help Center)**
- Control Center software section (article index) — help.elgato.com/hc/en-us/sections/360004115951
- Release Notes (Windows) — article 360028242831 (1.0–1.2 era)
- Control Center 1.9 Release Notes (Windows) — article 49081616343185
- Control Center 1.8.2 Release Notes (Windows) — article 40694391691793
- Control Center 1.7 Release Notes (Windows) — article 29957845668237
- Control Center 1.6 Release Notes (Windows) — article 26005473904781
- What Communication Protocol Is Used by Elgato Wi-Fi Products? — article 360060048331
- mDNS Service Strings for Elgato Devices — article 4413403384845
- Manually Pair Wi-Fi Product (Windows Only) — article 4413643069197
- Manually pair via IP Address (Windows Only) — article 5960047523085
- Enable location permissions on Windows for Wi-Fi access — article 30743844830989
- Automatically Switch Off Accessories — article 30939597720337
- Wi-Fi Signal Strength — article 5939000239245; Light Firmware Update — article 5938558858125; Key Light MK.I Quick Start — article 360028244011

**Community / reverse-engineering (protocol ground truth)**
- API Handyman — "Hacking and reviewing Elgato Key Light API with Postman" (2022): capture methodology, `/lights` semantics, clamp/silent-error findings
- elgato-key-light-mini-firmware-re (GitHub) — `docs/03-http-api.md`: full endpoint map, status codes, Origin-403 rule, firmware A/B slots, `hardwareRevision` note
- keylightd docs (jmylchreest) — mired table and conversion algorithm
- ioBroker adapter request #704 — packet-capture polling cadences (10 s / 5 s / 1 s), temperature stepping observations
- briandorey.com ESP32 Key Light remote — REST payloads
- grantcarthew/elgato, inorganic-software/keylight, danielhe4rt/elgato-control-center — client implementations and Light Strip payload differences
- Harness Automation GEM driver — integration constraints (IP pinning, fw ≥ 1.0.3 accessory-info 404, `lights[0]` behavior)

**Packaging**
- Chocolatey community package `elgato-controlcenter` 1.9.0 — MSI URL, SHA-256, .NET LaunchCondition workaround, install path
- Elgato downloads page — elgato.com/us/en/s/downloads
