import unittest
from unittest import mock

from harness import cli, waituntil as W
from harness.util import HarnessError
from tests.support import th


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class WaitUntilLoop(unittest.TestCase):
    def test_matches_on_a_later_run_and_returns_the_line(self):
        outs = iter(["connected: false", "connected: false", "x\nconnected: true\ny"])
        c = Clock()
        res = W.wait_until(lambda: (0, next(outs), ""), r"connected: true", timeout=60, every=5, sleep=c.sleep, clock=c.now)
        self.assertEqual(res.line, "connected: true")
        self.assertEqual(res.attempts, 3)
        self.assertEqual(c.t, 10)

    def test_timeout_runs_at_least_once_and_never_past_the_deadline(self):
        c = Clock()
        calls = []
        res = W.wait_until(lambda: (calls.append(c.t) or (1, "nope", "boom")), "never", timeout=12, every=5,
                           sleep=c.sleep, clock=c.now)
        self.assertIsNone(res.line)
        self.assertEqual(calls, [0, 5, 10, 12])
        self.assertEqual(res.last_error, "boom")

    def test_bad_regex_is_a_harness_error(self):
        with self.assertRaises(HarnessError):
            W.wait_until(lambda: (0, "", ""), "(", timeout=1, every=1)

    def test_ignore_case_and_progress(self):
        c = Clock()
        seen = []
        outs = iter(["a"] * 7 + ["READY"])
        res = W.wait_until(lambda: (0, next(outs), ""), "ready", timeout=100, every=5, ignore_case=True,
                           sleep=c.sleep, clock=c.now, progress=seen.append)
        self.assertEqual(res.line, "READY")
        self.assertTrue(seen)

    def test_split_command_drops_the_program_name(self):
        self.assertEqual(W.split_command('./trio-test-harness logs trio --grep "a b"'), ["logs", "trio", "--grep", "a b"])
        with self.assertRaises(HarnessError):
            W.split_command("  ")


class WaitCommand(unittest.TestCase):
    def stub_logs(self, states):
        it = iter(states)

        def fake(args):
            print(next(it))
            return 0
        return mock.patch.object(cli, "cmd_logs", fake)

    def test_until_polls_the_subcommand_in_process(self):
        with self.stub_logs(["state: 1", "state: 2", "state: 3"]):
            rc, out, err = th("wait", "--until", "logs trio --since 2m", "--matches", "state: 3", "--every", "0.01")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines()[-1], "state: 3")
        self.assertIn("matched after", err)

    def test_timeout_exits_1_with_the_last_output(self):
        with self.stub_logs(["state: 1"] * 100):
            rc, out, err = th("wait", "--until", "logs trio", "--matches", "never", "--timeout", "0.05", "--every", "0.01")
        self.assertEqual(rc, 1)
        self.assertIn("state: 1", err)

    def test_the_cap_applies(self):
        rc, _, err = th("wait", "--until", "logs trio", "--matches", "x", "--timeout", "281")
        self.assertEqual(rc, 2)
        self.assertIn("280", err)

    def test_plain_wait_and_its_errors_remain(self):
        rc, _, err = th("wait", "0")
        self.assertEqual(rc, 2)
        rc, _, err = th("wait")
        self.assertEqual(rc, 2)
        rc, _, err = th("wait", "5", "--until", "logs", "--matches", "x")
        self.assertEqual(rc, 2)
        with mock.patch("time.sleep") as sl:
            rc, out, _ = th("wait", "1")
        self.assertEqual(rc, 0)
        self.assertIn("waited 1 of 1 s", out)

    def test_cannot_poll_wait_itself(self):
        rc, _, err = th("wait", "--until", "wait 5", "--matches", "x")
        self.assertEqual(rc, 2)

    def test_the_outer_target_is_carried_to_the_command(self):
        seen = []

        def fake(args):
            seen.append(args.target)
            print("ok")
            return 0
        with mock.patch.object(cli, "cmd_logs", fake):
            th("wait", "--target", "sim", "--until", "logs trio", "--matches", "ok")
        self.assertEqual(seen, ["sim"])


if __name__ == "__main__":
    unittest.main()
