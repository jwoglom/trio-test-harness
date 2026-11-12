"""Typed `faketandem` subcommands over faketandem's HTTP API (harness/faketandem.py has the transport).

Each wrapper builds one request (a pure function returning `(method, path, body)`, so tests need no pump), sends it
through `faketandem.api` (the sim daemon when one runs, which is how the CI agent's sandbox reaches the pump) and
prints what it did. `faketandem api` stays the way to anything without a wrapper; it only normalises the path.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from typing import Callable, Iterable

from .util import HarnessError

FAULT_KINDS = ("drop_response", "delay_response", "error_response", "disconnect", "drop_fragment", "reject_quick_pair",
               "radio_off", "radio_on")
# /api/state/<action>: the route table in faketandem's pkg/harness/actions.go
STATE_ACTIONS = ("bolus/start", "bolus/stall", "bolus/resume", "bolus/abort", "bolus/complete", "tempbasal/start",
                 "tempbasal/stop", "suspend", "resume", "history/append", "history/bulk", "history/erase", "scenario",
                 "qualifyingevent", "time", "filltubing/button", "alert", "alarm", "malfunction", "reminder", "cgmalert",
                 "cartridge/ready", "cartridge/insert")
NOTIFICATION_KINDS = ("alert", "alarm", "malfunction", "reminder", "cgmalert")
SUSPEND_REASONS = ("user", "occlusion", "alarm")
_STATE_WORDS = {w for a in STATE_ACTIONS for w in a.split("/")}


# ---------------- fields -----------------

def get_field(data, path: str):
    """`a.b.0.c` into parsed JSON; a missing step names the keys that exist there."""
    cur = data
    for i, step in enumerate(path.split(".")):
        here = ".".join(path.split(".")[:i]) or "(top level)"
        if isinstance(cur, dict):
            if step not in cur:
                raise HarnessError(f"--field {path}: no key {step!r} at {here}; it has: {', '.join(map(str, cur)) or 'nothing'}")
            cur = cur[step]
        elif isinstance(cur, list):
            try:
                cur = cur[int(step)]
            except (ValueError, IndexError):
                raise HarnessError(f"--field {path}: {step!r} is not an index into the {len(cur)}-item list at {here}") from None
        else:
            raise HarnessError(f"--field {path}: {here} is {json.dumps(cur)}, not an object or list")
    return cur


def format_value(v) -> str:
    """A scalar as a shell would want it (true, null, 1.5, text); an object or list as JSON."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        return json.dumps(v, indent=2)
    return str(v)


# ---------------- paths -----------------

def _squash(s: str) -> str:
    return re.sub(r"[_\-\s]", "", s).lower()


def normalise_path(path: str, settings_names: Callable[[], Iterable[str]] | None = None) -> str:
    """The path faketandem's router knows: `fill_tubing` and `temp-basal` spellings under /api/state/ become
    `filltubing`, `tempbasal`; a /api/settings/<Message> name takes the case it has in `GET /api/settings`."""
    path = path if path.startswith("/") else "/" + path
    base, sep, query = path.partition("?")
    parts = base.rstrip("/").split("/")
    if len(parts) > 3 and parts[1] == "api" and parts[2] == "state":
        fixed = {w for w in _STATE_WORDS}
        for i in range(3, len(parts)):
            sq = _squash(parts[i])
            if sq in fixed:
                parts[i] = sq
    elif len(parts) > 3 and parts[1] == "api" and parts[2] == "settings" and settings_names:
        known = {n.lower(): n for n in settings_names()}
        if parts[3] not in known.values():
            parts[3] = known.get(parts[3].lower(), parts[3])
    return "/".join(parts) + sep + query


def enrich_error(method: str, path: str, err: HarnessError) -> HarnessError:
    """What faketandem's bare errors leave out: the routes, and the response modes."""
    text = str(err)
    if "unknown response mode" in text and "valid modes" not in text:
        return HarnessError(text + " (valid modes: constant, incremental, time_based)")
    if " -> 404" in text and path.startswith("/api/state/"):
        return HarnessError(text + f" (the /api/state/ actions are: {', '.join(STATE_ACTIONS)})")
    if "unknown fault kind" in text:
        return HarnessError(text + f" (kinds: {', '.join(FAULT_KINDS)})")
    return err


# ---------------- history -----------------

def parse_types(values: list[str] | None) -> set[str]:
    return {t.strip().lower() for v in values or [] for t in v.split(",") if t.strip()}


def _entry_time(e: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(e.get("time")).replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_history(entries: list[dict], *, types: set[str] | None = None, last: int | None = None,
                   since: str | None = None) -> list[dict]:
    """Records of the given types (case-insensitive), at or after `since` (a sequence number, or a time as the logs
    take it: 10m, 2h, an ISO time), the last `last` of them. Without --type the 5-minute BasalDelivery-style
    records stay out, as in the plain `history`."""
    out = list(entries)
    if types:
        out = [e for e in out if str(e.get("type", "")).lower() in types]
    else:
        out = [e for e in out if e.get("type") not in ("BasalDelivery", "DailyBasal", "ControlIQPcmChange")]
    if since:
        if re.fullmatch(r"\d+", since.strip()):
            out = [e for e in out if int(e.get("sequence", 0)) >= int(since)]
        else:
            from .applogs import parse_since
            cutoff = parse_since(since)
            out = [e for e in out if (t := _entry_time(e)) is None or t >= cutoff]
    return out[-last:] if last else out


def format_entry(e: dict) -> str:
    return f"#{e.get('sequence')} {str(e.get('time'))[:19]}Z {e.get('type')} {json.dumps(e.get('data') or {}, sort_keys=True)}"


# ---------------- request builders -----------------

def fault_request(kind: str, *, message: str | None = None, delay_ms: int | None = None, every: bool = False,
                  error_code: int | None = None, index: int | None = None, opcode: int | None = None,
                  count: int | None = None, characteristic: str | None = None, after: str | None = None,
                  fragments_sent: int | None = None, every_nth: int | None = None) -> tuple[str, str, dict]:
    if kind not in FAULT_KINDS:
        raise HarnessError(f"unknown fault kind {kind!r} ({', '.join(FAULT_KINDS)})")
    if kind == "delay_response" and not delay_ms:
        raise HarnessError("fault delay_response needs --delay-ms N")
    body: dict = {"kind": kind}
    for key, val in (("message", message), ("delay_ms", delay_ms), ("error_code", error_code), ("index", index),
                     ("opcode", opcode), ("count", count), ("characteristic", characteristic), ("after", after),
                     ("fragments_sent", fragments_sent), ("every_nth", every_nth)):
        if val is not None:
            body[key] = val
    if every:
        body["every"] = True
    return "POST", "/api/faults", body


def clock_requests(*, offset_seconds: float | None = None, advance_seconds: float | None = None,
                   advance_ticks: int | None = None, set_time: str | None = None, zone: str | None = None,
                   real: bool = False, pump_local_time: str | None = None,
                   shift_pump_time: float | None = None) -> list[tuple[str, str, dict | None]]:
    """The calls for one `faketandem clock`, in the order they have to run. `advance` needs the manual clock, which
    the first of these puts in place when the caller has not."""
    reqs: list[tuple[str, str, dict | None]] = []
    if real:
        reqs.append(("PUT", "/api/clock", {"mode": "real", "pump_offset_seconds": 0}))
    if set_time:
        reqs.append(("PUT", "/api/clock", {"mode": "manual", "now": _rfc3339(set_time)}))
    put: dict = {}
    if offset_seconds is not None:
        put["pump_offset_seconds"] = offset_seconds
    if zone:
        put["pump_timezone"] = zone
    if put:
        reqs.append(("PUT", "/api/clock", put))
    if pump_local_time and shift_pump_time is not None:
        raise HarnessError("clock: --pump-local-time and --shift-pump-time do not go together")
    if pump_local_time:
        reqs.append(("POST", "/api/state/time", {"local_time": pump_local_time.rstrip("Z")[:19]}))
    if shift_pump_time is not None:
        reqs.append(("POST", "/api/state/time", {"delta_seconds": shift_pump_time}))
    if advance_seconds is not None or advance_ticks is not None:
        body: dict = {"seconds": advance_seconds or 0}
        if advance_ticks:
            body["ticks"] = advance_ticks
        reqs.append(("POST", "/api/clock/advance", body))
    return reqs or [("GET", "/api/clock", None)]


def _rfc3339(text: str) -> str:
    try:
        t = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HarnessError(f"clock --set {text!r}: use an ISO time with a zone, e.g. 2026-03-08T01:55:00-05:00 or "
                           "2026-03-08T06:55:00Z") from None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).isoformat()


def parse_assignments(items: list[str]) -> dict:
    """KEY=VALUE pairs for PUT /api/state; each value is JSON when it parses (true, 42.5, [..], {..}), else text."""
    body: dict = {}
    for item in items:
        key, eq, raw = item.partition("=")
        if not eq or not key:
            raise HarnessError(f"set: {item!r} is not KEY=VALUE")
        try:
            body[key] = json.loads(raw)
        except ValueError:
            body[key] = raw
    if not body:
        raise HarnessError("set: give at least one KEY=VALUE (e.g. reservoir_units=42.5 battery_percent=17)")
    return body


def suspend_request(reason: str | None, keep_temp: bool, message: str | None = None) -> tuple[str, str, dict]:
    if reason and reason not in SUSPEND_REASONS:
        raise HarnessError(f"suspend --reason {reason!r}: {' | '.join(SUSPEND_REASONS)}")
    body: dict = {}
    if reason:
        body["reason"] = reason
    if keep_temp:
        if reason == "user":
            raise HarnessError("suspend: --keep-temp is for --reason occlusion or alarm; faketandem refuses it with user")
        body["reason"] = reason or "occlusion"
        body["keep_temp"] = True
    if message:
        body["message"] = message
    return "POST", "/api/state/suspend", body


def notification_request(kind: str, action: str, ident: str, *, message: str | None = None,
                         fault_id: int | None = None) -> tuple[str, str, dict]:
    """POST (raise) or DELETE (clear) /api/state/<kind>: IDENT is a pumpX2 name (LOW_INSULIN, LOW_INSULIN_ALERT) or a bit."""
    if kind not in NOTIFICATION_KINDS:
        raise HarnessError(f"unknown notification kind {kind!r} ({', '.join(NOTIFICATION_KINDS)})")
    ident = ident.strip()
    if not ident:
        raise HarnessError(f"{kind} {action}: give a name (LOW_INSULIN) or a bit number")
    body: dict = {"code": int(ident, 0)} if re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", ident) else {"type": ident}
    if action == "raise":
        if message and kind in ("alert", "alarm"):
            body["message"] = message
        if fault_id is not None:
            if kind != "malfunction":
                raise HarnessError("--fault-id is for malfunction")
            body["fault_id"] = fault_id
    return "POST" if action == "raise" else "DELETE", f"/api/state/{kind}", body


def cartridge_request(action: str, *, units: float | None = None) -> tuple[str, str, dict]:
    if action == "ready":
        return "POST", "/api/state/cartridge/ready", {}
    if not units or units <= 0:
        raise HarnessError("cartridge insert needs --units U (positive, at most 300)")
    return "POST", "/api/state/cartridge/insert", {"units": units}


def bolus_request(action: str, *, units: float | None = None, source: str | None = None,
                  duration_seconds: float | None = None, rate: float | None = None,
                  delivered_units: float | None = None) -> tuple[str, str, dict]:
    if action == "start":
        if not units or units <= 0:
            raise HarnessError("bolus start needs --units U (positive)")
        body = {"units": units}
        for k, v in (("source", source), ("duration_seconds", duration_seconds), ("rate", rate)):
            if v is not None:
                body[k] = v
    else:
        body = {"delivered_units": delivered_units} if delivered_units is not None else {}
    return "POST", f"/api/state/bolus/{action}", body


def tempbasal_request(action: str, *, percent: int | None = None, rate: float | None = None,
                      duration_minutes: int | None = None) -> tuple[str, str, dict]:
    if action == "stop":
        return "POST", "/api/state/tempbasal/stop", {}
    if percent is None and rate is None:
        raise HarnessError("tempbasal start needs --percent P or --rate U/HR")
    if percent is not None and rate is not None:
        raise HarnessError("tempbasal start: --percent and --rate do not go together")
    body: dict = {"percent": percent} if percent is not None else {"rate": rate}
    body["duration_minutes"] = duration_minutes or 30
    return "POST", "/api/state/tempbasal/start", body


def log_path(since: int | None) -> str:
    return "/api/log" + (f"?since={since}" if since else "")


def format_log_entry(e: dict, cargo: bool = False) -> str:
    bits = [f"#{e.get('seq')}", str(e.get("time", ""))[11:23], f"{e.get('kind', ''):9}", e.get("message") or ""]
    if e.get("fault"):
        bits.append(f"[fault {e['fault']}]")
    if e.get("note"):
        bits.append(f"({e['note']})")
    if e.get("connected") is not None:
        bits.append("connected" if e["connected"] else "disconnected")
    if cargo and e.get("cargo"):
        bits.append(json.dumps(e["cargo"], sort_keys=True))
    return " ".join(b for b in bits if b)


# ---------------- running them -----------------

def _call(FT, method: str, path: str, body=None):
    names = None
    if path.startswith("/api/settings/"):
        def names():
            return list((FT.api("GET", "/api/settings") or {}).keys())
    fixed = normalise_path(path, names)
    if fixed != path:
        print(f"faketandem: {path} -> {fixed}", file=sys.stderr)
    try:
        return FT.api(method, fixed, body)
    except HarnessError as e:
        raise enrich_error(method, fixed, e) from None


def _state_lines(FT, result) -> list[str]:
    """The pump's side after an action, without the pairing line."""
    if isinstance(result, dict) and isinstance(result.get("state"), dict):
        return FT.summary(result["state"])[1:]
    return []


def _show(FT, what: str, result, extra: dict | None = None) -> None:
    print(what)
    if isinstance(result, dict):
        for k, v in result.items():
            if k != "state":
                print(f"  {k}: {json.dumps(v)}")
    for k, v in (extra or {}).items():
        print(f"  {k}: {v}")
    for line in _state_lines(FT, result):
        print("  " + line)


def run(FT, args) -> int:
    """Handles the typed subcommands of `faketandem` (cli.cmd_faketandem passes the rest here); the pump's running."""
    sub = args.sub
    if sub == "fault":
        if args.fault_action == "clear":
            res = _call(FT, "DELETE", "/api/faults" + (f"/{args.id}" if args.id is not None else ""))
            n = res.get("cleared", 0) if isinstance(res, dict) else None
            if n == 0:
                print("no armed faults to clear (a one-shot fault is consumed when it fires; `fault list` shows what is armed)")
            else:
                print(f"cleared {n} fault(s)" if n is not None else "cleared")
        elif args.fault_action == "list":
            print(json.dumps(_call(FT, "GET", "/api/faults"), indent=2))
        else:
            m, p, b = fault_request(args.kind, message=args.message, delay_ms=args.delay_ms, every=args.every,
                                    error_code=args.error_code, index=args.index, opcode=args.opcode, count=args.count,
                                    characteristic=args.characteristic, after=args.after,
                                    fragments_sent=args.fragments_sent, every_nth=args.every_nth)
            res = _call(FT, m, p, b)
            if args.kind in ("radio_off", "radio_on"):
                print(f"radio {'on' if args.kind == 'radio_on' else 'off'}: {json.dumps(res)}")
            else:
                f = res.get("fault") if isinstance(res, dict) else None
                print(f"armed {args.kind}" + (f" #{f['id']}" if f else "") + (f" on {args.message}" if args.message else "")
                      + (" (every match)" if args.every else "") + f": {json.dumps(b)}")
        return 0
    if sub == "clock":
        reqs = clock_requests(offset_seconds=args.offset_seconds, advance_seconds=args.advance_seconds,
                              advance_ticks=args.advance_ticks, set_time=args.set, zone=args.zone, real=args.real,
                              pump_local_time=args.pump_local_time, shift_pump_time=args.shift_pump_time)
        if reqs[-1][1] == "/api/clock/advance" and _call(FT, "GET", "/api/clock").get("mode") != "manual" \
                and not any(b and b.get("mode") == "manual" for _, _, b in reqs):
            reqs.insert(0, ("PUT", "/api/clock", {"mode": "manual"}))
            print("clock: switching the pump to the manual clock first (advance needs it)")
        for m, p, b in reqs:
            _call(FT, m, p, b)
            if p != "/api/clock" or m != "GET":
                print(f"{m} {p} {json.dumps(b)}")
        clock = _call(FT, "GET", "/api/clock")
        print(f"clock: mode {clock.get('mode')}, now {clock.get('now')}, pump {clock.get('pump_now')} "
              f"(skew {clock.get('pump_offset_seconds', 0):g} s, zone {clock.get('pump_timezone')})")
        return 0
    if sub == "set":
        body = parse_assignments(args.assignments)
        res = _call(FT, "PUT", "/api/state", body)
        _show(FT, "set " + " ".join(f"{k}={json.dumps(v)}" for k, v in body.items()), res)
        return 0
    if sub == "suspend":
        m, p, b = suspend_request(args.reason, args.keep_temp, args.message)
        _show(FT, f"suspended ({b.get('reason', 'user')}{', keeping the temp' if args.keep_temp else ''})", _call(FT, m, p, b))
        return 0
    if sub in NOTIFICATION_KINDS:
        m, p, b = notification_request(sub, args.notify_action, args.ident, message=getattr(args, "message", None),
                                       fault_id=getattr(args, "fault_id", None))
        verb = "raised" if args.notify_action == "raise" else "cleared"
        _show(FT, f"{sub} {verb}: {json.dumps(b)}", _call(FT, m, p, b))
        return 0
    if sub == "cartridge":
        m, p, b = cartridge_request(args.cartridge_action, units=getattr(args, "units", None))
        _show(FT, "cartridge " + ("retracted, ready to change" if args.cartridge_action == "ready"
                                  else f"inserted ({args.units:g} U)"), _call(FT, m, p, b))
        return 0
    if sub == "resume":
        body = {"clear_alarms": False} if args.keep_alarms else {}
        _show(FT, "resumed", _call(FT, "POST", "/api/state/resume", body))
        return 0
    if sub == "bolus":
        m, p, b = bolus_request(args.bolus_action, units=args.units, source=args.source,
                                duration_seconds=args.duration_seconds, rate=args.rate,
                                delivered_units=args.delivered_units)
        _show(FT, f"bolus {args.bolus_action}" + (f" {args.units:g} U" if args.units else ""), _call(FT, m, p, b))
        return 0
    if sub == "tempbasal":
        m, p, b = tempbasal_request(args.temp_action, percent=args.percent, rate=args.rate,
                                    duration_minutes=args.duration_minutes)
        _show(FT, "temp basal stop" if args.temp_action == "stop" else f"temp basal start {json.dumps(b)}",
              _call(FT, m, p, b))
        return 0
    if sub == "log":
        if args.clear:
            _call(FT, "DELETE", "/api/log")
            print("request log cleared")
            return 0
        page = _call(FT, "GET", log_path(args.since))
        entries = page.get("entries", [])
        if args.message:
            rx = re.compile(args.message, re.I)
            entries = [e for e in entries if rx.search(e.get("message") or "") or rx.search(e.get("fault") or "")]
        if args.kind:
            entries = [e for e in entries if e.get("kind") == args.kind]
        if args.last:
            entries = entries[-args.last:]
        if args.count:
            print(len(entries))
        elif args.json:
            print(json.dumps(entries, indent=2))
        else:
            print("\n".join(format_log_entry(e, args.cargo) for e in entries) or "(no entries)")
        return 0
    if sub == "transport":
        if args.mtu is None:
            print(json.dumps(_call(FT, "GET", "/api/transport"), indent=2))
        else:
            print(json.dumps(_call(FT, "PUT", "/api/transport", {"att_mtu": args.mtu}), indent=2))
        return 0
    raise HarnessError(f"unknown faketandem command {sub}")


def add_parsers(fts) -> None:
    """The typed subcommands, onto `faketandem`'s subparsers."""
    f = fts.add_parser("fault", help="arm or clear a pump fault: drop_response (applied, answer lost), error_response, "
                                     "delay_response, disconnect, drop_fragment, reject_quick_pair, radio_off/radio_on")
    fa = f.add_subparsers(dest="fault_action", required=True)
    fadd = fa.add_parser("add", help="arm a fault against the next matching message (--every: every one)")
    fadd.add_argument("kind", choices=FAULT_KINDS)
    fadd.add_argument("--message", help="the exchange it applies to, request or response name, e.g. InitiateBolusRequest")
    fadd.add_argument("--delay-ms", type=int, help="delay_response: how long to hold the answer")
    fadd.add_argument("--every", action="store_true", help="every matching message, not just the next")
    fadd.add_argument("--count", type=int, help="the next N matches (default 1)")
    fadd.add_argument("--error-code", type=int, help="error_response: the ErrorResponse code")
    fadd.add_argument("--index", type=int, help="drop_fragment: which fragment (0-based) of a message")
    fadd.add_argument("--every-nth", type=int, help="drop_fragment: every Nth fragment")
    fadd.add_argument("--opcode", type=int); fadd.add_argument("--characteristic")
    fadd.add_argument("--after", choices=["request", "partial_response"], help="disconnect: before or part-way through the answer")
    fadd.add_argument("--fragments-sent", type=int, help="disconnect after partial_response: fragments that get out")
    fclr = fa.add_parser("clear", help="remove every armed fault, or one with --id")
    fclr.add_argument("--id", type=int)
    fa.add_parser("list", help="the armed faults")

    c = fts.add_parser("clock", help="the pump's clock (no option: show it): skew it, step it, set its zone or time")
    c.add_argument("--offset-seconds", type=float, metavar="S", help="skew the pump's clock S seconds from the phone's "
                                                                     "(PUT /api/clock pump_offset_seconds); 90000 is over the driver's 24 h limit")
    c.add_argument("--advance-seconds", type=float, metavar="S", help="move the manual clock S seconds on (switches to it)")
    c.add_argument("--advance-ticks", type=int, metavar="N", help="run N simulation steps for that jump (or alone)")
    c.add_argument("--set", metavar="ISO", help="put the clock on manual at this time (ISO with a zone)")
    c.add_argument("--zone", metavar="TZ", help="the pump's clock set to this IANA zone's local time now, e.g. America/New_York")
    c.add_argument("--pump-local-time", metavar="LOCAL", help="the pump's screen time set to e.g. 2026-03-08T01:55:00 (POST /api/state/time; tells no connected driver)")
    c.add_argument("--shift-pump-time", type=float, metavar="S", help="as the screen, move the pump's time by S seconds")
    c.add_argument("--real", action="store_true", help="back to the real clock, no skew")

    s = fts.add_parser("set", help="stage pump fields: KEY=VALUE... (PUT /api/state; values are JSON, nested too), e.g. "
                                   "reservoir_units=5 closed_loop_enabled=true refusals='{\"temp_rate_while_running\":false}' "
                                   "fill_cannula_stream_lost=true cartridge_loaded=true")
    s.add_argument("assignments", nargs="+", metavar="KEY=VALUE")

    su = fts.add_parser("suspend", help="stop delivery from the pump's side (POST /api/state/suspend)")
    su.add_argument("--reason", choices=SUSPEND_REASONS, help="default user; occlusion and alarm raise the pump's alarm")
    su.add_argument("--keep-temp", action="store_true", help="occlusion or alarm stop that leaves the running temp set (it runs again on resume); "
                                                      "alone, an occlusion stop, as a Mobi was seen doing")
    su.add_argument("--message", help="alarm text")
    for kind, what in (("alert", "an alert (LOW_INSULIN)"), ("alarm", "an alarm (RESUME_PUMP_ALARM, OCCLUSION_ALARM)"),
                       ("malfunction", "a malfunction (stays until cleared here; the app cannot dismiss it)"),
                       ("reminder", "a reminder (SITE_CHANGE)"), ("cgmalert", "a CGM alert (LOW_CGM_ALERT)")):
        n = fts.add_parser(kind, help=f"raise or clear {what}: POST/DELETE /api/state/{kind}")
        na = n.add_subparsers(dest="notify_action", required=True)
        for act in ("raise", "clear"):
            x = na.add_parser(act, help=f"{act} it by pumpX2 name (category suffix optional) or by bit number")
            x.add_argument("ident", metavar="NAME_OR_BIT")
            if kind in ("alert", "alarm") and act == "raise":
                x.add_argument("--message", help="text on the pump's screen")
            if kind == "malfunction" and act == "raise":
                x.add_argument("--fault-id", type=lambda v: int(v, 0), help="the fault HighestAam reports (default 8305)")
    ca = fts.add_parser("cartridge", help="the cartridge change from the pump's side: ready, insert")
    cs = ca.add_subparsers(dest="cartridge_action", required=True)
    cs.add_parser("ready", help="finish retracting now (after the app's Enter) instead of after cartridge_ready_seconds")
    ci = cs.add_parser("insert", help="put a new cartridge into a pump that is ready for it")
    ci.add_argument("--units", type=float, required=True, help="what it holds, at most 300")
    r = fts.add_parser("resume", help="resume delivery from the pump's side")
    r.add_argument("--keep-alarms", action="store_true", help="leave the pump's alarms raised")

    b = fts.add_parser("bolus", help="a bolus on the pump itself: start, stall, resume, abort, complete")
    b.add_argument("bolus_action", choices=["start", "stall", "resume", "abort", "complete"])
    b.add_argument("--units", type=float); b.add_argument("--source", choices=["pump", "remote"], help="pump (default) or an app's")
    b.add_argument("--duration-seconds", type=float, help="start: how long delivery takes"); b.add_argument("--rate", type=float, help="start: units per second")
    b.add_argument("--delivered-units", type=float, help="abort/complete: what was delivered")
    t = fts.add_parser("tempbasal", help="a temp rate set on the pump itself: start or stop")
    t.add_argument("temp_action", choices=["start", "stop"])
    t.add_argument("--percent", type=int); t.add_argument("--rate", type=float, help="U/hr instead of --percent")
    t.add_argument("--duration-minutes", type=int, help="default 30")

    lg = fts.add_parser("log", help="what the app sent the pump and the pump answered (GET /api/log)")
    lg.add_argument("--last", type=int, metavar="N"); lg.add_argument("--since", type=int, metavar="SEQ", help="entries after this seq")
    lg.add_argument("--message", metavar="REGEX", help="only messages (or faults) matching, case-insensitive")
    lg.add_argument("--kind", help="only this entry kind")
    lg.add_argument("--cargo", action="store_true", help="show each request's fields")
    lg.add_argument("--count", action="store_true", help="print only how many entries match")
    lg.add_argument("--json", action="store_true"); lg.add_argument("--clear", action="store_true", help="empty the log")

    tr = fts.add_parser("transport", help="the link's ATT MTU: show it, or --mtu N (23 = fragmented 20-byte notifications, 247 = a real phone)")
    tr.add_argument("--mtu", type=int)
