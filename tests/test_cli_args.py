import unittest
from unittest import mock

from harness import cli
from harness.util import HarnessError
from tests.support import th


def parse(*argv):
    return cli.build_parser().parse_args(cli.hoist_global_options(list(argv)))


class GlobalOptions(unittest.TestCase):
    def test_target_before_and_after_the_subcommand(self):
        for argv in (["--target", "sim", "faketandem", "status"], ["faketandem", "status", "--target", "sim"],
                     ["faketandem", "--target", "sim", "status"], ["--target=sim", "faketandem", "status"],
                     ["faketandem", "status", "--target=sim"]):
            a = parse(*argv)
            self.assertEqual((a.target, a.cmd, a.sub), ("sim", "faketandem", "status"), argv)

    def test_app_and_git_protocol_move_too(self):
        a = parse("logs", "trio", "--app", "loop", "--git-protocol", "https", "--target", "sim")
        self.assertEqual((a.app, a.git_protocol, a.target, a.which), ("loop", "https", "sim", ["trio"]))

    def test_app_path_is_not_the_global_app(self):
        a = parse("install", "--app-path", "/x.app")
        self.assertEqual((a.app, a.app_path), (None, "/x.app"))

    def test_nothing_after_double_dash_moves(self):
        self.assertEqual(cli.hoist_global_options(["type", "--", "--target"]), ["type", "--", "--target"])

    def test_missing_value(self):
        with self.assertRaises(HarnessError):
            cli.hoist_global_options(["shot", "--target"])
        with self.assertRaises(HarnessError):
            cli.hoist_global_options(["shot", "--target", "--name", "x"])

    def test_values_stay_in_place(self):
        argv = ["faketandem", "api", "POST", "/api/state/bolus/start", '{"units": 1}', "--target", "sim"]
        self.assertEqual(cli.hoist_global_options(argv), ["--target", "sim", *argv[:-2]])

    def test_help_still_works(self):
        rc, out, _ = th("--help")
        self.assertEqual(rc, 0)
        self.assertIn("accepted before or after", out)
        self.assertIn("faketandem", out)


class SimDefaultsType(unittest.TestCase):
    def test_dash_values_are_accepted(self):
        for t, want in (("-string", "-string"), ("string", "-string"), ("-bool", "-bool"), ("boolean", "-bool"), ("int", "-int")):
            self.assertEqual(parse("sim", "defaults", "k", "v", "--type", t).type, want)

    def test_missing_value_is_clear(self):
        rc, _, err = th("sim", "defaults", "k", "v", "--type")
        self.assertEqual(rc, 2)
        self.assertIn("--type needs a value", err)

    def test_bad_value_is_clear(self):
        rc, _, err = th("sim", "defaults", "k", "v", "--type", "banana")
        self.assertEqual(rc, 2)
        self.assertIn("string, int, float or bool", err)


class KeyCount(unittest.TestCase):
    def test_key_pressed_n_times(self):
        drv = mock.Mock()
        with mock.patch.object(cli, "get_driver", return_value=drv), mock.patch("time.sleep"):
            rc, _, _ = th("key", "backspace", "--count", "12")
            self.assertEqual(rc, 0)
            self.assertEqual(drv.key.call_args_list, [mock.call("backspace")] * 12)
            drv.reset_mock()
            th("key", "return")
            self.assertEqual(drv.key.call_count, 1)

    def test_count_must_be_positive(self):
        self.assertEqual(th("key", "backspace", "--count", "0")[0], 2)

    def test_tap_count_still_there(self):
        self.assertEqual(parse("tap", "1", "2", "--count", "2").count, 2)


class LogsArgs(unittest.TestCase):
    def test_several_logs_and_level_alias(self):
        a = parse("logs", "tandem", "trio", "--level", "warn", "--count")
        self.assertEqual((a.which, a.level, a.count), (["tandem", "trio"], "warning", True))
        self.assertEqual(parse("logs").which, [])
        self.assertEqual(parse("logs", "trio,tandem").which, ["trio,tandem"])


if __name__ == "__main__":
    unittest.main()
