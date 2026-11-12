"""A stand-in faketandem (HTTP) and helpers to run harness commands against it, with no pump, simulator or daemon."""
from __future__ import annotations

import contextlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from harness import cli

STATE = {
    "auth": {"pairing_state": "PairStep2", "pairing_code": "123456", "long_term_key_present": True},
    "connection": {"connected": True, "authenticated": True},
    "basal": {"suspended": False, "temp_active": True, "temp_percent": 150, "temp_rate": 1.275,
              "temp_end": "2026-01-01T01:00:00Z", "profile_rate": 0.85},
    "insulin": {"iob": 1.25, "iob_type": 0, "reservoir_units": 120.5},
    "history": {"count": 4},
    "list": [{"a": 1}, {"a": 2}],
}
HISTORY = [
    {"sequence": 1, "type": "BolusCompleted", "time": "2026-01-01T00:00:00Z", "data": {"units": 1.0}},
    {"sequence": 2, "type": "BasalDelivery", "time": "2026-01-01T00:05:00Z", "data": {"rate": 850}},
    {"sequence": 3, "type": "BasalDelivery", "time": "2026-01-01T00:10:00Z", "data": {"rate": 900}},
    {"sequence": 4, "type": "TempRateActivated", "time": "2026-01-01T00:12:00Z", "data": {"percent": 150}},
]


class FakeFaketandem:
    """Records every request as (method, path, parsed body) and answers the few routes the tests use."""

    def __init__(self):
        self.requests: list[tuple[str, str, object]] = []
        self.fail: dict[tuple[str, str], tuple[int, str]] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                outer.requests.append((self.command, self.path, body))
                if (self.command, self.path) in outer.fail:
                    code, text = outer.fail[(self.command, self.path)]
                    return self._send(code, text)
                path = self.path.split("?")[0]
                if self.command == "GET" and path == "/api/state":
                    return self._send(200, json.dumps(STATE))
                if self.command == "GET" and path == "/api/history":
                    return self._send(200, json.dumps({"entries": HISTORY}))
                if self.command == "GET" and path == "/api/settings":
                    return self._send(200, json.dumps({"PumpVersionRequest": {"mode": "constant"}}))
                if self.command == "GET" and path == "/api/clock":
                    return self._send(200, json.dumps({"mode": "real", "now": "2026-01-01T00:00:00Z",
                                                       "pump_now": "2026-01-01T00:00:00Z", "pump_offset_seconds": 0,
                                                       "pump_timezone": "UTC"}))
                if self.command == "GET" and path == "/api/log":
                    return self._send(200, json.dumps({"entries": [
                        {"seq": 1, "time": "2026-01-01T00:00:00.123Z", "kind": "request", "message": "InitiateBolusRequest"},
                        {"seq": 2, "time": "2026-01-01T00:00:01.123Z", "kind": "response", "message": "InitiateBolusResponse",
                         "fault": "drop_response"}], "last_seq": 2}))
                if path == "/api/faults" and self.command == "POST":
                    return self._send(200, json.dumps({"fault": {"id": 7, **(body or {})}, "faults": []}))
                if path.startswith("/api/faults") and self.command == "DELETE":
                    return self._send(200, json.dumps({"cleared": 2, "faults": []}))
                return self._send(200, json.dumps({"state": STATE}))

            do_GET = do_POST = do_PUT = do_DELETE = _do

            def _send(self, code, text):
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]


@contextlib.contextmanager
def patched_pump(fake: FakeFaketandem):
    with mock.patch("harness.faketandem.current", return_value={"api": fake.url, "setup": "preseed"}), \
            mock.patch("harness.daemon.is_running", return_value=False):
        yield


def th(*argv: str) -> tuple[int, str, str]:
    """cli.main(argv) with stdout and stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()
