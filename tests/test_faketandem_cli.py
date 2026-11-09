import json
import unittest

from harness import ftcli as F
from harness.util import HarnessError
from tests.support import FakeFaketandem, STATE, HISTORY, patched_pump, th


class Fields(unittest.TestCase):
    def test_dotted_paths_and_indexes(self):
        self.assertEqual(F.get_field(STATE, "basal.temp_percent"), 150)
        self.assertEqual(F.get_field(STATE, "list.1.a"), 2)
        self.assertFalse(F.get_field(STATE, "basal.suspended") if "suspended" in STATE["basal"] else False)

    def test_missing_step_lists_the_keys(self):
        with self.assertRaises(HarnessError) as cm:
            F.get_field(STATE, "basal.nope")
        self.assertIn("temp_active", str(cm.exception))
        with self.assertRaises(HarnessError):
            F.get_field(STATE, "list.9")
        with self.assertRaises(HarnessError):
            F.get_field(STATE, "basal.temp_percent.x")

    def test_format_value(self):
        self.assertEqual([F.format_value(v) for v in (True, False, None, 1.5, "a")], ["true", "false", "null", "1.5", "a"])
        self.assertEqual(json.loads(F.format_value({"a": 1})), {"a": 1})


class History(unittest.TestCase):
    def test_default_hides_basal_cycles(self):
        self.assertEqual([e["sequence"] for e in F.filter_history(HISTORY)], [1, 4])

    def test_type_is_case_insensitive_and_last_takes_the_tail(self):
        got = F.filter_history(HISTORY, types=F.parse_types(["basaldelivery"]))
        self.assertEqual([e["sequence"] for e in got], [2, 3])
        self.assertEqual([e["sequence"] for e in F.filter_history(HISTORY, types={"basaldelivery"}, last=1)], [3])
        self.assertEqual(F.parse_types(["BolusCompleted,TempRateActivated", "x"]), {"boluscompleted", "temprateactivated", "x"})

    def test_since_sequence_and_iso(self):
        self.assertEqual([e["sequence"] for e in F.filter_history(HISTORY, types={"basaldelivery"}, since="3")], [3])
        got = F.filter_history(HISTORY, types={"basaldelivery"}, since="2026-01-01T00:07:00+00:00")
        self.assertEqual([e["sequence"] for e in got], [3])


class Paths(unittest.TestCase):
    def test_filltubing_spellings(self):
        for p in ("/api/state/fill_tubing/button", "/api/state/fill-tubing/button", "api/state/FillTubing/button",
                  "/api/state/filltubing/button/"):
            self.assertEqual(F.normalise_path(p), "/api/state/filltubing/button", p)

    def test_other_actions_and_untouched_paths(self):
        self.assertEqual(F.normalise_path("/api/state/temp_basal/start"), "/api/state/tempbasal/start")
        self.assertEqual(F.normalise_path("/api/state/qualifying_event"), "/api/state/qualifyingevent")
        self.assertEqual(F.normalise_path("/api/state/history/bulk?x=1"), "/api/state/history/bulk?x=1")
        self.assertEqual(F.normalise_path("/api/state"), "/api/state")
        self.assertEqual(F.normalise_path("/api/faults"), "/api/faults")

    def test_settings_names_take_the_servers_case(self):
        names = lambda: ["PumpVersionRequest", "CurrentBolusStatusRequest"]
        self.assertEqual(F.normalise_path("/api/settings/pumpversionrequest", names), "/api/settings/PumpVersionRequest")
        self.assertEqual(F.normalise_path("/api/settings/PUMPVERSIONREQUEST/reset", names),
                         "/api/settings/PumpVersionRequest/reset")
        self.assertEqual(F.normalise_path("/api/settings/Unknown", names), "/api/settings/Unknown")

    def test_errors_gain_the_missing_lists(self):
        e = F.enrich_error("PUT", "/api/settings/X", HarnessError("faketandem PUT -> 400: unknown response mode: foo"))
        self.assertIn("constant, incremental, time_based", str(e))
        e = F.enrich_error("POST", "/api/state/nope", HarnessError("faketandem POST /api/state/nope -> 404: not found"))
        self.assertIn("filltubing/button", str(e))


class Builders(unittest.TestCase):
    def test_fault(self):
        self.assertEqual(F.fault_request("drop_response", message="InitiateBolusRequest"),
                         ("POST", "/api/faults", {"kind": "drop_response", "message": "InitiateBolusRequest"}))
        _, _, b = F.fault_request("delay_response", message="HistoryLogStatusRequest", delay_ms=8000, every=True)
        self.assertEqual(b, {"kind": "delay_response", "message": "HistoryLogStatusRequest", "delay_ms": 8000, "every": True})
        _, _, b = F.fault_request("drop_fragment", index=1, every=True, characteristic="CurrentStatus")
        self.assertEqual(b["index"], 1)
        with self.assertRaises(HarnessError):
            F.fault_request("delay_response")
        with self.assertRaises(HarnessError):
            F.fault_request("explode")

    def test_clock(self):
        self.assertEqual(F.clock_requests(), [("GET", "/api/clock", None)])
        self.assertEqual(F.clock_requests(offset_seconds=90000), [("PUT", "/api/clock", {"pump_offset_seconds": 90000})])
        self.assertEqual(F.clock_requests(zone="America/New_York"), [("PUT", "/api/clock", {"pump_timezone": "America/New_York"})])
        r = F.clock_requests(set_time="2026-03-08T06:55:00Z", advance_seconds=600, advance_ticks=6)
        self.assertEqual(r[0], ("PUT", "/api/clock", {"mode": "manual", "now": "2026-03-08T06:55:00+00:00"}))
        self.assertEqual(r[1], ("POST", "/api/clock/advance", {"seconds": 600, "ticks": 6}))
        self.assertEqual(F.clock_requests(pump_local_time="2026-03-08T01:55:00"),
                         [("POST", "/api/state/time", {"local_time": "2026-03-08T01:55:00"})])
        self.assertEqual(F.clock_requests(shift_pump_time=-10800), [("POST", "/api/state/time", {"delta_seconds": -10800})])
        with self.assertRaises(HarnessError):
            F.clock_requests(set_time="tomorrow")

    def test_assignments_are_json_typed(self):
        self.assertEqual(F.parse_assignments(["reservoir_units=42.5", "closed_loop_enabled=true", "workflow_mode=fill_tubing",
                                              'idp_profiles=[{"id":1}]', "pairing_code=123456"]),
                         {"reservoir_units": 42.5, "closed_loop_enabled": True, "workflow_mode": "fill_tubing",
                          "idp_profiles": [{"id": 1}], "pairing_code": 123456})
        for bad in ([], ["x"], ["=1"]):
            with self.assertRaises(HarnessError):
                F.parse_assignments(bad)

    def test_suspend_bolus_tempbasal(self):
        self.assertEqual(F.suspend_request("alarm", True), ("POST", "/api/state/suspend", {"reason": "alarm", "keep_temp": True}))
        self.assertEqual(F.suspend_request(None, True)[2], {"reason": "alarm", "keep_temp": True})
        with self.assertRaises(HarnessError):
            F.suspend_request("occlusion", True)
        self.assertEqual(F.bolus_request("start", units=1.5, source="remote"),
                         ("POST", "/api/state/bolus/start", {"units": 1.5, "source": "remote"}))
        self.assertEqual(F.bolus_request("abort", delivered_units=0.4), ("POST", "/api/state/bolus/abort", {"delivered_units": 0.4}))
        with self.assertRaises(HarnessError):
            F.bolus_request("start")
        self.assertEqual(F.tempbasal_request("start", percent=150, duration_minutes=45)[2], {"percent": 150, "duration_minutes": 45})
        self.assertEqual(F.tempbasal_request("start", rate=1.2)[2], {"rate": 1.2, "duration_minutes": 30})
        with self.assertRaises(HarnessError):
            F.tempbasal_request("start")


class AgainstAFakePump(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFaketandem()
        self.addCleanup(self.fake.close)
        p = patched_pump(self.fake)
        p.__enter__()
        self.addCleanup(p.__exit__, None, None, None)

    def test_status_field(self):
        rc, out, _ = th("faketandem", "status", "--json", "--field", "basal.temp_percent")
        self.assertEqual((rc, out.strip()), (0, "150"))
        rc, out, _ = th("faketandem", "status", "--field", "connection.connected")
        self.assertEqual(out.strip(), "true")
        rc, _, err = th("faketandem", "status", "--field", "basal.zzz")
        self.assertEqual(rc, 2)
        self.assertIn("temp_rate", err)

    def test_api_field_and_path_normalisation(self):
        rc, out, _ = th("faketandem", "api", "GET", "/api/state", "--field", "insulin.reservoir_units")
        self.assertEqual(out.strip(), "120.5")
        rc, out, err = th("faketandem", "api", "POST", "/api/state/fill_tubing/button", '{"hold_seconds": 3}')
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/filltubing/button", {"hold_seconds": 3}))
        self.assertIn("filltubing", err)
        rc, _, _ = th("faketandem", "api", "GET", "/api/settings/pumpversionrequest")
        self.assertEqual(self.fake.requests[-1][1], "/api/settings/PumpVersionRequest")

    def test_api_body_not_json(self):
        rc, _, err = th("faketandem", "api", "POST", "/api/state/suspend", "{reason: x}")
        self.assertEqual(rc, 2)
        self.assertIn("not JSON", err)

    def test_history_filters(self):
        rc, out, _ = th("faketandem", "history", "--type", "BasalDelivery", "--last", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines(), ["#3 2026-01-01T00:10:00Z BasalDelivery {\"rate\": 900}"])
        rc, out, _ = th("faketandem", "history", "--type", "basaldelivery", "--count")
        self.assertEqual(out.strip(), "2")
        rc, out, _ = th("faketandem", "history", "--type", "TempRateActivated", "--json")
        self.assertEqual(json.loads(out)[0]["data"], {"percent": 150})
        rc, out, _ = th("faketandem", "history")
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_fault_add_and_clear(self):
        rc, out, _ = th("faketandem", "fault", "add", "drop_response", "--message", "InitiateBolusRequest")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/faults", {"kind": "drop_response", "message": "InitiateBolusRequest"}))
        self.assertIn("armed drop_response #7 on InitiateBolusRequest", out)
        th("faketandem", "fault", "add", "delay_response", "--message", "HistoryLogStatusRequest", "--delay-ms", "8000", "--every")
        self.assertEqual(self.fake.writes()[-1][2]["every"], True)
        th("faketandem", "fault", "add", "error_response", "--message", "InitiateBolusRequest", "--error-code", "3", "--count", "2")
        self.assertEqual(self.fake.writes()[-1][2]["error_code"], 3)
        rc, out, _ = th("faketandem", "fault", "add", "radio_off")
        self.assertIn("radio off", out)
        rc, out, _ = th("faketandem", "fault", "clear")
        self.assertEqual(self.fake.writes()[-1][:2], ("DELETE", "/api/faults"))
        self.assertIn("cleared 2", out)
        th("faketandem", "fault", "clear", "--id", "7")
        self.assertEqual(self.fake.writes()[-1][:2], ("DELETE", "/api/faults/7"))
        rc, _, err = th("faketandem", "fault", "add", "delay_response")
        self.assertEqual(rc, 2)

    def test_clock(self):
        rc, out, _ = th("faketandem", "clock", "--offset-seconds", "90000")
        self.assertEqual(self.fake.writes()[-1], ("PUT", "/api/clock", {"pump_offset_seconds": 90000.0}))
        self.assertIn("clock: mode", out)
        th("faketandem", "clock", "--zone", "America/New_York")
        self.assertEqual(self.fake.writes()[-1][2], {"pump_timezone": "America/New_York"})
        rc, out, _ = th("faketandem", "clock", "--advance-seconds", "600", "--advance-ticks", "6")
        w = self.fake.writes()
        self.assertEqual(w[-2], ("PUT", "/api/clock", {"mode": "manual"}))
        self.assertEqual(w[-1], ("POST", "/api/clock/advance", {"seconds": 600.0, "ticks": 6}))
        th("faketandem", "clock", "--shift-pump-time", "-3600")
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/time", {"delta_seconds": -3600.0}))
        n = len(self.fake.writes())
        th("faketandem", "clock")
        self.assertEqual(len(self.fake.writes()), n)

    def test_set(self):
        rc, out, _ = th("faketandem", "set", "reservoir_units=5", "battery_percent=10", "closed_loop_enabled=true")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.writes()[-1], ("PUT", "/api/state", {"reservoir_units": 5, "battery_percent": 10,
                                                                         "closed_loop_enabled": True}))
        self.assertIn("set reservoir_units=5", out)
        self.assertIn("reservoir 120.5 U", out)
        self.assertEqual(th("faketandem", "set", "nokey")[0], 2)

    def test_suspend_resume_bolus_tempbasal(self):
        th("faketandem", "suspend", "--reason", "alarm", "--keep-temp")
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/suspend", {"reason": "alarm", "keep_temp": True}))
        th("faketandem", "suspend")
        self.assertEqual(self.fake.writes()[-1][2], {})
        th("faketandem", "resume", "--keep-alarms")
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/resume", {"clear_alarms": False}))
        th("faketandem", "bolus", "start", "--units", "1.5")
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/bolus/start", {"units": 1.5}))
        for a in ("stall", "resume", "abort", "complete"):
            th("faketandem", "bolus", a)
            self.assertEqual(self.fake.writes()[-1][1], f"/api/state/bolus/{a}")
        th("faketandem", "tempbasal", "start", "--percent", "150", "--duration-minutes", "30")
        self.assertEqual(self.fake.writes()[-1], ("POST", "/api/state/tempbasal/start", {"percent": 150, "duration_minutes": 30}))
        th("faketandem", "tempbasal", "stop")
        self.assertEqual(self.fake.writes()[-1][1], "/api/state/tempbasal/stop")

    def test_log_and_transport(self):
        rc, out, _ = th("faketandem", "log", "--last", "5")
        self.assertIn("InitiateBolusResponse", out)
        self.assertIn("[fault drop_response]", out)
        rc, out, _ = th("faketandem", "log", "--message", "response$", "--count")
        self.assertEqual(out.strip(), "1")
        th("faketandem", "log", "--since", "1")
        self.assertEqual(self.fake.requests[-1][1], "/api/log?since=1")
        th("faketandem", "transport", "--mtu", "23")
        self.assertEqual(self.fake.writes()[-1], ("PUT", "/api/transport", {"att_mtu": 23}))

    def test_server_errors_are_enriched(self):
        self.fake.fail[("POST", "/api/state/nope")] = (404, "404 page not found")
        rc, _, err = th("faketandem", "api", "POST", "/api/state/nope")
        self.assertEqual(rc, 2)
        self.assertIn("filltubing/button", err)


if __name__ == "__main__":
    unittest.main()
