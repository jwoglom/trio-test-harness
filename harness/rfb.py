"""Minimal synchronous RFB (VNC) client: auth, zlib/raw framebuffer grabs,
pointer and key events. Used for TrollVNC on the iPhone; vncdotool's Twisted
client negotiated encodings TrollVNC never answered, so we speak plain RFB 3.8 here.

Frames come zlib-encoded (measured 2026-09-25 on TrollVNC: 259 KB instead of 5.8 MB per 828x1792 frame, ~160 ms
instead of ~245 ms over Wi-Fi), with raw as the fallback; TH_VNC_RAW=1 asks for raw only."""
from __future__ import annotations

import socket
import struct
import time
import zlib

from PIL import Image

from .config import CONFIG
from .util import HarnessError

KEYSYMS = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "backspace": 0xFF08, "bsp": 0xFF08, "delete": 0xFFFF,
    "del": 0xFFFF, "escape": 0xFF1B, "esc": 0xFF1B, "space": 0x20, "up": 0xFF52, "down": 0xFF54, "left": 0xFF51,
    "right": 0xFF53, "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56, "shift": 0xFFE1,
    "ctrl": 0xFFE3, "alt": 0xFFE9, "meta": 0xFFE7, "super": 0xFFEB, "cmd": 0xFFE7,
}


def _vnc_des_encrypt(password: str, challenge: bytes) -> bytes:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from vncdotool.rfb import des_encrypt, _vnc_des  # reuse the well-tested helper
        return des_encrypt(_vnc_des(password), challenge)


class RFBClient:
    def __init__(self, host: str, port: int, password: str | None = None, timeout: float = 20.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock: socket.socket | None = None
        self.width = self.height = 0
        self.name = ""

    # ---- connection ----
    def connect(self) -> "RFBClient":
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        ver = self._read(12)
        if not ver.startswith(b"RFB "):
            raise HarnessError(f"not a VNC server: {ver!r}")
        s.sendall(b"RFB 003.008\n")
        n = self._read(1)[0]
        if n == 0:
            raise HarnessError("VNC server refused: " + self._read_string())
        types = list(self._read(n))
        if 1 in types:
            s.sendall(b"\x01")
        elif 2 in types:
            if not self.password:
                raise HarnessError("VNC server requires a password (set TH_VNC_PASSWORD)")
            s.sendall(b"\x02")
            s.sendall(_vnc_des_encrypt(self.password, self._read(16)))
        else:
            raise HarnessError(f"unsupported VNC security types {types}")
        res = struct.unpack(">I", self._read(4))[0]
        if res != 0:
            raise HarnessError("VNC authentication failed (check TH_VNC_PASSWORD, first 8 chars matter)")
        s.sendall(b"\x01")  # ClientInit: shared
        si = self._read(24)
        self.width, self.height = struct.unpack(">HH", si[:4])
        self.name = self._read(struct.unpack(">I", si[20:24])[0]).decode("utf-8", "replace")
        # 32bpp truecolor, little-endian, RGB in low 24 bits -> easy PIL conversion
        s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        encodings = [0] if CONFIG.bool("TH_VNC_RAW") else [6, 0]   # zlib, raw
        s.sendall(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))
        self._zlib = zlib.decompressobj()   # one zlib stream per connection (RFC 6143 7.7.2)
        return self

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _read(self, n: int) -> bytes:
        assert self.sock
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(n - len(buf), 1 << 20))
            if not chunk:
                raise HarnessError("VNC connection closed")
            buf += chunk
        return bytes(buf)

    def _read_string(self) -> str:
        return self._read(struct.unpack(">I", self._read(4))[0]).decode("utf-8", "replace")

    # ---- framebuffer ----
    def capture(self, incremental: bool = False) -> Image.Image:
        assert self.sock
        self.sock.sendall(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))
        img = Image.new("RGBA", (self.width, self.height))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            mtype = self._read(1)[0]
            if mtype == 0:  # FramebufferUpdate
                self._read(1)
                nrects = struct.unpack(">H", self._read(2))[0]
                for _ in range(nrects):
                    x, y, w, h, enc = struct.unpack(">HHHHi", self._read(12))
                    if enc in (0, 6):
                        if enc == 0:
                            data = self._read(w * h * 4)
                        else:
                            data = self._zlib.decompress(self._read(struct.unpack(">I", self._read(4))[0]))
                            if len(data) != w * h * 4:
                                raise HarnessError(f"zlib rect {w}x{h}: {len(data)} bytes, expected {w * h * 4}")
                        rect = Image.frombytes("RGBA", (w, h), data, "raw", "BGRA")
                        img.paste(rect, (x, y))
                    elif enc == 1:  # CopyRect
                        sx, sy = struct.unpack(">HH", self._read(4))
                        img.paste(img.crop((sx, sy, sx + w, sy + h)), (x, y))
                    else:
                        raise HarnessError(f"unexpected VNC encoding {enc}")
                return img.convert("RGB")
            elif mtype == 1:  # SetColourMapEntries
                self._read(1); first, n = struct.unpack(">HH", self._read(4)); self._read(n * 6)
            elif mtype == 2:  # Bell
                pass
            elif mtype == 3:  # ServerCutText
                self._read(3); self._read(struct.unpack(">I", self._read(4))[0])
            else:
                raise HarnessError(f"unknown VNC server message {mtype}")
        raise HarnessError("timed out waiting for a framebuffer update")

    # ---- input ----
    def pointer(self, x: int, y: int, mask: int = 0) -> None:
        assert self.sock
        x = max(0, min(self.width - 1, int(x))); y = max(0, min(self.height - 1, int(y)))
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def key(self, keysym: int, down: bool) -> None:
        assert self.sock
        self.sock.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    def key_press(self, key: str, hold: float = 0.03) -> None:
        if len(key) == 1:
            ks = ord(key)
        else:
            ks = KEYSYMS.get(key.lower())
            if ks is None:
                raise HarnessError(f"unknown key {key!r}")
        self.key(ks, True); time.sleep(hold); self.key(ks, False)

    def cut_text(self, text: str) -> None:
        assert self.sock
        data = text.encode("latin-1", "replace")
        self.sock.sendall(struct.pack(">BxxxI", 6, len(data)) + data)
