from __future__ import annotations

import struct
import unittest

from gen4.pk4 import (
    DATA_SIZE,
    PARTY_SIZE,
    _BLOCK_OFFSETS,
    parse_pk4,
    scan_pk4,
)


def encrypt_words(data: bytes, seed: int) -> bytes:
    out = bytearray(data)
    state = seed & 0xFFFFFFFF
    for i in range(0, len(out), 2):
        state = (state * 1103515245 + 24691) & 0xFFFFFFFF
        key = (state >> 16) & 0xFFFF
        word = struct.unpack_from("<H", out, i)[0] ^ key
        struct.pack_into("<H", out, i, word)
    return bytes(out)


def make_pk4() -> bytes:
    pid = 1  # shiny with OT ID 0, nature Lonely
    logical_a = bytearray(0x20)
    logical_b = bytearray(0x20)
    logical_c = bytearray(0x20)
    logical_d = bytearray(0x20)

    struct.pack_into("<HHII", logical_a, 0, 152, 4, 0, 12345)
    logical_a[0x0C] = 70
    logical_a[0x0D] = 65
    logical_a[0x0F] = 2
    logical_a[0x10:0x16] = bytes([1, 2, 3, 4, 5, 6])

    struct.pack_into("<4H", logical_b, 0, 33, 45, 0, 0)
    ivs = (31, 30, 29, 28, 27, 26)
    iv_word = (
        ivs[0]
        | (ivs[1] << 5)
        | (ivs[2] << 10)
        | (ivs[3] << 15)
        | (ivs[4] << 20)
        | (ivs[5] << 25)
    )
    struct.pack_into("<I", logical_b, 0x10, iv_word)
    logical_b[0x18] = (1 << 1) | (0 << 3)
    struct.pack_into("<HH", logical_b, 0x1C, 0, 126)

    shuffle = (pid & 0x3E000) >> 13
    offsets = _BLOCK_OFFSETS[shuffle]
    plain = bytearray(DATA_SIZE)
    for block, off in zip((logical_a, logical_b, logical_c, logical_d), offsets):
        plain[off:off + 0x20] = block

    checksum = sum(struct.unpack("<64H", plain)) & 0xFFFF
    enc = encrypt_words(bytes(plain), checksum)

    raw = bytearray(PARTY_SIZE)
    struct.pack_into("<IHH", raw, 0, pid, 0, checksum)
    raw[8:8 + DATA_SIZE] = enc
    raw[0x8C] = 5
    return bytes(raw)


class PK4Tests(unittest.TestCase):
    def test_parse_and_scan(self):
        raw = make_pk4()
        mon = parse_pk4(raw, address=0x02000100)
        self.assertIsNotNone(mon)
        assert mon is not None
        self.assertEqual(mon.species, 152)
        self.assertEqual(mon.level, 5)
        self.assertEqual(mon.ivs, (31, 30, 29, 28, 27, 26))
        self.assertEqual(mon.nature, "Lonely")
        self.assertTrue(mon.shiny)
        self.assertEqual(mon.shiny_value, 1)

        memory = bytearray(0x2000)
        memory[0x100:0x100 + len(raw)] = raw
        found = scan_pk4(bytes(memory), base_address=0x02000000)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].address, 0x02000100)
        self.assertEqual(found[0].species, 152)


if __name__ == "__main__":
    unittest.main()
