from __future__ import annotations

"""Read-only Pokebot-Luma unified framebuffer client.

This transport is presentation/evidence only. It never decides whether a
Pokémon is shiny, never writes game RAM, and never sends controller input.

Firmware protocol on shared UDP 4952:
  11 FRAMEBUFFER_INFO -> live screen metadata
  12 FRAMEBUFFER_READ -> legacy bounded live BGR8 span
  13 FRAMEBUFFER_SNAPSHOT -> freeze one complete converted screen in firmware
  14 FRAMEBUFFER_SNAPSHOT_READ -> read bytes from the frozen snapshot

Commands 13-14 are preferred. Commands 11-12 remain as a compatibility
fallback for older framebuffer-enabled Pokebot-Luma builds.

Commands 1-10 are the existing RAM + acknowledged-controller protocol and are
not used by this module.
"""

import binascii
import secrets
import socket
import struct
import time
import zlib
from pathlib import Path

PORT = 4952
REQ_MAGIC = 0x5242524F
RESP_MAGIC = 0x5342524F
VERSION = 1

CMD_FRAMEBUFFER_INFO = 11
CMD_FRAMEBUFFER_READ = 12
CMD_FRAMEBUFFER_SNAPSHOT = 13
CMD_FRAMEBUFFER_SNAPSHOT_READ = 14

SCREEN_TOP_LEFT = 0
SCREEN_TOP_RIGHT = 1
SCREEN_BOTTOM = 2

STATUS_OK = 0
STATUS_BAD_COMMAND = 3
STATUS_NAMES = {
    0: "OK",
    1: "BAD_MAGIC",
    2: "BAD_VERSION",
    3: "BAD_COMMAND",
    4: "GAME_NOT_FOUND",
    5: "OPEN_FAILED",
    6: "QUERY_FAILED",
    7: "NOT_READABLE",
    8: "RANGE_INVALID",
    9: "LENGTH_INVALID",
    10: "MAP_FAILED",
    11: "INTERNAL",
    12: "INPUT_INVALID",
    13: "INPUT_BUSY",
    14: "INPUT_LEGACY_ACTIVE",
    15: "INPUT_PATCH_FAILED",
    16: "FRAMEBUFFER_INVALID",
    17: "FRAMEBUFFER_UNSUPPORTED",
}

REQ = struct.Struct("<IHHIII")
RESP = struct.Struct("<IHHIIiI")
FB_INFO = struct.Struct("<IIIIII")

FB_FLAG_TOP = 1 << 0
FB_FLAG_RIGHT_EYE = 1 << 1
FB_FLAG_3D_ACTIVE = 1 << 2
FB_FLAG_BGR8 = 1 << 3
FB_FLAG_LIVE = 1 << 4
FB_FLAG_FROZEN = 1 << 5


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return (
        struct.pack(">I", len(data))
        + payload
        + struct.pack(">I", binascii.crc32(payload) & 0xFFFFFFFF)
    )


def write_png(path, bgr: bytes, width: int, height: int, *, bottom_up=True):
    """Write BGR8 bytes as a standard RGB PNG.

    The fb1 firmware exposes Luma's native rotated-framebuffer coordinates.
    Matching its supplied PC BMP tester requires vertically reversing the
    reconstructed rows for normal image presentation.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row_bytes = int(width) * 3
    rows = range(height - 1, -1, -1) if bottom_up else range(height)
    scan = bytearray()
    for y in rows:
        row = bgr[y * row_bytes:(y + 1) * row_bytes]
        scan.append(0)
        for x in range(0, row_bytes, 3):
            blue, green, red = row[x:x + 3]
            scan.extend((red, green, blue))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(scan), 6))
        + _png_chunk(b"IEND", b"")
    )
    return path


class FramebufferBridgeError(RuntimeError):
    pass


class _FramebufferBridge:
    def __init__(self, host, port=PORT, timeout=1.0):
        self.remote = (str(host), int(port))
        self.timeout = max(0.2, float(timeout))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        self.request_id = secrets.randbelow(0x7FFFFFFE) + 1

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _next_id(self):
        self.request_id = (self.request_id + 1) & 0x7FFFFFFF
        if not self.request_id:
            self.request_id = 1
        return self.request_id

    def request(self, command, argument=0, aux=0, *, retries=2):
        request_id = self._next_id()
        packet = REQ.pack(
            REQ_MAGIC,
            VERSION,
            int(command),
            request_id,
            int(argument) & 0xFFFFFFFF,
            int(aux) & 0xFFFFFFFF,
        )

        for _attempt in range(max(1, int(retries) + 1)):
            # Retries reuse the same request packet/request ID. Framebuffer
            # commands are read-only, so retrying a lost response is harmless.
            self.sock.sendto(packet, self.remote)
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sock.settimeout(remaining)
                try:
                    raw, _ = self.sock.recvfrom(4096)
                except socket.timeout:
                    break
                if len(raw) < RESP.size:
                    continue
                magic, version, status, got_id, echoed_arg, result, payload_len = RESP.unpack_from(raw)
                if magic != RESP_MAGIC or version != VERSION or got_id != request_id:
                    continue
                payload = raw[RESP.size:]
                if len(payload) != payload_len:
                    raise FramebufferBridgeError(
                        f"payload mismatch {len(payload)} != {payload_len}"
                    )
                if status != STATUS_OK:
                    name = STATUS_NAMES.get(int(status), f"UNKNOWN_{int(status)}")
                    if int(status) == STATUS_BAD_COMMAND:
                        raise FramebufferBridgeError(
                            f"BAD_COMMAND framebuffer command {int(command)}"
                        )
                    raise FramebufferBridgeError(
                        f"framebuffer command {command} failed: {name} "
                        f"result=0x{int(result) & 0xFFFFFFFF:08X}"
                    )
                return {
                    "argument": int(echoed_arg),
                    "result": int(result),
                    "payload": payload,
                }

        raise FramebufferBridgeError(
            f"UDP/{self.remote[1]} timeout command={int(command)} "
            f"after {max(1, int(retries) + 1)} attempt(s)"
        )

    def framebuffer_info(self, selector):
        response = self.request(CMD_FRAMEBUFFER_INFO, int(selector), 0)
        payload = response["payload"]
        if len(payload) != FB_INFO.size:
            raise FramebufferBridgeError(
                f"framebuffer info payload {len(payload)} != {FB_INFO.size}"
            )
        selector, width, height, bpp, max_pixels, flags = FB_INFO.unpack(payload)
        return {
            "selector": int(selector),
            "width": int(width),
            "height": int(height),
            "bytes_per_pixel": int(bpp),
            "max_pixels_per_read": int(max_pixels),
            "flags": int(flags),
        }

    def framebuffer_span(self, selector, y, x, count):
        selector = int(selector)
        y = int(y)
        x = int(x)
        count = int(count)
        if not (0 <= selector <= 2):
            raise ValueError("invalid framebuffer selector")
        if not (0 <= y <= 0xFF):
            raise ValueError("invalid framebuffer y")
        if not (0 <= x <= 0xFFFF):
            raise ValueError("invalid framebuffer x")
        if count <= 0:
            raise ValueError("invalid framebuffer pixel count")

        argument = (
            (selector & 0xFF)
            | ((y & 0xFF) << 8)
            | ((x & 0xFFFF) << 16)
        )
        payload = self.request(
            CMD_FRAMEBUFFER_READ,
            argument,
            count,
        )["payload"]
        expected = count * 3
        if len(payload) != expected:
            raise FramebufferBridgeError(
                f"framebuffer span payload {len(payload)} != {expected}"
            )
        return payload


    def framebuffer_snapshot(self, selector):
        """Freeze one complete screen in firmware and return its metadata."""
        response = self.request(CMD_FRAMEBUFFER_SNAPSHOT, int(selector), 0)
        payload = response["payload"]
        if len(payload) != FB_INFO.size:
            raise FramebufferBridgeError(
                f"framebuffer snapshot info payload {len(payload)} != {FB_INFO.size}"
            )
        selector, width, height, bpp, max_bytes, flags = FB_INFO.unpack(payload)
        return {
            "selector": int(selector),
            "width": int(width),
            "height": int(height),
            "bytes_per_pixel": int(bpp),
            "max_bytes_per_read": int(max_bytes),
            "flags": int(flags),
            "generation": int(response["result"]) & 0xFFFFFFFF,
        }

    def framebuffer_snapshot_bytes(self, offset, count):
        offset = int(offset)
        count = int(count)
        if offset < 0 or count <= 0:
            raise ValueError("invalid framebuffer snapshot byte range")
        payload = self.request(
            CMD_FRAMEBUFFER_SNAPSHOT_READ,
            offset,
            count,
        )["payload"]
        if len(payload) != count:
            raise FramebufferBridgeError(
                f"snapshot payload {len(payload)} != {count}"
            )
        return payload


def _capture_live_legacy(bridge, selector, info):
    width = int(info["width"])
    height = int(info["height"])
    bpp = int(info["bytes_per_pixel"])
    max_pixels = int(info["max_pixels_per_read"])

    if bpp != 3:
        raise FramebufferBridgeError(
            f"unsupported framebuffer bytes/pixel {bpp}; expected BGR8"
        )
    if width <= 0 or width > 800 or height <= 0 or height > 240:
        raise FramebufferBridgeError(
            f"unexpected framebuffer geometry {width}x{height}"
        )
    if max_pixels <= 0 or max_pixels > 400:
        raise FramebufferBridgeError(
            f"unexpected framebuffer max_pixels {max_pixels}"
        )
    if not (int(info["flags"]) & FB_FLAG_BGR8):
        raise FramebufferBridgeError(
            f"framebuffer did not advertise BGR8: flags=0x{int(info['flags']):08X}"
        )

    image = bytearray(width * height * 3)
    for y in range(height):
        row_offset = y * width * 3
        x = 0
        while x < width:
            count = min(max_pixels, width - x)
            chunk = bridge.framebuffer_span(selector, y, x, count)
            dst = row_offset + x * 3
            image[dst:dst + len(chunk)] = chunk
            x += count
    return bytes(image), width, height, {
        "flags": int(info["flags"]),
        "generation": None,
        "frozen": False,
        "source": "Pokebot-Luma legacy live framebuffer commands 11-12",
    }


def _capture_frozen(bridge, selector):
    info = bridge.framebuffer_snapshot(selector)
    width = int(info["width"])
    height = int(info["height"])
    bpp = int(info["bytes_per_pixel"])
    max_bytes = int(info["max_bytes_per_read"])

    if bpp != 3:
        raise FramebufferBridgeError(
            f"unsupported frozen framebuffer bytes/pixel {bpp}; expected BGR8"
        )
    if width <= 0 or width > 400 or height <= 0 or height > 240:
        raise FramebufferBridgeError(
            f"unexpected frozen framebuffer geometry {width}x{height}"
        )
    if max_bytes <= 0 or max_bytes > 1200:
        raise FramebufferBridgeError(
            f"unexpected snapshot max_bytes {max_bytes}"
        )
    if not (int(info["flags"]) & FB_FLAG_BGR8):
        raise FramebufferBridgeError(
            f"snapshot did not advertise BGR8: flags=0x{int(info['flags']):08X}"
        )
    if not (int(info["flags"]) & FB_FLAG_FROZEN):
        raise FramebufferBridgeError(
            f"snapshot did not advertise FROZEN: flags=0x{int(info['flags']):08X}"
        )

    total = width * height * 3
    image = bytearray(total)
    offset = 0
    while offset < total:
        count = min(max_bytes, total - offset)
        chunk = bridge.framebuffer_snapshot_bytes(offset, count)
        image[offset:offset + count] = chunk
        offset += count

    return bytes(image), width, height, {
        "flags": int(info["flags"]),
        "generation": int(info["generation"]),
        "frozen": True,
        "source": "Pokebot-Luma frozen framebuffer commands 13-14",
    }


def capture_screen(
    host,
    output_path,
    *,
    selector=SCREEN_TOP_LEFT,
    port=PORT,
    timeout=1.0,
):
    """Capture one screen and save PNG.

    Preferred path: firmware-side frozen snapshot (commands 13-14).
    Compatibility path: older live row reads (commands 11-12).

    Shiny authority is never derived from either path.
    """
    bridge = _FramebufferBridge(host, port=port, timeout=timeout)
    started = time.monotonic()
    try:
        try:
            image, width, height, meta = _capture_frozen(bridge, selector)
        except FramebufferBridgeError as exc:
            # fb1 firmware has only commands 11-12. Keep it usable, but clearly
            # report that the resulting image was reconstructed from live rows.
            if "BAD_COMMAND framebuffer command 13" not in str(exc):
                raise
            info = bridge.framebuffer_info(selector)
            image, width, height, meta = _capture_live_legacy(
                bridge, selector, info
            )

        output_path = Path(output_path)
        write_png(
            output_path,
            image,
            width,
            height,
            bottom_up=True,
        )
        return {
            "path": str(output_path.resolve()),
            "selector": int(selector),
            "width": width,
            "height": height,
            "bytes": len(image),
            "flags": f"0x{int(meta['flags']):08X}",
            "live": not bool(meta["frozen"]),
            "frozen": bool(meta["frozen"]),
            "snapshot_generation": meta["generation"],
            "duration_s": round(time.monotonic() - started, 3),
            "source": meta["source"],
        }
    finally:
        bridge.close()


def capture_top_screen(host, output_path, *, port=PORT, timeout=1.0):
    return capture_screen(
        host,
        output_path,
        selector=SCREEN_TOP_LEFT,
        port=port,
        timeout=timeout,
    )


def self_test():
    # PNG writer sanity: 2x2 BGR image must emit a PNG signature and IHDR.
    raw = bytes([
        0, 0, 255,      0, 255, 0,
        255, 0, 0,      255, 255, 255,
    ])
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "self_test.png"
        write_png(path, raw, 2, 2, bottom_up=True)
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or b"IHDR" not in data:
            raise RuntimeError("framebuffer PNG self-test failed")
    return True
