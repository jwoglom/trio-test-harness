"""Live phone view for `th webui` (rooted-vnc): a WebSocket <-> TrollVNC proxy for noVNC, and harness buttons.

The browser runs noVNC against /api/vnc/ws. This process opens its own (shared) RFB connection to TrollVNC, does the
VNC password handshake itself (TH_VNC_PASSWORD never reaches the browser), then offers the browser RFB 3.8 with
security type None and pipes bytes both ways. TrollVNC takes several shared clients, so the harness daemon, an agent
and this viewer can all be connected at once.

Buttons (screenshot, Home, lock, unlock, wake, type, launch Trio) go through the harness driver: the rooted-vnc daemon
when one is running, else one VNCDriver kept by this process (it also owns the iproxy USB forward, if used).
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading

from .util import HarnessError, log

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TARGET = "rooted-vnc"

_local = None                      # VNCDriver used when no daemon runs (and for iproxy)
_lock = threading.RLock()
last_error = ""


def _local_driver():
    global _local
    with _lock:
        if _local is None:
            from .vnc import VNCDriver
            _local = VNCDriver()
        return _local


def driver():
    from . import daemon as D
    if D.is_running(TARGET):
        return D.RemoteDriver(TARGET)
    return _local_driver()


def act(fn):
    """Run fn(driver); if the kept local connection went stale (phone slept, TrollVNC restarted), reconnect once."""
    with _lock:
        drv = driver()
        try:
            return fn(drv)
        except (HarnessError, OSError):
            if drv is not _local:
                raise
            _local.close()
            return fn(_local)


def close() -> None:
    with _lock:
        if _local is not None:
            _local.close()


# ---------------- upstream RFB -----------------

def _recv(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise HarnessError("TrollVNC closed the connection")
        buf += chunk
    return bytes(buf)


def _connect(d) -> tuple[socket.socket, str, int]:
    """TCP to TrollVNC: the configured route first, then the USB forward (iproxy on 127.0.0.1:TH_VNC_LOCAL_PORT).
    With TH_VNC_HOST set to the phone's Wi-Fi address the harness never uses USB, but Wi-Fi is often the part that
    is down (phone asleep, other subnet) while the USB forward still answers."""
    routes = []
    if not d.via_usb:
        routes.append((d.host, d.port))
    routes.append(("127.0.0.1", d.local_port))
    errors = []
    for host, port in routes:
        try:
            if host == "127.0.0.1":
                with _lock:
                    was, d.via_usb = d.via_usb, True
                    try:
                        d._ensure_iproxy()          # starts `iproxy` if nothing listens on the local port yet
                    finally:
                        d.via_usb = was
            return socket.create_connection((host, port), timeout=15), host, port
        except (OSError, HarnessError) as e:
            errors.append(f"{host}:{port}: {e}")
    raise HarnessError("cannot reach TrollVNC (" + "; ".join(errors) + "). Is the phone awake, TrollVNC on, and the "
                       "phone on USB or the same network?")


def upstream() -> socket.socket:
    """A TrollVNC connection that has passed security (the next thing it expects is ClientInit)."""
    from .rfb import _vnc_des_encrypt
    d = _local_driver()
    s, host, port = _connect(d)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        if not _recv(s, 12).startswith(b"RFB "):
            raise HarnessError(f"{host}:{port} is not a VNC server")
        s.sendall(b"RFB 003.008\n")
        n = _recv(s, 1)[0]
        if n == 0:
            raise HarnessError("TrollVNC refused: " + _recv(s, struct.unpack(">I", _recv(s, 4))[0]).decode("utf-8", "replace"))
        types = list(_recv(s, n))
        if 1 in types:
            s.sendall(b"\x01")
        elif 2 in types:
            if not d.password:
                raise HarnessError("TrollVNC wants a password; set TH_VNC_PASSWORD in .env")
            s.sendall(b"\x02")
            s.sendall(_vnc_des_encrypt(d.password, _recv(s, 16)))
        else:
            raise HarnessError(f"unsupported VNC security types {types}")
        if struct.unpack(">I", _recv(s, 4))[0] != 0:
            raise HarnessError("VNC authentication failed (check TH_VNC_PASSWORD; the first 8 characters count)")
    except BaseException:
        s.close()
        raise
    s.settimeout(None)
    return s


# ---------------- WebSocket (RFC 6455, server side) -----------------

def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    m = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(len(data), "big")


class WebSocket:
    def __init__(self, rfile, wfile):
        self.r, self.w = rfile, wfile
        self.buf = bytearray()
        self.wlock = threading.Lock()

    def _read(self, n: int) -> bytes:
        data = self.r.read(n)
        if data is None or len(data) < n:
            raise EOFError
        return data

    def frame(self) -> bytes | None:
        """The next data frame's payload; None once the browser closed."""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 16 << 20:
                raise EOFError
            mask = self._read(4) if b1 & 0x80 else b"\0\0\0\0"
            data = _unmask(self._read(n), mask)
            if op == 0x8:
                self.send(data[:2], 0x8)
                return None
            if op == 0x9:
                self.send(data, 0xA)
            elif op in (0x0, 0x1, 0x2):
                return data

    def recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            f = self.frame()
            if f is None:
                raise EOFError
            self.buf += f
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv(self) -> bytes | None:
        if self.buf:
            out = bytes(self.buf)
            self.buf.clear()
            return out
        return self.frame()

    def send(self, data: bytes, op: int = 0x2) -> None:
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else
                                     bytes([126]) + struct.pack(">H", n) if n < 1 << 16 else
                                     bytes([127]) + struct.pack(">Q", n))
        with self.wlock:
            self.w.write(head + data)
            self.w.flush()


def proxy(ws: WebSocket) -> None:
    """Bridge an upgraded browser WebSocket to TrollVNC until either side closes."""
    global last_error
    try:
        up = upstream()
        last_error = ""
    except (HarnessError, OSError) as e:
        last_error = str(e)
        log(f"webui: live view: {e}")
        ws.send(struct.pack(">H", 1011) + str(e).encode()[:120], 0x8)
        return
    try:
        # the browser's side of the handshake: RFB 3.8, security None (upstream already authenticated)
        ws.send(b"RFB 003.008\n")
        ws.recv_exact(12)
        ws.send(b"\x01\x01")
        ws.recv_exact(1)
        ws.send(b"\0\0\0\0")

        def pump():
            try:
                while chunk := up.recv(1 << 16):
                    ws.send(chunk)
            except OSError:
                pass
            try:
                ws.send(struct.pack(">H", 1000), 0x8)
            except OSError:
                pass

        threading.Thread(target=pump, daemon=True, name="vnc-pump").start()
        while (data := ws.recv()) is not None:
            up.sendall(data)
    except (EOFError, OSError, ValueError):
        pass
    finally:
        try:
            up.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        up.close()
