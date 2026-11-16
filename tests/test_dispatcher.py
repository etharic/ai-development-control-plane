"""W-0015 dispatcher tests. Claude is never launched: the runner, GitHub and (mostly) git inputs are fakes."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import dispatcher  # noqa: E402

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
OK = {"is_error": False, "permission_denials": [], "session_id": "s", "num_turns": 3, "usage": {}}


def item(iid, status="ready", assignee="claude", priority="2", depends_on="", h="aaa"):
    d = {"id": iid, "status": status, "assignee": assignee, "priority": priority, "title": f"t{iid}", "_hash": h}
    if depends_on:
        d["depends_on"] = depends_on
    return d


class FakeRunner:
    """start/wait behaviour is scripted per call: 'ok', a result dict, or an exception instance."""

    def __init__(self):
        self.starts, self.start_plan, self.wait_plan = [], [], []

    def start(self, prompt, root):
        self.starts.append(prompt)
        step = self.start_plan.pop(0) if self.start_plan else "ok"
        if isinstance(step, BaseException):
            raise step
        return {"pid": 1000 + len(self.starts)}

    def pid(self, handle):
        return handle["pid"]

    def wait(self, handle):
        step = self.wait_plan.pop(0) if self.wait_plan else OK
        if isinstance(step, BaseException):
            raise step
        return step


class Fake:
    def __init__(self):
        self.items, self.claims, self.prs, self.problems, self.mode = [], set(), [], [], "BALANCED"
        self.now, self.net_error = T0, None
        self.runner = FakeRunner()

    def inputs(self, root):
        if self.net_error:
            raise RuntimeError(self.net_error)
        return {"items": list(self.items), "claims": set(self.claims)}


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

    def outcome(self, d, iid):
        return next(v for k, v in d.load()["handled"].items() if iid in k)["outcome"]

    # --- basic eligibility
    def test_idle_for_an_hour_without_invoking_claude(self):
        self.f.items = [item("W-0001", assignee="owner"), item("W-0002", status="blocked"),
                        item("W-0003", depends_on="W-0001"), item("W-0004", status="done")]
        self.f.prs = [{"number": 9, "headRefName": "claude/W-0003-x", "headRefOid": "c" * 40, "title": "mine"}]
        d = self.make()
        for _ in range(12):  # an hour at the 5-minute cadence
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(self.f.runner.starts, [])

    def test_new_eligible_item_launches_exactly_once(self):
        self.f.items = [item("W-0001", status="done"), item("W-0005", depends_on="W-0001")]
        d = self.make()
        res = d.poll()
        self.assertEqual((res["action"], res["outcome"]), ("launched", "done"))
        for _ in range(5):
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(len(self.f.runner.starts), 1)
        self.assertIn("W-0005", self.f.runner.starts[0])
        self.assertIn("dontAsk", self.f.runner.starts[0])

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
        self.assertEqual(len(self.f.runner.starts), 2)

    # --- launch state machine (W-0015 review finding HIGH)
    def test_definite_spawn_failure_rearms_after_backoff(self):
        self.f.items = [item("W-0020")]
        self.f.runner.start_plan = [dispatcher.SpawnFailed("claude CLI not found on PATH")]
        d = self.make()
        res = d.poll()
        self.assertEqual(res["action"], "spawn-failed")
        self.assertEqual(self.outcome(d, "W-0020"), "spawn_failed")
        self.assertEqual(self.make().poll()["action"], "idle")  # restart before retry_after: still waits
        self.f.now = datetime.fromisoformat(res["retry_after"]) + timedelta(seconds=1)
        res2 = self.make().poll()  # restart after retry_after: launches
        self.assertEqual((res2["action"], res2["outcome"]), ("launched", "done"))
        self.assertEqual(len(self.f.runner.starts), 2)
        self.assertEqual(d.load()["handled"][res2["work"][0]]["attempts"], 2)

    def test_repeated_spawn_failures_back_off_further(self):
        self.f.items = [item("W-0021")]
        self.f.runner.start_plan = [dispatcher.SpawnFailed("x"), dispatcher.SpawnFailed("x")]
        d = self.make()
        first = datetime.fromisoformat(d.poll()["retry_after"]) - self.f.now
        self.f.now = self.f.now + first + timedelta(seconds=1)
        second = datetime.fromisoformat(d.poll()["retry_after"]) - self.f.now
        self.assertGreater(second, first)

    def test_failure_after_start_is_ambiguous_and_never_relaunched(self):
        self.f.items = [item("W-0022")]
        self.f.runner.wait_plan = [dispatcher.AmbiguousRun("timed out")]
        d = self.make()
        self.assertEqual(d.poll()["action"], "ambiguous")
        self.assertEqual(self.outcome(d, "W-0022"), "ambiguous")
        self.f.now += timedelta(days=2)
        for _ in range(3):
            self.assertEqual(self.make().poll()["action"], "idle")  # across restarts too
        self.assertEqual(len(self.f.runner.starts), 1)

    def test_unexpected_wait_error_is_ambiguous(self):
        self.f.items = [item("W-0023")]
        self.f.runner.wait_plan = [ValueError("transport broke")]
        d = self.make()
        self.assertEqual(d.poll()["action"], "ambiguous")
        self.assertEqual(self.make().poll()["action"], "idle")

    def test_crash_while_reserved_recovers_as_ambiguous(self):
        self.f.items = [item("W-0024")]
        self.f.runner.start_plan = [KeyboardInterrupt()]  # killed between reserve and confirmed start
        d = self.make()
        with self.assertRaises(KeyboardInterrupt):
            d.poll()
        self.assertEqual(self.outcome(d, "W-0024"), "reserved")
        self.assertFalse((d.dir / "lock").exists())
        self.assertEqual(self.make().poll()["action"], "idle")  # "reboot"
        self.assertEqual(self.outcome(d, "W-0024"), "ambiguous")
        self.assertEqual(len(self.f.runner.starts), 1)

    def test_crash_while_started_recovers_as_ambiguous(self):
        self.f.items = [item("W-0025")]
        self.f.runner.wait_plan = [KeyboardInterrupt()]  # killed while Claude runs
        d = self.make()
        with self.assertRaises(KeyboardInterrupt):
            d.poll()
        self.assertEqual(self.outcome(d, "W-0025"), "started")
        self.assertEqual(self.make().poll()["action"], "idle")
        self.assertEqual(self.outcome(d, "W-0025"), "ambiguous")
        self.assertEqual(len(self.f.runner.starts), 1)

    def test_in_flight_run_is_not_recovered_by_another_poller(self):
        self.f.items = [item("W-0026")]
        d = self.make()
        state = {"handled": {"item:W-0026:aaa": {"outcome": "started"}}, "last_problem": None}
        d.save(state)
        (d.dir / "lock").write_text(json.dumps({"pid": 4242, "started": T0.isoformat()}), encoding="utf-8")
        self.assertEqual(self.make(pid_alive=lambda pid: True).poll()["action"], "busy")
        self.assertEqual(self.outcome(d, "W-0026"), "started")  # the live run owns it

    def test_permission_block_is_recorded_and_not_relaunched(self):
        self.f.items = [item("W-0006")]
        self.f.runner.wait_plan = [{"is_error": False, "permission_denials": [{"tool_name": "Bash"}],
                                    "session_id": "s1"}]
        d = self.make()
        self.assertEqual(d.poll()["outcome"], "blocked")
        for _ in range(5):
            self.assertEqual(d.poll()["action"], "idle")
        self.assertEqual(len(self.f.runner.starts), 1)
        self.assertEqual(self.outcome(d, "W-0006"), "blocked")

    def test_restart_recovers_state(self):
        self.f.items = [item("W-0007")]
        self.make().poll()
        self.assertEqual(self.make().poll()["action"], "idle")
        self.assertEqual(len(self.f.runner.starts), 1)

    # --- lock, modes, gating
    def test_single_flight_and_stale_lock_recovery(self):
        self.f.items = [item("W-0009")]
        d = self.make(pid_alive=lambda pid: True)
        (d.dir / "lock").write_text(json.dumps({"pid": 4242, "started": T0.isoformat()}), encoding="utf-8")
        self.assertEqual(d.poll()["action"], "busy")
        self.assertEqual(self.f.runner.starts, [])
        self.assertEqual(self.make(pid_alive=lambda pid: False).poll()["action"], "launched")
        (d.dir / "lock").write_text(json.dumps({"pid": 4242, "started": T0.isoformat()}), encoding="utf-8")
        self.f.items = [item("W-0010")]
        self.f.now = T0 + timedelta(seconds=dispatcher.RUN_TIMEOUT_S + 601)
        self.assertEqual(self.make(pid_alive=lambda pid: True).poll()["action"], "launched")

    def test_conserve_wakes_only_for_priority_one_and_reviews(self):
        self.f.mode = "CONSERVE"
        self.f.items = [item("W-0011", priority="2")]
        d = self.make()
        self.assertEqual(d.poll()["action"], "idle")
        self.f.items.append(item("W-0012", priority="1"))
        res = d.poll()
        self.assertTrue(res["work"] and all("W-0012" in k for k in res["work"]))

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
        self.assertEqual(self.f.runner.starts, [])
        self.assertEqual(d.load()["handled"], {})

    def test_network_error_raises_for_backoff_and_releases_lock(self):
        self.f.net_error = "git fetch failed"
        d = self.make()
        with self.assertRaises(RuntimeError):
            d.poll()
        self.assertFalse((d.dir / "lock").exists())

    def test_backoff_grows_caps_and_jitters(self):
        self.assertAlmostEqual(dispatcher.next_delay(0, 300, rng=lambda: 0.5), 300)
        self.assertAlmostEqual(dispatcher.next_delay(2, 300, rng=lambda: 0.5), 1200)
        self.assertAlmostEqual(dispatcher.next_delay(10, 300, rng=lambda: 0.5), dispatcher.MAX_BACKOFF_S)
        self.assertGreaterEqual(dispatcher.next_delay(0, 300, rng=lambda: 0.0), 270)
        self.assertLessEqual(dispatcher.next_delay(0, 300, rng=lambda: 1.0), 330)

    def test_reset_acknowledges_then_rearms(self):
        self.f.items = [item("W-0016")]
        self.f.runner.wait_plan = [dispatcher.AmbiguousRun("x")]
        d = self.make()
        d.poll()
        key = next(iter(d.load()["handled"]))
        with open(os.devnull, "w") as null, mock.patch.object(sys, "stdout", null):
            dispatcher.main(["--root", str(self.root), "reset", key])
            self.assertTrue(d.load()["handled"][key]["acknowledged"])
            dispatcher.main(["--root", str(self.root), "reset", key])
        self.assertNotIn(key, d.load()["handled"])
        self.assertEqual(d.poll()["action"], "launched")


class ClaudeCLITest(unittest.TestCase):
    def test_missing_cli_is_a_definite_spawn_failure(self):
        with mock.patch.object(dispatcher.shutil, "which", return_value=None):
            with self.assertRaises(dispatcher.SpawnFailed):
                dispatcher.ClaudeCLI().start("x", REPO)

    def test_os_refusal_is_a_definite_spawn_failure(self):
        with mock.patch.object(dispatcher.shutil, "which", return_value="claude"), \
                mock.patch.object(dispatcher.subprocess, "Popen", side_effect=OSError("denied")):
            with self.assertRaises(dispatcher.SpawnFailed):
                dispatcher.ClaudeCLI().start("x", REPO)

    def test_unparseable_output_after_start_is_ambiguous(self):
        proc = mock.Mock(returncode=1)
        proc.communicate.return_value = ("not json", "boom")
        with self.assertRaises(dispatcher.AmbiguousRun):
            dispatcher.ClaudeCLI().wait(proc)

    def test_timeout_after_start_kills_and_is_ambiguous(self):
        proc = mock.Mock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("claude", 1)
        with self.assertRaises(dispatcher.AmbiguousRun):
            dispatcher.ClaudeCLI().wait(proc)
        proc.kill.assert_called_once()

    def test_command_never_bypasses_permissions(self):
        import inspect
        src = inspect.getsource(dispatcher.ClaudeCLI)
        self.assertIn('"--permission-mode", "dontAsk"', src)
        self.assertNotIn("dangerously", src)
        self.assertNotIn("bypassPermissions", src)


class SnapshotTest(unittest.TestCase):
    """W-0015 review finding MEDIUM: queue and claims come from one fetched origin snapshot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def git(self, cwd, *args):
        subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       check=True, capture_output=True)

    def write_item(self, repo, iid, status="ready"):
        q = repo / "control" / "queue"
        q.mkdir(parents=True, exist_ok=True)
        (q / f"{iid}.md").write_text(f"---\nid: {iid}\ntitle: t\nassignee: claude\nstatus: {status}\npriority: 1\n"
                                     f"project: p\ncreated: 2026-11-16\n---\n", encoding="utf-8")

    def test_stale_local_main_and_local_branches_are_ignored(self):
        remote, local, other = self.tmp / "remote.git", self.tmp / "local", self.tmp / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
        self.git(other, "switch", "-q", "-c", "main")
        self.write_item(other, "W-0030")
        self.git(other, "add", ".")
        self.git(other, "commit", "-q", "-m", "one")
        self.git(other, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(remote), str(local)], check=True, capture_output=True)
        # origin moves on: a new item, W-0030 done, and a pushed claim for W-0031
        self.write_item(other, "W-0030", status="done")
        self.write_item(other, "W-0031")
        self.git(other, "add", ".")
        self.git(other, "commit", "-q", "-m", "two")
        self.git(other, "push", "-q", "origin", "main")
        self.git(other, "push", "-q", "origin", "main:refs/heads/gpt/W-0031-x")
        # local main stays stale (W-0030 still ready in the working tree); a local-only claim branch exists
        self.git(local, "branch", "claude/W-0030-local-only")
        inputs = dispatcher.git_inputs(local)
        by_id = {i["id"]: i["status"] for i in inputs["items"]}
        self.assertEqual(by_id, {"W-0030": "done", "W-0031": "ready"})  # from origin/main, not the stale tree
        self.assertEqual(inputs["claims"], {"W-0031"})  # remote claim counted; local-only branch ignored
        self.assertIn("status: ready", (local / "control/queue/W-0030.md").read_text(encoding="utf-8"))
        # a claim branch deleted on origin disappears after fetch --prune
        self.git(other, "push", "-q", "origin", "--delete", "gpt/W-0031-x")
        self.assertEqual(dispatcher.git_inputs(local)["claims"], set())


if __name__ == "__main__":
    unittest.main()
