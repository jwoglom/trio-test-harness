import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import rounds as R
from harness import scenarioscope as S
from harness.report import Run
from harness.util import HarnessError
from tests.support import th


def make_run(tmp: str, plan: list[dict] | None, current: int = 0) -> Run:
    run = Run(Path(tmp))
    meta = {"name": "t", "status": "passed"}
    if plan is not None:
        meta["rounds"] = {"plan": plan, "current": current, "by": "option", "reason": "",
                          "args": {"mode": "simulator", "target": "sim", "onboarding": "preseed", "pump": "faketandem",
                                   "pump_setup": "preseed", "watch": False, "no_smoke": False, "no_cgm_sim": False,
                                   "no_pump_sim": False, "faketandem_ref": None}}
    run.update_meta(**meta)
    return run


PLAN = [{"scenario": "pump-reset", "setup": "walk"}, {"scenario": "recent-doses", "setup": "preseed"},
        {"scenario": "clock-73h-ahead", "setup": "walk"}]


class Parse(unittest.TestCase):
    def test_one_several_auto_and_none(self):
        self.assertIsNone(R.parse(None))
        self.assertIsNone(R.parse("none"))
        self.assertEqual(R.parse("recent-doses"), ["recent-doses"])
        self.assertEqual(R.parse(" pump-reset , clock-fall-back "), ["pump-reset", "clock-fall-back"])
        self.assertEqual(R.parse("auto"), "auto")

    def test_refuses_typos_repeats_too_many_and_auto_in_a_list(self):
        for bad in ("pump-rest", "pump-reset,pump-reset", "auto,pump-reset",
                    ",".join(["recent-doses", "pump-reset", "app-doses", "clock-fall-back", "clock-set-back",
                              "pump-boluses", "day-of-use"])):
            with self.subTest(bad), self.assertRaises(HarnessError):
                R.parse(bad)

    def test_plan_walks_the_history_presets_only_when_asked(self):
        names = ["pump-reset", "recent-doses"]
        self.assertEqual([r["setup"] for r in R.plan(names, "preseed")], ["preseed", "preseed"])
        self.assertEqual([r["setup"] for r in R.plan(names, "preseed", walk_history=True)], ["walk", "preseed"])
        self.assertEqual(len(R.preseed_warnings(R.plan(names, "preseed"))), 1)


class State(unittest.TestCase):
    def test_a_run_of_one_scenario_has_no_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, [PLAN[0]])
            self.assertIsNone(R.state(tmp))
            self.assertEqual(R.count(tmp), 1)
            self.assertEqual(R.agent_notes(tmp), "")
            self.assertEqual(R.faketandem_save_dir(tmp), Path(tmp) / "faketandem")

    def test_rounds_count_and_save_into_the_current_rounds_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN, current=1)
            self.assertEqual(R.count(tmp), 3)
            self.assertEqual(R.faketandem_save_dir(tmp), Path(tmp) / "faketandem" / "2-recent-doses")

    def test_notes_name_the_round_and_what_comes_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN, current=0)
            job = R.agent_notes(tmp)
            self.assertIn("scenario 1 of 3: pump-reset", job.lower())
            self.assertIn("end your session", job)
            self.assertIn("run finish", job)
            self.assertIn("do not restate the others", job)
            self.assertIn("rounds next", R.agent_notes(tmp, job=False))
            make_run(tmp, PLAN, current=2)
            last = R.agent_notes(tmp)
            self.assertIn("last scenario", last)
            self.assertIn("run finish", last)
            self.assertNotIn("summing up every scenario", last)

    def test_finish_keeps_the_worst_verdict_and_lists_each_round_on_a_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(tmp, PLAN, current=0)
            R.finish(run, "failed", "reset bolus reported twice")
            m = run.meta
            m["rounds"]["current"] = 1
            run.update_meta(rounds=m["rounds"])
            R.finish(run, "passed", "fine\nreally")
            meta = Run(Path(tmp)).meta
            self.assertEqual(meta["status"], "failed")
            self.assertEqual(meta["summary"].splitlines(), [
                "Pump scenarios:",
                "- **1. pump-reset**: failed. reset bolus reported twice",
                "- **2. recent-doses**: passed. fine really",
            ])
            self.assertEqual(meta["rounds"]["plan"][0]["result"]["status"], "failed")

    def test_a_round_without_a_verdict_is_listed_and_makes_the_run_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(tmp, PLAN, current=1)
            R.finish(run, "passed", "fine")
            meta = Run(Path(tmp)).meta
            self.assertEqual(meta["status"], "partial")
            self.assertIn("- **1. pump-reset**: no verdict recorded", meta["summary"])
            self.assertNotIn("clock-73h-ahead", meta["summary"])

    def test_settling_at_the_end_lists_rounds_that_never_ran(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = [dict(PLAN[0], result={"status": "passed", "summary": "ok"}), PLAN[1], PLAN[2]]
            run = make_run(tmp, plan, current=1)
            R.settle(run, final=True)
            lines = Run(Path(tmp)).meta["summary"].splitlines()
            self.assertEqual(lines[1:], ["- **1. pump-reset**: passed. ok",
                                         "- **2. recent-doses**: no verdict recorded",
                                         "- **3. clock-73h-ahead**: not run"])
            self.assertEqual(Run(Path(tmp)).meta["status"], "partial")

    def test_rounds_summaries_settles_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = [dict(x, result={"status": "passed", "summary": x["scenario"]}) for x in PLAN]
            make_run(tmp, plan, current=2)
            rc, _, _ = th("rounds", "summaries", "--run", tmp)
            self.assertEqual(rc, 0)
            meta = Run(Path(tmp)).meta
            self.assertEqual(meta["status"], "passed")
            self.assertEqual(len(meta["summary"].splitlines()), 4)

    def test_run_finish_goes_through_the_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN, current=0)
            rc, _, _ = th("run", "finish", "partial", "--summary", "half", "--run", tmp)
            self.assertEqual(rc, 0)
            self.assertEqual(Run(Path(tmp)).meta["rounds"]["plan"][0]["result"], {"status": "partial", "summary": "half"})

    def test_summaries_get_a_heading_each_and_note_a_round_that_did_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN)
            (Path(tmp) / "agent-summary-1.md").write_text("Works.")
            text = R.combine_summaries(tmp).read_text()
            self.assertIn("## Scenario 1: pump-reset\n\nWorks.", text)
            self.assertIn("## Scenario 3: clock-73h-ahead\n\n(no agent summary", text)


class NextRound(unittest.TestCase):
    def test_refused_inside_a_job_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN)
            with mock.patch.dict(os.environ, {"TH_JOB_AGENT": "1"}):
                rc, _, err = th("rounds", "next", "--run", tmp)
            self.assertEqual(rc, 2)
            self.assertIn("refused", err)

    def test_refuses_a_run_without_rounds_or_with_none_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, [PLAN[0]])
            with self.assertRaises(HarnessError):
                R.next_round(tmp)
            make_run(tmp, PLAN, current=2)
            with self.assertRaises(HarnessError):
                R.next_round(tmp)

    def test_saves_the_last_round_then_reinstalls_and_starts_the_next_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, PLAN, current=0)
            seen = {}

            def stop(save_to=None):
                seen["saved_to"] = R.faketandem_save_dir(save_to)
                return True

            def launch(args, bi, run):
                seen["args"] = args
                return "drv"

            with mock.patch("harness.faketandem.current", return_value={"pid": 1}), \
                    mock.patch("harness.faketandem.stop", side_effect=stop), \
                    mock.patch("harness.build.BuildInfo.load", return_value=argparse.Namespace(bundle_id="b")), \
                    mock.patch("harness.pipeline.stage_install") as install, \
                    mock.patch("harness.pipeline.stage_launch", side_effect=launch), \
                    mock.patch("harness.pipeline.stage_home"), \
                    mock.patch("harness.pipeline.stage_smoke"):
                r = R.next_round(tmp)
            self.assertEqual(r["scenario"], "recent-doses")
            self.assertEqual(seen["saved_to"], Path(tmp) / "faketandem" / "1-pump-reset")
            a = seen["args"]
            self.assertEqual((a.pump_scenario, a.pump_setup, a.reinstall, a.faketandem_reuse),
                             ("recent-doses", "preseed", True, {"pid": 1}))
            self.assertTrue(install.call_args.args[0].reinstall)
            run = Run(Path(tmp))
            self.assertEqual(run.meta["rounds"]["current"], 1)
            self.assertEqual(run.meta["status"], "passed")
            self.assertEqual([s["action"] for s in run.steps()][:1], ["round"])
            self.assertIn("Scenario 2 of 3: recent-doses", run.steps()[0]["note"])


class Auto(unittest.TestCase):
    def test_answer_keeps_presets_in_order_without_repeats_up_to_the_cap(self):
        out = S.parse_answer({"scenarios": ["pump-reset", "nope", "pump-reset", "clock-fall-back", "recent-doses",
                                            "app-doses", "clock-73h-ahead"], "reason": "r"})
        self.assertEqual(out["scenarios"], ["pump-reset", "clock-fall-back", "recent-doses", "app-doses"])
        self.assertIsNone(S.parse_answer({"scenarios": ["nope"]}))

    def test_paths_pick_history_presets_for_history_code(self):
        hist = S.by_paths([{"files": ["TandemKit/Sources/TandemKit/PumpManager/History/HistoryTimestampReconciler.swift"]}])
        self.assertEqual(hist["scenarios"], S.HISTORY_PICK)
        other = S.by_paths([{"files": ["Trio/Sources/Views/Home.swift"]}])
        self.assertEqual(other["scenarios"], S.GENERAL_PICK)
        from harness.faketandem import SCENARIOS
        self.assertLessEqual(set(S.HISTORY_PICK + S.GENERAL_PICK), set(SCENARIOS))

    def test_pipeline_turns_an_auto_pick_into_rounds_walking_the_history_ones(self):
        from harness import pipeline as P
        args = argparse.Namespace(pump_scenarios="auto", pump_setup="preseed", pump_scenario="auto", mode="simulator",
                                  target="sim", onboarding="preseed", pump="faketandem", watch=False)
        with mock.patch("harness.scenarioscope.decide",
                        return_value={"scenarios": ["pump-reset", "recent-doses"], "by": "classifier", "reason": "x"}), \
                mock.patch("harness.pipeline._app", return_value=argparse.Namespace(base_branch="dev", key="trio")):
            meta = P.decide_rounds(args, {}, Path("."))
        self.assertEqual(meta["plan"], [{"scenario": "pump-reset", "setup": "walk"},
                                        {"scenario": "recent-doses", "setup": "preseed"}])
        self.assertEqual((args.pump_scenario, args.pump_setup), ("pump-reset", "walk"))
        self.assertEqual(meta["args"]["pump_setup"], "walk")

    def test_pipeline_keeps_a_single_scenario_without_rounds(self):
        from harness import pipeline as P
        args = argparse.Namespace(pump_scenarios=["recent-doses"], pump_setup="preseed", pump_scenario="recent-doses")
        self.assertIsNone(P.decide_rounds(args, {}, Path(".")))
        self.assertEqual(args.pump_scenario, "recent-doses")


class CI(unittest.TestCase):
    def test_trio_test_ci_sends_a_list_as_pump_scenarios(self):
        text = Path(__file__).resolve().parents[1].joinpath("trio-test-ci").read_text()
        self.assertIn('f["pump_scenarios"] = a.pump_scenario', text)

    def test_the_workflow_has_the_inputs_the_rounds_need(self):
        wf = Path(__file__).resolve().parents[1].joinpath(".github", "workflows", "e2e.yml").read_text()
        self.assertIn("pump_scenarios:", wf)
        self.assertIn("rounds next --run", wf)
        self.assertIn("agent_timeout", wf)


if __name__ == "__main__":
    unittest.main()
