import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import cli, versionmock as VM
from harness.util import HarnessError

SCENARIOS = Path(VM.__file__).parent / "scenarios" / "versionmock-branch-sha.json"


def parse(*argv):
    return cli.build_parser().parse_args(cli.hoist_global_options(list(argv)))


class TempState(unittest.TestCase):
    """Points the module's state and config folders at a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in (("WORK_DIR", self.tmp / "vm"), ("ACTIVE_DIR", self.tmp / "vm" / "active"),
                            ("STATE_FILE", self.tmp / "versionmock.json")):
            patcher = mock.patch.object(VM, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class BuildConfig(unittest.TestCase):
    def test_build_overrides(self):
        cfg, files = VM.build_config({"version": "0.6.0.12", "build_number": "42", "branch": "dev", "sha": "a1b2c3d",
                                      "build_details": {"com-trio-build-date": "x"}}, token="t")
        self.assertEqual(cfg["info"], {"CFBundleShortVersionString": "0.6.0.12", "AppDevVersion": "0.6.0.12",
                                       "CFBundleVersion": "42"})
        dev, _ = VM.build_config({"version": "1.0.2", "dev_version": "1.0.2.83"})
        self.assertEqual(dev["info"], {"CFBundleShortVersionString": "1.0.2", "AppDevVersion": "1.0.2.83"})
        self.assertEqual(cfg["plists"], {"BuildDetails.plist": {"com-trio-build-date": "x", "com-trio-branch": "dev",
                                                                "com-trio-commit-sha": "a1b2c3d"}})
        self.assertEqual((cfg["reset_token"], cfg["reset_every_launch"]), ("t", False))
        self.assertEqual(cfg["reset_defaults"], VM.CACHE_KEYS)
        self.assertEqual((cfg["routes"], files), ([], {}))

    def test_empty_branch_and_sha_remove_the_keys(self):
        cfg, _ = VM.build_config({"branch": "", "sha": ""})
        self.assertEqual(cfg["plists"]["BuildDetails.plist"], {"com-trio-branch": None, "com-trio-commit-sha": None})
        cfg, _ = VM.build_config({})
        self.assertEqual(cfg["plists"], {})

    def test_routes(self):
        bl = VM.blacklist_json([{"version": "0.5.0"}])
        cfg, files = VM.build_config({"blacklist": bl, "blacklist_status": 404, "latest": "1.1.0",
                                      "routes": [{"url": "https://x/*", "body_text": "hi"}]})
        by_url = {r["url"]: r for r in cfg["routes"]}
        self.assertEqual(by_url[VM.URLS["blacklist"]], {"url": VM.URLS["blacklist"], "status": 404,
                                                        "body_file": "bodies/blacklist"})
        self.assertEqual(files["bodies/blacklist"], bl)
        self.assertIn("APP_VERSION = 1.1.0", files["bodies/config"])
        self.assertIn("APP_DEV_VERSION = 1.1.0", files["bodies/dev-config"])
        self.assertEqual(by_url["https://x/*"]["body_file"], "bodies/route-0")
        self.assertEqual(files["bodies/route-0"], "hi")

    def test_fail(self):
        cfg, files = VM.build_config({"blacklist": "{}", "fail": ["blacklist", "config"]})
        self.assertEqual(cfg["routes"], [{"url": VM.URLS["blacklist"], "error": "offline"},
                                         {"url": VM.URLS["config"], "error": "offline"}])
        self.assertEqual(files, {})
        with self.assertRaises(HarnessError):
            VM.build_config({"fail": ["nope"]})

    def test_nothing_mocked_leaves_the_network_alone(self):
        cfg, _ = VM.build_config({"version": "1.0"})
        self.assertEqual(cfg["routes"], [])

    def test_entries_from(self):
        self.assertEqual(json.loads(VM.entries_from([{"version": "1"}])), {"blacklistedVersions": [{"version": "1"}]})
        self.assertEqual(VM.entries_from("{broken"), "{broken")
        with self.assertRaises(HarnessError):
            VM.entries_from("no-such-file.json")


class CliSpec(unittest.TestCase):
    def spec(self, *argv):
        return cli.versionmock_spec(parse("versionmock", "set", *argv))

    def test_version_defaults_latest_to_itself(self):
        self.assertEqual(self.spec("--version", "0.5.0"), {"version": "0.5.0", "latest": "0.5.0"})
        self.assertEqual(self.spec("--version", "0.5.0", "--real-config"), {"version": "0.5.0"})
        self.assertEqual(self.spec("--version", "0.5.0", "--latest", "1.0")["latest"], "1.0")

    def test_entries_and_details(self):
        s = self.spec("--branch", "dev", "--sha", "", "--build-detail", "com-trio-build-date=", "--info", "A=b",
                      "--blacklist-entry", '{"version": "", "sha": "a1b2c3d", "message": "m"}', "--fail", "dev-config")
        self.assertEqual((s["branch"], s["sha"], s["build_details"], s["info"], s["fail"]),
                         ("dev", "", {"com-trio-build-date": None}, {"A": "b"}, ["dev-config"]))
        self.assertEqual(json.loads(s["blacklist"])["blacklistedVersions"][0]["sha"], "a1b2c3d")

    def test_bad_input(self):
        with self.assertRaises(HarnessError):
            self.spec("--blacklist-entry", "{nope")
        with self.assertRaises(HarnessError):
            self.spec("--info", "novalue")
        with self.assertRaises(HarnessError):
            self.spec("--route", "https://x", "neither-file-nor-keyword")

    def test_routes(self):
        s = self.spec("--route", "https://x/*", "status:503", "--route", "https://y", "offline")
        self.assertEqual(s["routes"], [{"url": "https://x/*", "status": 503, "body": ""},
                                       {"url": "https://y", "error": "offline"}])


class Verdict(unittest.TestCase):
    STD = VM.BLACKLIST_TEXT

    def alert(self, msg, kind="update-required"):
        return {"kind": kind, "title": "Update Required", "message": msg}

    def test_kinds(self):
        self.assertTrue(VM.verdict({"alert": "none"}, None)[0])
        self.assertFalse(VM.verdict({"alert": "none"}, self.alert(self.STD))[0])
        self.assertFalse(VM.verdict({"alert": "update-required"}, None)[0])
        self.assertFalse(VM.verdict({"alert": "update-available"}, self.alert(self.STD))[0])

    def test_messages(self):
        with_msg = self.alert(self.STD + "\n\nB: commit a1b2c3d, any version")
        self.assertTrue(VM.verdict({"alert": "update-required", "message": "B: commit a1b2c3d, any version"}, with_msg)[0])
        self.assertFalse(VM.verdict({"alert": "update-required", "message": "D: other"}, with_msg)[0])
        self.assertFalse(VM.verdict({"alert": "update-required", "message": None}, with_msg)[0])
        self.assertTrue(VM.verdict({"alert": "update-required", "message": None}, self.alert(self.STD))[0])
        self.assertTrue(VM.verdict({"alert": "update-required"}, with_msg)[0])      # message not checked


def reference_matches(entry, version, branch, sha):
    """nightscout/Trio#1798's VersionEntry.matches, for checking the scenario file's expectations."""
    def norm(v):
        v = v.strip() if isinstance(v, str) else None
        return v or None

    def crit(v):
        v = norm(v)
        return None if v in (None, "SKIP") else v
    ev, eb, es = crit(entry.get("version")), crit(entry.get("branch")), crit(entry.get("sha"))
    if not (ev or eb or es):
        return False
    if ev and ev != norm(version):
        return False
    if eb and eb != norm(branch):
        return False
    if es:
        cur = (norm(sha) or "").lower()
        es = es.lower()
        if not cur or min(len(es), len(cur)) < 7 or not (es.startswith(cur) or cur.startswith(es)):
            return False
    return True


class ScenarioFile(unittest.TestCase):
    def test_loads(self):
        data, scenarios = VM.load_scenarios(SCENARIOS)
        self.assertGreater(len(scenarios), 20)
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))
        for s in scenarios:
            self.assertTrue(set(VM.spec_of(s)) <= set(VM.SPEC_KEYS))

    def test_expectations_follow_the_matching_rules(self):
        _, scenarios = VM.load_scenarios(SCENARIOS)
        for s in scenarios:
            if s.get("fail") or s.get("blacklist_status") or s.get("reset") is False:
                continue
            with self.subTest(s["name"]):
                raw = json.loads(s["blacklist"])["blacklistedVersions"]
                entries = [e for e in raw if isinstance(e, dict) and isinstance(e.get("version"), str)]
                hits = [e for e in entries if reference_matches(e, s["version"], s.get("branch"), s.get("sha"))]
                newer = [int(x) for x in s["latest"].split(".")] > [int(x) for x in s["version"].split(".")]
                kind = "update-required" if hits else ("update-available" if newer else "none")
                self.assertEqual(s["expect"]["alert"], kind)
                if hits and "message" in s["expect"]:
                    msg = next((e["message"].strip() for e in hits if (e.get("message") or "").strip()), None)
                    self.assertEqual(s["expect"]["message"], msg)
                legacy_ok = all(isinstance(e, dict) and isinstance(e.get("version"), str) for e in raw)
                legacy_hit = legacy_ok and s["version"] in [e["version"] for e in raw]
                legacy = "update-required" if legacy_hit else ("update-available" if newer else "none")
                self.assertEqual(s["expect_legacy"]["alert"], legacy)

    def test_parts_cover_everything_once_and_keep_cache_pairs(self):
        _, scenarios = VM.load_scenarios(SCENARIOS)
        for n in (1, 3, 5, 7):
            parts = [VM.part(scenarios, f"{i}/{n}") for i in range(1, n + 1)]
            self.assertEqual([s["name"] for p in parts for s in p], [s["name"] for s in scenarios])
            for p in parts:
                self.assertFalse(p and p[0].get("reset") is False, f"{n} parts: a cache scenario opens a part")
                self.assertLessEqual(len(p), len(scenarios) // n + 2)
        with self.assertRaises(HarnessError):
            VM.part(scenarios, "0/3")
        with self.assertRaises(HarnessError):
            VM.part(scenarios, "x")

    def test_bad_expect(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"scenarios": [{"name": "x", "expect": {"alert": "maybe"}}]}, f)
        with self.assertRaises(HarnessError):
            VM.load_scenarios(Path(f.name))


class FakeSim:
    def __init__(self, root: Path, installed=True):
        self.root, self.installed = root, installed

    def is_installed(self, bundle_id):
        return self.installed

    def app_data_container(self, bundle_id):
        return self.root / bundle_id


class Simulator(TempState):
    def test_launch_env_only_for_the_mocked_app(self):
        drv = FakeSim(self.tmp / "containers")
        self.assertEqual(VM.simulator_launch_env(drv, "org.x.trio"), {})
        with mock.patch.object(VM, "shim_path", return_value=Path("/shim.dylib")):
            VM.activate({"version": "0.5.0", "blacklist": VM.blacklist_json([{"version": "0.5.0"}])},
                        target="sim", bundle_id="org.x.trio", drv=drv)
            self.assertEqual(VM.simulator_launch_env(drv, "org.x.trio"), {"DYLD_INSERT_LIBRARIES": "/shim.dylib"})
            self.assertEqual(VM.simulator_launch_env(drv, "com.apple.Preferences"), {})
        dest = drv.app_data_container("org.x.trio") / VM.CONTAINER_SUBDIR
        cfg = json.loads((dest / "config.json").read_text())
        self.assertEqual(cfg["info"]["CFBundleShortVersionString"], "0.5.0")
        self.assertTrue((dest / "bodies" / "blacklist").exists())
        (dest / "log.jsonl").write_text('{"event": "loaded"}\n{"event": "request", "url": "u", "status": 200}\nbad\n')
        self.assertEqual(VM.served(VM.simulator_log(drv, "org.x.trio")), [{"event": "request", "url": "u", "status": 200}])

    def test_token_kept_or_renewed(self):
        drv = FakeSim(self.tmp / "containers")
        with mock.patch.object(VM, "shim_path", return_value=Path("/shim.dylib")):
            first = VM.activate({}, target="sim", bundle_id="b", drv=drv)["token"]
            self.assertEqual(VM.activate({}, target="sim", bundle_id="b", drv=drv, token=first)["token"], first)
            self.assertNotEqual(VM.activate({}, target="sim", bundle_id="b", drv=drv)["token"], first)
            self.assertNotEqual(VM.new_token(), first)

    def test_not_installed_yet(self):
        drv = FakeSim(self.tmp / "containers", installed=False)
        with mock.patch.object(VM, "shim_path", return_value=Path("/shim.dylib")):
            VM.activate({"version": "1"}, target="sim", bundle_id="b", drv=drv)
        self.assertTrue((VM.ACTIVE_DIR / "config.json").exists())

    def test_clear(self):
        drv = FakeSim(self.tmp / "containers")
        with mock.patch.object(VM, "shim_path", return_value=Path("/shim.dylib")):
            VM.activate({"version": "1"}, target="sim", bundle_id="b", drv=drv)
        VM.deactivate(target="sim", drv=drv)
        self.assertIsNone(VM.current())
        self.assertFalse((drv.app_data_container("b") / VM.CONTAINER_SUBDIR).exists())

    def test_other_targets_refused(self):
        with self.assertRaises(HarnessError):
            VM.activate({}, target="iphone-usb", bundle_id="b")


class ReadAlert(unittest.TestCase):
    def node(self, label):
        from harness.driver import UINode
        return UINode(label, "", "", "StaticText", True, 0, 0, 10, 10)

    def test_from_tree(self):
        class D:
            def __init__(s, labels):
                s.nodes = [self.node(x) for x in labels]

            def ui_tree(s):
                return s.nodes
        msg = VM.BLACKLIST_TEXT + "\n\nB: x"
        self.assertEqual(VM.read_alert(D(["Update Required", msg, "OK"])),
                         {"kind": "update-required", "title": "Update Required", "message": msg})
        self.assertEqual(VM.read_alert(D(["Update Available", "A new version (1.1.0) is available. It is recommended "
                                          "to update.", "OK"]))["kind"], "update-available")
        self.assertIsNone(VM.read_alert(D(["Home", "Settings"])))


if __name__ == "__main__":
    unittest.main()
