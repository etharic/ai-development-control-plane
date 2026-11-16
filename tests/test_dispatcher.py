"""W-0015 dispatcher tests. Claude is never launched: the runner, GitHub and git inputs are fakes."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import dispatcher  # noqa: E402

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def item(iid, status="ready", assignee="claude", priority="2", depends_on="", h="aaa"):
    d = {"id": iid, "status": status, "assignee": assignee, "priority": priority, "title": f"t{iid}", "_hash": h}
    if depends_on:
        d["depends_on"] = depends_on
    return d


class Fake:
    def __init__(self):
        self.items, self.claims, self.prs, self.problems, self.mode = [], set(), [], [], "BALANCED"
        self.results, self.calls, self.now, self.net_error = [], [], T0, None

    def inputs(self, root):
        if self.net_error:
            raise RuntimeError(self.net_error)
        return {"items": list(self.items), "claims": set(self.claims)}

    def runner(self, prompt, root):
        self.calls.append(prompt)
        return self.results.pop(0) if self.results else {"is_error": False, "permission_denials": [],
                                                          "session_id": "s", "num_turns": 3, "usage": {}}


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "control").mkdir()
        shutil.copy(REPO / "control" / "projects.json", self.root / "control" / "projects.json")
        os.environ["CP_STATE_DIR"] = str(self.root / ".local")
        self.addCleanup(os.environ.pop, "CP_STATE_DIR", None)
        self.f = Fake()

    def make(self, pid_alive=lambda pid: True):
        f = self.f
        return dispatcher.Dispatcher(self.root, inputs=f.inputs, prs=lambda repo, root: list(f.prs),
                                     problems=lambda root: list(f.problems), mode=lambda root: f.mode,
                                     runner=f.runner, clock=lambda: f.now, pid_alive=pid_alive, log_events=False)

    def test_idle_for_an_hour_without_invoking_claude(self):
        self.f.items = [item("W-0001", assignee="owner"), item("W-0002", status="blocked"),
                        item("W-0003", depends_on="W-0001"), item("W-0004", status="done")]
        self.f.claims = set()
        self.f.prs = [{"number": 9, "headRefName": "claude/W-0003-x", "headRefOid": "c" * 40, "title": "mine"}]
        d = self.make()
        for _ in range(12):  # an hour at the 5-minute cadence
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(self.f.calls, [])

    def test_new_eligible_item_launches_exactly_once(self):
        self.f.items = [item("W-0001", status="done"), item("W-0005", depends_on="W-0001")]
        d = self.make()
        res = d.poll()
        self.assertEqual((res["action"], res["outcome"]), ("launched", "done"))
        for _ in range(5):
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(len(self.f.calls), 1)
        self.assertIn("W-0005", self.f.calls[0])
        self.assertIn("dontAsk", self.f.calls[0])

    def test_claimed_item_does_not_launch(self):
        self.f.items = [item("W-0005")]
        self.f.claims = {"W-0005"}
        self.assertEqual(self.make().poll()["action"], "idle")

    def test_changed_item_or_new_pr_head_relaunches_once(self):
        self.f.prs = [{"number": 5, "headRefName": "gpt/W-0015-x", "headRefOid": "a" * 40, "title": "t"}]
        d = self.make()
        self.assertEqual(d.poll()["action"], "launched")
        self.assertEqual(d.poll()["action"], "idle")
        self.f.prs[0]["headRefOid"] = "b" * 40  # GPT pushed again: one new review
        self.assertEqual(d.poll()["action"], "launched")
        self.assertEqual(len(self.f.calls), 2)

    def test_permission_block_is_recorded_and_not_relaunched(self):
        self.f.items = [item("W-0006")]
        self.f.results = [{"is_error": False, "permission_denials": [{"tool_name": "Bash", "tool_input": {}}],
                           "session_id": "s1"}]
        d = self.make()
        self.assertEqual(d.poll()["outcome"], "blocked")
        for _ in range(5):
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(len(self.f.calls), 1)
        entry = next(iter(d.load()["handled"].values()))
        self.assertEqual(entry["outcome"], "blocked")

    def test_restart_recovers_state(self):
        self.f.items = [item("W-0007")]
        self.make().poll()
        self.assertEqual(self.make().poll()["action"], "idle")  # fresh instance after "reboot"
        self.assertEqual(len(self.f.calls), 1)

    def test_crash_mid_run_never_relaunches(self):
        self.f.items = [item("W-0008")]

        def crash(prompt, root):
            raise KeyboardInterrupt  # process killed while Claude runs

        d = self.make()
        d.runner = crash
        with self.assertRaises(KeyboardInterrupt):
            d.poll()
        self.assertFalse((d.dir / "lock").exists())
        self.assertEqual(self.make().poll()["action"], "idle")
        self.assertEqual(next(iter(d.load()["handled"].values()))["outcome"], "launched")

    def test_single_flight_and_stale_lock_recovery(self):
        self.f.items = [item("W-0009")]
        d = self.make(pid_alive=lambda pid: True)
        (d.dir / "lock").write_text(json.dumps({"pid": 4242, "started": T0.isoformat()}), encoding="utf-8")
        self.assertEqual(d.poll()["action"], "busy")
        self.assertEqual(self.f.calls, [])
        dead = self.make(pid_alive=lambda pid: False)
        self.assertEqual(dead.poll()["action"], "launched")  # dead owner: lock recovered
        (d.dir / "lock").write_text(json.dumps({"pid": 4242, "started": T0.isoformat()}), encoding="utf-8")
        self.f.items = [item("W-0010")]
        self.f.now = T0 + timedelta(seconds=dispatcher.RUN_TIMEOUT_S + 601)
        self.assertEqual(self.make(pid_alive=lambda pid: True).poll()["action"], "launched")  # too old

    def test_conserve_wakes_only_for_priority_one_and_reviews(self):
        self.f.mode = "CONSERVE"
        self.f.items = [item("W-0011", priority="2")]
        d = self.make()
        self.assertEqual(d.poll()["action"], "idle")
        self.f.items.append(item("W-0012", priority="1"))
        res = d.poll()
        self.assertEqual(res["work"], [k for k in res["work"] if "W-0012" in k])

    def test_local_problems_block_launch_and_log_once(self):
        self.f.items = [item("W-0013")]
        self.f.problems = ["checkout has uncommitted changes (2 paths)"]
        d = self.make()
        for _ in range(3):
            self.assertEqual(d.poll()["action"], "blocked-local")
        log = (d.dir / "log.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum("problem" in json.loads(l) for l in log), 1)
        self.f.problems = []
        self.assertEqual(d.poll()["action"], "launched")

    def test_dry_run_never_launches(self):
        self.f.items = [item("W-0014")]
        d = self.make()
        self.assertEqual(d.poll(dry_run=True)["action"], "dry-run")
        self.assertEqual(self.f.calls, [])
        self.assertEqual(d.load()["handled"], {})

    def test_network_error_raises_for_backoff(self):
        self.f.net_error = "git fetch failed"
        with self.assertRaises(RuntimeError):
            self.make().poll()

    def test_backoff_grows_caps_and_jitters(self):
        self.assertAlmostEqual(dispatcher.next_delay(0, 300, rng=lambda: 0.5), 300)
        self.assertAlmostEqual(dispatcher.next_delay(2, 300, rng=lambda: 0.5), 1200)
        self.assertAlmostEqual(dispatcher.next_delay(10, 300, rng=lambda: 0.5), dispatcher.MAX_BACKOFF_S)
        self.assertGreaterEqual(dispatcher.next_delay(0, 300, rng=lambda: 0.0), 270)
        self.assertLessEqual(dispatcher.next_delay(0, 300, rng=lambda: 1.0), 330)

    def test_reset_acknowledges_then_rearms(self):
        self.f.items = [item("W-0016")]
        self.f.results = [{"permission_denials": [{"tool_name": "Bash"}]}]
        d = self.make()
        d.poll()
        key = next(iter(d.load()["handled"]))
        with open(os.devnull, "w") as null:
            stdout, sys.stdout = sys.stdout, null
            try:
                dispatcher.main(["--root", str(self.root), "reset", key])
                self.assertTrue(d.load()["handled"][key]["acknowledged"])
                dispatcher.main(["--root", str(self.root), "reset", key])
            finally:
                sys.stdout = stdout
        self.assertNotIn(key, d.load()["handled"])


class RealCommandShapeTest(unittest.TestCase):
    def test_runner_never_bypasses_permissions(self):
        import inspect
        src = inspect.getsource(dispatcher.claude_runner)
        self.assertIn('"--permission-mode", "dontAsk"', src)
        self.assertNotIn("dangerously", src)
        self.assertNotIn("bypassPermissions", src)


if __name__ == "__main__":
    unittest.main()
