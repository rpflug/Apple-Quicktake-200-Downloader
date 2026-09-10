import struct
import unittest

from quicktake200 import app as qt


class PacketTests(unittest.TestCase):
    def test_picture_count_packet(self):
        self.assertEqual(
            qt._packet(qt.CMD_PICTURE_COUNT),
            bytes((0x10, 0x02, 0x00, 0x0B, 0x00, 0x00, 0x10, 0x03, 0x08)),
        )

    def test_dle_stuffing_and_checksum(self):
        packet = qt._packet(b"\x00\x02", b"\x10")
        self.assertEqual(packet.count(b"\x10\x10"), 1)
        self.assertEqual(packet[-1], qt._bcc(b"\x00\x02\x01\x00\x10"))

    def test_even_parity_bit(self):
        # Model the USA-28 patch: parity bit makes total one-bits even.
        for value in range(256):
            parity = value.bit_count() & 1
            self.assertEqual((value.bit_count() + parity) & 1, 0)


class FakePort:
    def __init__(self, response):
        self.response = bytearray(response)
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    def read_exact(self, size):
        data = bytes(self.response[:size])
        del self.response[:size]
        if len(data) != size:
            raise AssertionError("fake response exhausted")
        return data


class ResponseTests(unittest.TestCase):
    def test_receive_response(self):
        payload = b"1.0,QuickTake 200"
        raw = b"\x00\x09" + struct.pack("<H", len(payload)) + payload
        wire = bytes((qt.DLE, qt.STX)) + raw + bytes((qt.DLE, qt.ETX, qt._bcc(raw)))
        port = FakePort(wire)
        camera = qt.QuickTake200(port)
        self.assertEqual(camera._receive_response(), payload)
        self.assertEqual(port.writes, [bytes((qt.ACK,))])

    def test_bad_checksum_is_rejected(self):
        raw = b"\x00\x0b\x02\x00\x01\x00"
        wire = bytes((qt.DLE, qt.STX)) + raw + bytes((qt.DLE, qt.ETX, 0xFF))
        port = FakePort(wire)
        with self.assertRaises(qt.QuickTakeError):
            qt.QuickTake200(port)._receive_response()
        self.assertEqual(port.writes, [bytes((qt.NAK,))])


if __name__ == "__main__":
    unittest.main()
