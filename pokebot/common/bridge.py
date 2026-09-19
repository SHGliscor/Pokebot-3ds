from __future__ import annotations

import socket
import struct
import time

HOST_DEFAULT = "192.168.0.28"
BRIDGE_PORT = 4952
BRIDGE_TIMEOUT = 2.0

REQ_MAGIC = 0x5242524F
RESP_MAGIC = 0x5342524F
VERSION = 1

CMD_PING = 1
CMD_GAME_INFO = 2
CMD_QUERY = 3
CMD_READ = 4

REQ = struct.Struct("<IHHIII")
RESP = struct.Struct("<IHHIIiI")
GAME_INFO = struct.Struct("<QI8sI")
QUERY_INFO = struct.Struct("<IIIII")

STATUS = {
    0: "OK", 1: "BAD_MAGIC", 2: "BAD_VERSION", 3: "BAD_COMMAND",
    4: "GAME_NOT_FOUND", 5: "OPEN_FAILED", 6: "QUERY_FAILED",
    7: "NOT_READABLE", 8: "RANGE_INVALID", 9: "LENGTH_INVALID",
    10: "MAP_FAILED", 11: "INTERNAL",
}


class Bridge:
    """Low-level read-only Pokebot3DS-CFW transport.

    This module knows nothing about Torchic, Treecko, Mudkip, starter slots,
    starter timings, or starter state machines.
    """

    def __init__(self, host=HOST_DEFAULT, port=BRIDGE_PORT, timeout=BRIDGE_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, command, argument=0, aux=0):
        request_id = int(time.time_ns() & 0xFFFFFFFF)
        packet = REQ.pack(
            REQ_MAGIC, VERSION, command, request_id,
            argument & 0xFFFFFFFF, aux & 0xFFFFFFFF
        )

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(self.timeout)
            s.sendto(packet, (self.host, self.port))
            data, remote = s.recvfrom(4096)

        if len(data) < RESP.size:
            raise RuntimeError(f"short bridge response: {len(data)} bytes")

        magic, version, status, echoed_id, echoed_arg, result, payload_len = RESP.unpack_from(data)
        payload = data[RESP.size:]

        if magic != RESP_MAGIC:
            raise RuntimeError(f"bad response magic 0x{magic:08X}")
        if version != VERSION:
            raise RuntimeError(f"protocol version {version}, expected {VERSION}")
        if echoed_id != request_id:
            raise RuntimeError("request ID mismatch")
        if len(payload) != payload_len:
            raise RuntimeError(f"payload mismatch header={payload_len} actual={len(payload)}")

        return {
            "remote": f"{remote[0]}:{remote[1]}",
            "status": status,
            "status_name": STATUS.get(status, f"UNKNOWN_{status}"),
            "result": result,
            "result_hex": f"0x{result & 0xFFFFFFFF:08X}",
            "argument": echoed_arg,
            "payload_len": payload_len,
            "payload": payload,
        }

    def game_info(self):
        r = self.request(CMD_GAME_INFO)
        payload = r.pop("payload")
        if len(payload) == GAME_INFO.size:
            title_id, pid, raw_name, flags = GAME_INFO.unpack(payload)
            r.update({
                "title_id": f"0x{title_id:016X}",
                "pid": pid,
                "process_name": raw_name.rstrip(b"\0").decode("ascii", errors="replace"),
                "flags": flags,
            })
        return r

    def query(self, address):
        r = self.request(CMD_QUERY, address)
        payload = r.pop("payload")
        r["address"] = f"0x{address:08X}"
        if len(payload) == QUERY_INFO.size:
            base, size, perm, state, page_flags = QUERY_INFO.unpack(payload)
            r.update({
                "base": f"0x{base:08X}",
                "size": size,
                "end": f"0x{base + size:08X}",
                "perm": perm,
                "state": state,
                "page_flags": page_flags,
            })
        return r

    def read(self, address, length):
        r = self.request(CMD_READ, address, length)
        payload = r.pop("payload")
        if r["status"] != 0:
            raise RuntimeError(
                f"READ 0x{address:08X}+{length} failed: "
                f"{r['status_name']} result={r['result_hex']}"
            )
        if len(payload) != length:
            raise RuntimeError(
                f"READ 0x{address:08X}: expected {length}, got {len(payload)}"
            )
        return payload
