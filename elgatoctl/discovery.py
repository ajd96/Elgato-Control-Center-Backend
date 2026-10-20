"""mDNS/DNS-SD discovery for Elgato lighting accessories.

Elgato Wi-Fi lights advertise the DNS-SD service ``_elg._tcp.local`` on
UDP port 5353 (multicast group 224.0.0.251) and expose an HTTP control
plane on TCP port 9123. The exact record layout is documented in
``Elgato-Control-Center-System-Design.md`` section 6.2.

This module implements just enough of RFC 1035 / RFC 6762 / RFC 6763 to
issue *legacy unicast* mDNS queries and parse device responses using the
standard library only. It does not require Avahi or python-zeroconf, so
it runs on any Linux distribution out of the box.

Limitations (documented, by design):

- IPv4 only (Elgato lights also answer with AAAA records, which are ignored).
- The responder must include SRV (+ ideally TXT/A) records in the same
  reply packet as the PTR record, which is standard DNS-SD behaviour.
"""

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_NAME = "_elg._tcp.local"

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
TYPE_AAAA = 28

RECV_BUFFER = 9016  # 64KB DNS payload cap per RFC; devices are far smaller


class DiscoveryError(Exception):
    """Raised when the local multicast socket cannot be set up."""


@dataclass
class DiscoveredDevice:
    """A single Elgato accessory seen on the network."""

    name: str                       # DNS-SD instance name, e.g. "Elgato Key Light Air F3DF"
    host: str                       # mDNS hostname, e.g. "elgato-key-light-air-f3df.local"
    ip: str                         # IPv4 address (falls back to responder IP)
    port: int                       # TCP control-plane port (always 9123 today)
    mac: str = ""                   # TXT "id" = Wi-Fi module MAC address
    model: str = ""                 # TXT "md", e.g. "Elgato Key Light Air 20LAB9901"
    device_type: Optional[int] = None   # TXT "dt", e.g. 200 = Key Light Air
    manufacturer: str = ""          # TXT "mf"
    protocol_version: str = ""      # TXT "pv"
    responder_ip: str = ""          # Source address of the mDNS reply packet


# --------------------------------------------------------------------------
# Wire-format encoding
# --------------------------------------------------------------------------

def _encode_name(name: str) -> bytes:
    """Encode a dotted DNS name into uncompressed label octets."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        encoded = label.encode("idna") if any(c > "z" or c < "0" and c not in "-_" for c in label) else label.encode("ascii")
        if len(encoded) > 63:
            raise ValueError("DNS label too long: %r" % label)
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def build_query(name: str, qtype: int, unicast_response: bool = True) -> bytes:
    """Build a DNS query packet for use on the mDNS wire.

    `unicast_response` sets the QU bit (top bit of the qclass field,
    RFC 6762 section 5.4) so the device replies immediately via unicast
    to our source port instead of multicasting into the network.
    """
    qclass = 0x8001 if unicast_response else 0x0001
    header = struct.pack(">HHHHHH", 0, 0x0000, 1, 0, 0, 0)  # id=0, no flags
    return header + _encode_name(name) + struct.pack(">HH", qtype, qclass)


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def _read_name(data: bytes, offset: int) -> Tuple[str, int]:
    """Read a (possibly compressed) domain name.

    Returns ``(name, offset_after_name_in_original_stream)`` where the
    trailing offset accounts for compression pointers per RFC 1035
    section 4.1.4.
    """
    labels = []
    end = None
    jumps = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated domain name")
        length = data[offset]
        if length == 0:
            offset += 1
            if end is None:
                end = offset
            return ".".join(label.decode("ascii", "replace") for label in labels), end
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(data):
                raise ValueError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if end is None:
                end = offset + 2
            jumps += 1
            if jumps > 32 or pointer >= len(data):
                raise ValueError("bad or looping compression pointer")
            offset = pointer
            continue
        if length & 0xC0:
            raise ValueError("reserved DNS label type 0x%02x" % length)
        if offset + 1 + length > len(data):
            raise ValueError("truncated DNS label")
        labels.append(data[offset + 1 : offset + 1 + length])
        offset += 1 + length


def _txt_to_dict(rdata: bytes) -> Dict[str, str]:
    """Decode a TXT record into a key/value mapping (RFC 6763 6.4)."""
    values: Dict[str, str] = {}
    pos = 0
    while pos < len(rdata):
        length = rdata[pos]
        pos += 1
        chunk = rdata[pos : pos + length]
        pos += length
        text = chunk.decode("ascii", "replace")
        if not text:
            continue
        if "=" in text:
            key, _, value = text.partition("=")
            values[key] = value
        else:
            values[text] = ""
    return values


def parse_response(data: bytes, responder_ip: str) -> Dict[str, DiscoveredDevice]:
    """Parse one mDNS packet, returning any Elgato devices it advertises."""
    if len(data) < 12:
        return {}
    try:
        qdcount, ancount, nscount, arcount = struct.unpack_from(">HHHH", data, 4)
    except struct.error:
        return {}

    offset = 12
    for _ in range(qdcount):
        _name, offset = _read_name(data, offset)
        offset += 4  # qtype + qclass
        if offset > len(data):
            return {}

    records: List[Tuple[str, str, object]] = []
    total_rr = ancount + nscount + arcount
    for _ in range(total_rr):
        try:
            rr_name, offset = _read_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack_from(">HHIH", data, offset)
            offset += 10
            rd_start = offset
            offset += rdlen
            if offset > len(data):
                break
        except (ValueError, struct.error):
            break

        name_key = rr_name.lower()
        try:
            if rtype == TYPE_PTR:
                target, _ = _read_name(data, rd_start)
                records.append(("PTR", name_key, target))
            elif rtype == TYPE_SRV and rdlen >= 7:
                _prio, _weight, port = struct.unpack_from(">HHH", data, rd_start)
                target, _ = _read_name(data, rd_start + 6)
                records.append(("SRV", name_key, (port, target)))
            elif rtype == TYPE_TXT:
                records.append(("TXT", name_key, _txt_to_dict(data[rd_start : rd_start + rdlen])))
            elif rtype == TYPE_A and rdlen == 4:
                records.append(("A", name_key, socket.inet_ntoa(data[rd_start : rd_start + 4])))
        except (ValueError, struct.error):
            continue

    return _extract_devices(records, responder_ip)


def _extract_devices(
    records: List[Tuple[str, str, object]], responder_ip: str
) -> Dict[str, DiscoveredDevice]:
    """Correlate PTR/SRV/TXT/A records into complete device records."""
    service_key = SERVICE_NAME.lower()
    devices: Dict[str, DiscoveredDevice] = {}

    ptrs = [value for rtype, key, value in records if rtype == "PTR" and key == service_key]
    for instance_full in ptrs:
        inst_key = instance_full.lower()
        srv = next(
            (value for rtype, key, value in records if rtype == "SRV" and key == inst_key),
            None,
        )
        if not srv:
            continue  # cannot control a device we have no port for
        port, host = srv
        if port <= 0:
            continue
        host_key = host.lower()

        txt = next(
            (value for rtype, key, value in records if rtype == "TXT" and key == inst_key),
            {},
        )
        ips = [
            value for rtype, key, value in records if rtype == "A" and key == host_key
        ]
        ip = ips[0] if ips else responder_ip

        # Display name: the instance label only (strip the "_elg._tcp.local"
        # service suffix from the PTR target's FQDN).
        suffix = "." + service_key
        display = (
            instance_full[: -len(suffix)]
            if instance_full.lower().endswith(suffix)
            else instance_full.rstrip(".")
        )

        device_type: Optional[int] = None
        raw_dt = txt.get("dt", "")
        if raw_dt.lstrip("-").isdigit():
            device_type = int(raw_dt)

        devices[inst_key] = DiscoveredDevice(
            name=display,
            host=host.rstrip("."),
            ip=ip,
            port=port,
            mac=txt.get("id", ""),
            model=txt.get("md", ""),
            device_type=device_type,
            manufacturer=txt.get("mf", ""),
            protocol_version=txt.get("pv", ""),
            responder_ip=responder_ip,
        )
    return devices


# --------------------------------------------------------------------------
# Discovery entry point
# --------------------------------------------------------------------------

def discover(timeout: float = 2.0, interface: Optional[str] = None) -> List[DiscoveredDevice]:
    """Browse ``_elg._tcp.local`` and return the devices that answer.

    Sends a small burst of legacy unicast mDNS queries (each device
    responds rate-limited to ~1 answer/second, so three probes inside the
    window give lossy networks a fair chance) and collects replies until
    `timeout` seconds have elapsed.
    """
    results: Dict[str, DiscoveredDevice] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        if interface:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
                )
            except OSError as err:
                raise DiscoveryError("bad --interface address %r: %s" % (interface, err))
        try:
            sock.bind(("0.0.0.0", 0))
        except OSError as err:
            raise DiscoveryError("cannot bind UDP socket: %s" % err)

        query = build_query(SERVICE_NAME, TYPE_PTR, unicast_response=True)
        deadline = time.monotonic() + timeout
        next_send = 0.0
        sends = 0
        max_sends = 3

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send and sends < max_sends + 1 and sends < max_sends:
                try:
                    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass  # e.g. network down for this interface; keep polling
                sends += 1
                next_send = now + 0.8

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.2, remaining))
            try:
                data, addr = sock.recvfrom(RECV_BUFFER)
            except socket.timeout:
                continue
            except OSError:
                continue  # ICMP errors on unconnected UDP, ignore per RFC 1122

            for key, device in parse_response(data, addr[0]).items():
                existing = results.get(key)
                if existing is None or device.ip != existing.ip:
                    results[key] = device
                else:
                    results[key] = device
    finally:
        sock.close()

    return sorted(results.values(), key=lambda d: d.name.lower())
