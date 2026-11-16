import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import claude_usage  # noqa: E402
import cp  # noqa: E402
import statusline  # noqa: E402

POLICY = {"spend_gap_points": 15, "conserve_gap_points": 15,
          "harvest_window_hours": 36, "harvest_min_remaining_percent": 10}
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def weekly(used, hours_left, age_min=5, stale=False):
    return {"used_percent": used, "resets_at": (NOW + timedelta(hours=hours_left)).isoformat(),
            "measured_at": (NOW - timedelta(minutes=age_min)).isoformat(), "stale": stale,
            "age_minutes": age_min}


def rmtree(path):
    """Windows marks git object files read-only; clear the bit and retry."""
    def onexc(func, p, exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onexc=onexc)


def write_lines(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def assistant(mid, out, ts="2026-11-16T10:00:00Z", session="s1", model="claude-opus-5-5", sidechain=False):
    return {"type": "assistant", "timestamp": ts, "sessionId": session, "isSidechain": sidechain,
            "cwd": "C:\\repo", "requestId": "req_" + mid,
            "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                  "cache_read_input_tokens": 100, "output_tokens": out}}}


def human(text, ts="2026-11-16T09:59:00Z"):
    return {"type": "user", "timestamp": ts, "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def tool_result(ts="2026-11-16T10:00:01Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


class AllocationModeTests(unittest.TestCase):
    def mode(self, w, **kw):
        return cp.allocation_mode(w, POLICY, NOW, **kw)[0]

    def test_unknown_without_measurement(self):
        self.assertEqual(self.mode(None), "UNKNOWN")
        self.assertEqual(self.mode({"used_percent": None}), "UNKNOWN")

    def test_stale_is_unknown(self):
        self.assertEqual(self.mode(weekly(10, 100, stale=True)), "UNKNOWN")

    def test_harvest_near_reset_with_capacity(self):
        self.assertEqual(self.mode(weekly(70, 20)), "HARVEST")

    def test_no_harvest_when_nearly_used(self):
        self.assertEqual(self.mode(weekly(95, 20)), "BALANCED")

    def test_spend_when_under_pace(self):
        # 84h left of 168 -> 50% elapsed; 20% used -> gap -30
        self.assertEqual(self.mode(weekly(20, 84)), "SPEND")

    def test_conserve_when_over_pace(self):
        self.assertEqual(self.mode(weekly(80, 84)), "CONSERVE")

    def test_balanced_on_pace(self):
        self.assertEqual(self.mode(weekly(55, 84)), "BALANCED")

    def test_unknown_inside_harvest_window_asks_for_reading(self):
        hint = (NOW - timedelta(days=7) + timedelta(hours=10)).isoformat()  # projected forward one week
        mode, reason = cp.allocation_mode(None, POLICY, NOW, reset_hint=hint)
        self.assertEqual(mode, "UNKNOWN")
        self.assertIn("harvest window", reason)


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(rmtree, self.tmp)

    def test_dedupes_split_messages_and_counts_owner_prompts(self):
        t = self.tmp / "proj" / "s1.jsonl"
        write_lines(t, [human("hello"), assistant("m1", 50), assistant("m1", 50), tool_result(),
                        assistant("m2", 7),
                        {"type": "user", "isMeta": True, "message": {"content": "meta"}}])
        # subagent transcript nested under <session>/
        write_lines(self.tmp / "proj" / "s1" / "subagents" / "a.jsonl", [assistant("m3", 3, sidechain=True)])
        s = claude_usage.summarize(claude_usage.session_files(t))
        self.assertEqual(s["messages"], 3)
        self.assertEqual(s["owner_prompts"], 1)
        self.assertEqual(s["tokens"]["output_tokens"], 60)
        self.assertEqual(s["tokens"]["cache_read_input_tokens"], 300)

    def test_report_dedupes_across_sessions_and_filters_since(self):
        write_lines(self.tmp / "p" / "a.jsonl", [assistant("m1", 5, ts="2026-11-01T00:00:00Z"), assistant("m2", 5)])
        write_lines(self.tmp / "p" / "b.jsonl", [assistant("m2", 5)])  # resumed copy
        r = claude_usage.report(claude_usage.parse_ts("2026-11-10T00:00:00Z"), self.tmp)
        self.assertEqual(r["p"]["messages"], 1)

    def test_tolerates_partial_trailing_line(self):
        t = self.tmp / "s.jsonl"
        t.write_text(json.dumps(assistant("m1", 1)) + "\n{\"type\": \"assis", encoding="utf-8")
        self.assertEqual(claude_usage.summarize([t])["messages"], 1)


class StatuslineTests(unittest.TestCase):
    def test_records_measured_windows_only(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(rmtree, tmp)
        payload = {"model": {"display_name": "Opus"}, "context_window": {"used_percentage": 12},
                   "rate_limits": {"five_hour": {"used_percentage": 40.26, "resets_at": 1763300000},
                                   "seven_day": {"used_percentage": 61, "resets_at": 1763800000}}}
        env = dict(os.environ, CP_LIMITS_FILE=str(tmp / "limits.json"))
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "statusline.py")], input=json.dumps(payload),
                             capture_output=True, text=True, env=env, check=True).stdout
        self.assertIn("5h 40%", out)
        self.assertIn("wk 61%", out)
        snap = json.loads((tmp / "limits.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["source"], "claude-code-statusline")
        self.assertEqual(snap["windows"]["five_hour"]["used_percent"], 40.3)
        # unchanged values append no extra history line
        subprocess.run([sys.executable, str(REPO / "scripts" / "statusline.py")], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, check=True)
        self.assertEqual(len((tmp / "limits_history.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_no_rate_limits_records_nothing_and_never_crashes(self):
        self.assertIsNone(statusline.extract({"model": {}}))
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "statusline.py")], input="not json",
                           capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), "Claude")


class ControlStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(rmtree, self.root)
        for rel in ("control/resources.json", "control/projects.json", "control/queue/_TEMPLATE.md"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.root / rel)
        (self.root / ".local").mkdir()
        (self.root / ".local" / "events.jsonl").write_text("", encoding="utf-8")

    def item(self, iid, **fields):
        meta = {"id": iid, "title": "t", "assignee": "gpt56", "status": "ready", "priority": "2",
                "project": "ai-development-control-plane", "created": "2026-11-16"}
        meta.update(fields)
        body = "---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n## Objective\n"
        (self.root / "control" / "queue" / f"{iid}.md").write_text(body, encoding="utf-8")

    def test_repository_state_is_valid(self):
        self.assertEqual(cp.validate(REPO), [])

    def test_valid_minimal_state(self):
        self.item("W-0001")
        self.assertEqual(cp.validate(self.root), [])

    def test_rejects_bad_items(self):
        self.item("W-0001", assignee="intern", status="doing", priority="9", depends_on="W-0099")
        (self.root / "control" / "queue" / "W-0002.md").write_text("no frontmatter", encoding="utf-8")
        errors = "\n".join(cp.validate(self.root))
        for needle in ("assignee", "status", "priority", "W-0099", "frontmatter"):
            self.assertIn(needle, errors)

    def test_rejects_id_filename_mismatch(self):
        self.item("W-0001")
        (self.root / "control" / "queue" / "W-0001.md").rename(self.root / "control" / "queue" / "W-0003.md")
        self.assertTrue(any("filename" in e for e in cp.validate(self.root)))

    def test_committed_resources_must_not_carry_measurements(self):
        res = json.loads((self.root / "control/resources.json").read_text(encoding="utf-8"))
        res["resources"]["claude_weekly"].update(used_percent=40, measured_at=NOW.isoformat(),
                                                 source="claude-code-statusline")
        (self.root / "control/resources.json").write_text(json.dumps(res), encoding="utf-8")
        self.assertTrue(any("must be null in the repository" in e for e in cp.validate(self.root)))

    def test_fresh_checkout_without_local_state_is_valid(self):
        (self.root / ".local" / "events.jsonl").unlink()
        self.assertEqual(cp.validate(self.root), [])

    def test_state_dir_override(self):
        other = self.root / "elsewhere"
        os.environ["CP_STATE_DIR"] = str(other)
        self.addCleanup(os.environ.pop, "CP_STATE_DIR", None)
        with redirect_stdout(io.StringIO()):
            cp.main(["--root", str(self.root), "log", "note", "--note", "x"])
        self.assertTrue((other / "events.jsonl").exists())

    def test_rejects_estimated_percentage(self):
        res = json.loads((self.root / "control/resources.json").read_text(encoding="utf-8"))
        res["resources"]["claude_weekly"].update(used_percent=40, measured_at=NOW.isoformat(), source="token-estimate")
        (self.root / "control/resources.json").write_text(json.dumps(res), encoding="utf-8")
        self.assertTrue(any("not an estimate" in e for e in cp.validate(self.root)))

    def test_log_and_measure_round_trip(self):
        with redirect_stdout(io.StringIO()):
            cp.main(["--root", str(self.root), "log", "intervention", "--weight", "5", "--category", "setup", "--note", "x"])
            cp.main(["--root", str(self.root), "measure", "--weekly-used", "42", "--weekly-reset", "2026-11-20T08:00:00+00:00"])
        self.assertEqual(cp.validate(self.root), [])
        committed = json.loads((self.root / "control/resources.json").read_text(encoding="utf-8"))
        self.assertIsNone(committed["resources"]["claude_weekly"]["used_percent"])  # repo stays public-safe
        res = cp.load_resources(self.root)
        self.assertEqual(res["resources"]["claude_weekly"]["used_percent"], 42)
        self.assertEqual(res["resources"]["claude_weekly"]["source"], "owner-usage-command")
        lines = (self.root / ".local/events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines], ["intervention", "measurement"])

    def test_invalid_event_is_refused(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            cp.main(["--root", str(self.root), "log", "work_item", "--item", "bad", "--outcome", "done"])
        self.assertEqual((self.root / ".local/events.jsonl").read_text(encoding="utf-8"), "")

    def test_record_session_from_transcript(self):
        t = self.root / "s.jsonl"
        write_lines(t, [human("go"), assistant("m1", 9)])
        with redirect_stdout(io.StringIO()):
            cp.main(["--root", str(self.root), "record-session", "--transcript", str(t), "--session-id", "s1"])
        ev = json.loads((self.root / ".local/events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((ev["type"], ev["owner_prompts"], ev["tokens"]["output_tokens"]), ("session", 1, 9))
        self.assertEqual(cp.validate(self.root), [])

    def test_claims_from_branch_names(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        g = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run(g + ["branch", "gpt/W-0007-thing"], check=True)
        subprocess.run(g + ["branch", "feature/W-0008"], check=True)  # wrong prefix: not a claim
        c = cp.claims(self.root)
        self.assertIn("W-0007", c)
        self.assertNotIn("W-0008", c)

    def test_current_limits_prefers_fresher_snapshot(self):
        res = json.loads((self.root / "control/resources.json").read_text(encoding="utf-8"))
        res["resources"]["claude_weekly"].update(used_percent=10, measured_at=(NOW - timedelta(hours=3)).isoformat(),
                                                 source="owner-usage-command",
                                                 resets_at=(NOW + timedelta(days=2)).isoformat())
        # isolate from whatever five-hour reading the real repository state carries
        res["resources"]["claude_five_hour"].update(used_percent=None, measured_at=None, resets_at=None)
        snap = self.root / "limits.json"
        snap.write_text(json.dumps({"measured_at": (NOW - timedelta(minutes=2)).isoformat(),
                                    "source": "claude-code-statusline",
                                    "windows": {"seven_day": {"used_percent": 30,
                                                              "resets_at": (NOW + timedelta(days=2)).isoformat()}}}),
                        encoding="utf-8")
        lim = cp.current_limits(res, snap, NOW)
        self.assertEqual(lim["claude_weekly"]["used_percent"], 30)
        self.assertFalse(lim["claude_weekly"]["stale"])
        self.assertIsNone(lim["claude_five_hour"])


    def test_projects_must_declare_protected_branches(self):
        self.assertEqual(cp.validate_projects({"projects": {"a": {"protected_branches": ["main"]}}}), [])
        for bad in ({}, {"protected_branches": []}, {"protected_branches": [""]}, {"protected_branches": "main"}):
            self.assertEqual(len(cp.validate_projects({"projects": {"a": bad}})), 1, bad)


class InstallProjectTest(unittest.TestCase):
    """A second local repository wired to a copy of the control plane, hooks executed for real."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(rmtree, self.tmp)
        self.lab = self.tmp / "lab"
        for rel in ("control/resources.json", "control/queue/_TEMPLATE.md", ".claude/settings.json"):
            (self.lab / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.lab / rel)
        shutil.copytree(REPO / "scripts", self.lab / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        reg = json.loads((REPO / "control/projects.json").read_text(encoding="utf-8"))
        reg["projects"]["fixture"] = {"repository": "o/fixture", "role": "test", "production": True,
                                      "protected_branches": ["main", "stable"]}
        (self.lab / "control/projects.json").write_text(json.dumps(reg), encoding="utf-8")
        self.repo = self.tmp / "fixture"
        self.repo.mkdir()
        g = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(g + ["init", "-q", "-b", "feature"], check=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)

    def install(self, *extra, project="fixture"):
        with redirect_stdout(io.StringIO()) as out:
            code = cp.main(["--root", str(self.lab), "install-project", str(self.repo), "--project", project, *extra])
        return code, out.getvalue()

    def hook(self, settings, event, payload):
        entry = settings["hooks"][event][0]["hooks"][0]["command"]
        return subprocess.run(entry, shell=True, cwd=self.repo, input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60)

    def test_install_wires_hooks_policy_and_ignore(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        settings = json.loads((self.repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        lab = json.loads((self.lab / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], lab["permissions"])
        for event in ("SessionStart", "PreToolUse", "SessionEnd"):
            cmd = settings["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn(self.lab.resolve().as_posix(), cmd)
            self.assertIn("--project fixture", cmd)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "check-ignore", "-q",
                                         ".claude/settings.local.json"]).returncode, 0)

        def push(command):
            return self.hook(settings, "PreToolUse", {"tool_name": "Bash", "cwd": str(self.repo),
                                                      "tool_input": {"command": command}})
        blocked = push("git push origin stable")  # protected only by this project's registration
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("stable", blocked.stderr)
        self.assertEqual(push("git push -u origin feature").returncode, 0)
        status = self.hook(settings, "SessionStart", {})
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("managed project: fixture", status.stdout)

    def test_idempotent_refuses_to_clobber_and_requires_registration(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install()[0], 0)
        path = self.repo / ".claude" / "settings.local.json"
        path.write_text("{}", encoding="utf-8")
        code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn("--force", out)
        self.assertEqual(path.read_text(encoding="utf-8"), "{}")
        self.assertEqual(self.install("--force")[0], 0)
        self.assertEqual(self.install(project="unregistered")[0], 1)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count(".claude/settings.local.json"), 1)


if __name__ == "__main__":
    unittest.main()
