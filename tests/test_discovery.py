"""Tests for the pure-socket mDNS responder parser.

Builds a synthetic DNS-SD packet replicating the exact layout captured
from a real Key Light Air (see Elgato-Control-Center-System-Design.md
section 6.2) and asserts the parser correlates PTR/SRV/TXT/A correctly,
handles compression pointers, and ignores foreign services.
"""

import struct
import unittest

from elgatoctl import discovery


def write_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        out += struct.pack("B", len(raw)) + raw
    return out + b"\x00"


def build_response(
    instance="Elgato Key Light Air F3DF",
    host="elgato-key-light-air-f3df.local",
    ip="192.168.1.11",
    mac="3C:6A:9D:16:12:45",
    model="Elgato Key Light Air 20LAB9901",
    dt="200",
    port=9123,
    with_a_record=True,
):
    service = "_elg._tcp.local"
    record_count = 4 if with_a_record else 3

    packet = bytearray()
    # header: id, flags (qr=1 aa=1), 1 question, record_count answer records
    packet += struct.pack(">HHHHHH", 0x5337, 0x8400, 1, record_count, 0, 0)
    # question
    packet += write_name(service) + struct.pack(">HH", discovery.TYPE_PTR, 0x8001)

    # 1) PTR answer
    rdata = write_name("%s.%s" % (instance, service))
    packet += write_name(service) + struct.pack(">HHIH", discovery.TYPE_PTR, 1, 10, len(rdata)) + rdata

    # 2) SRV answer: priority weight port, then uncompressed target
    rdata = struct.pack(">HHH", 0, 0, port) + write_name(host)
    packet += write_name("%s.%s" % (instance, service)) + struct.pack(">HHIH", discovery.TYPE_SRV, 0x8001, 10, len(rdata)) + rdata

    # 3) TXT answer
    txt_rows = ["mf=Elgato", "dt=" + dt, "id=" + mac, "md=" + model, "pv=1.0"]
    rdata = b"".join(struct.pack("B", len(row)) + row.encode() for row in txt_rows)
    packet += write_name("%s.%s" % (instance, service)) + struct.pack(">HHIH", discovery.TYPE_TXT, 0x8001, 4500, len(rdata)) + rdata

    # 4) A answer for the host
    if with_a_record:
        rdata = bytes(int(part) for part in ip.split("."))
        packet += write_name(host) + struct.pack(">HHIH", discovery.TYPE_A, 0x8001, 10, len(rdata)) + rdata
    return bytes(packet)


class DiscoveryParsingTests(unittest.TestCase):
    def test_complete_packet(self):
        devices = discovery.parse_response(build_response(), responder_ip="10.0.0.5")
        self.assertEqual(len(devices), 1)
        device = next(iter(devices.values()))
        self.assertEqual(device.name, "Elgato Key Light Air F3DF")
        self.assertEqual(device.host, "elgato-key-light-air-f3df.local")
        self.assertEqual(device.ip, "192.168.1.11")  # from the A record
        self.assertEqual(device.port, 9123)
        self.assertEqual(device.mac, "3C:6A:9D:16:12:45")
        self.assertEqual(device.model, "Elgato Key Light Air 20LAB9901")
        self.assertEqual(device.device_type, 200)
        self.assertEqual(device.manufacturer, "Elgato")
        self.assertEqual(device.protocol_version, "1.0")

    def test_falls_back_to_responder_ip_without_a_record(self):
        devices = discovery.parse_response(
            build_response(with_a_record=False), responder_ip="10.0.0.7"
        )
        device = next(iter(devices.values()))
        self.assertEqual(device.ip, "10.0.0.7")
        self.assertEqual(device.responder_ip, "10.0.0.7")

    def test_foreign_service_ignored(self):
        devices = discovery.parse_response(build_response(), responder_ip="10.0.0.5")
        self.assertFalse(any("_hap" in key for key in devices))

    def test_truncated_packet_is_safe(self):
        pkt = build_response()
        devices = discovery.parse_response(pkt[:60], responder_ip="10.0.0.5")
        self.assertIsInstance(devices, dict)

    def test_query_encoding(self):
        query = discovery.build_query("_elg._tcp.local", discovery.TYPE_PTR)
        # uncompressed labels: "_elg" "_tcp" "local"
        self.assertIn(b"\x04_elg\x04_tcp\x05local\x00", query)
        # qtype=PTR (12), qclass 0x8001 = IN with the QU (unicast) bit set
        self.assertIn(struct.pack(">HH", discovery.TYPE_PTR, 0x8001), query)


if __name__ == "__main__":
    unittest.main()
