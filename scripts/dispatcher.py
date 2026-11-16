#!/usr/bin/env python3
"""Local Claude dispatcher, manual mode (W-0015). Standard library only.

The owner starts it in a terminal (`python scripts/dispatcher.py run`); it is never installed as a
scheduled task without a separate owner decision. While idle it runs only `git fetch` and read-only
`gh pr list`, so it uses no Claude tokens. It wakes Claude Code (`claude -p`) only when there is
actionable Claude work in the canonical control plane:
  - a `ready` item assigned to claude, dependencies done, not claimed by any branch;
  - an open PR from a `gpt/*` branch whose current head Claude hasn't been woken for yet.

Safety properties:
  - Claude runs with `--permission-mode dontAsk`: anything not pre-approved by the repository's
    permission rules is denied, never prompted, so no permission prompt is ever answered for the
    owner and nothing gets new authority. Denials are recorded as `owner_attention_blocked`, shown by
    `cp.py status`, and the same work is not relaunched.
  - Single-flight: one lock per state dir; a stale lock (dead PID or older than the run timeout) is
    recovered.
  - Idempotent: every piece of work has a key (item id + item content hash, or PR number + head SHA).
    A key is recorded as launched before Claude starts, so a crash or reboot never relaunches it.
    `reset KEY` re-arms one deliberately.
  - Fails quiet: an invalid control-plane state, a dirty checkout or a checkout off `main`
    prevents launching and is logged once per distinct problem; network errors back off
    exponentially with jitter.
  - Repository content only identifies work. It never expands authorization (doctrine).
  - Allocation-aware: in CONSERVE only priority-1 items and PR reviews wake Claude. Readings come
    from the machine-local state only, never estimated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cp  # noqa: E402

RUN_TIMEOUT_S = 3 * 3600
BASE_INTERVAL_S = 300
MAX_BACKOFF_S = 3600

PROMPT = """You were started by the local control-plane dispatcher (manual mode, W-0015), not by a person.
Follow CLAUDE.md. First run `git switch main` and `git pull --ff-only`.
Eligible work detected:
{work}
Do this work per doctrine, highest priority first. You are in dontAsk permission mode: anything that would need
owner permission is denied automatically. If that happens, stop that piece of work, record what was needed in the
item's Result section, and continue with other work. Never try to work around a denied permission.
Finish with a one-paragraph summary of what you did and what needs the owner."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


# ---------------------------------------------------------------- inputs (real implementations)

def git_inputs(root: Path) -> dict:
    """Fetch, then read the queue at origin/main and claim branches. Raises on network failure."""
    f = run(["git", "fetch", "-q", "--prune", "origin"], root)
    if f.returncode != 0:
        raise RuntimeError("git fetch failed: " + f.stderr.strip()[:200])
    names = run(["git", "ls-tree", "--name-only", "origin/main", "control/queue/"], root).stdout.split()
    items = []
    for name in names:
        if not Path(name).name.startswith("W-"):
            continue
        text = run(["git", "show", f"origin/main:{name}"], root).stdout
        try:
            meta, _ = cp.parse_item_text(text)
        except ValueError:
            continue
        meta["_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        items.append(meta)
    return {"items": items, "claims": set(cp.claims(root).keys())}


def gh_prs(repo: str, root: Path) -> list[dict]:
    r = run(["gh", "pr", "list", "-R", repo, "--state", "open", "--json",
             "number,headRefName,headRefOid,title"], root)
    if r.returncode != 0:
        raise RuntimeError("gh pr list failed: " + r.stderr.strip()[:200])
    return json.loads(r.stdout or "[]")


def local_problems(root: Path) -> list[str]:
    problems = [f"invalid control-plane state: {e}" for e in cp.validate(root)[:3]]
    branch = run(["git", "branch", "--show-current"], root).stdout.strip()
    if branch != "main":
        problems.append(f"checkout is on `{branch}`, not `main`")
    dirty = [l for l in run(["git", "status", "--porcelain"], root).stdout.splitlines() if l.strip()]
    if dirty:
        problems.append(f"checkout has uncommitted changes ({len(dirty)} paths)")
    return problems


def allocation_mode(root: Path) -> str:
    try:
        res = cp.load_resources(root)
        lim = cp.current_limits(res)
        mode, _ = cp.allocation_mode(lim["claude_weekly"], res["allocation_policy"],
                                     reset_hint=lim.get("claude_weekly_reset_hint"))
        return mode
    except (OSError, ValueError, KeyError):
        return "UNKNOWN"


def claude_runner(prompt: str, root: Path) -> dict:
    r = run(["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk"],
            root, timeout=RUN_TIMEOUT_S)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return {"is_error": True, "result": (r.stderr or r.stdout)[-500:], "permission_denials": []}
    return out


# ---------------------------------------------------------------- core

def find_work(inputs: dict, prs: list[dict], mode: str) -> list[dict]:
    items = inputs["items"]
    done = {i.get("id") for i in items if i.get("status") in ("done", "cancelled")}
    work = []
    for it in items:
        if it.get("assignee") != "claude" or it.get("status") != "ready" or it.get("id") in inputs["claims"]:
            continue
        deps = [d.strip() for d in it.get("depends_on", "").split(",") if d.strip()]
        if any(d not in done for d in deps):
            continue
        prio = int(it.get("priority", "9")) if str(it.get("priority", "9")).isdigit() else 9
        if mode == "CONSERVE" and prio > 1:
            continue
        work.append({"key": f"item:{it['id']}:{it['_hash']}", "priority": prio,
                     "describe": f"- {it['id']} (P{prio}): {it.get('title', '')}"})
    for pr in prs:
        if not str(pr.get("headRefName", "")).startswith("gpt/"):
            continue
        work.append({"key": f"pr:{pr['number']}:{pr['headRefOid']}", "priority": 0,
                     "describe": f"- review GPT-5.6 PR #{pr['number']} at {pr['headRefOid'][:7]}: {pr.get('title', '')}"})
    return sorted(work, key=lambda w: (w["priority"], w["key"]))


class Dispatcher:
    def __init__(self, root: Path, *, inputs=git_inputs, prs=gh_prs, problems=local_problems,
                 mode=allocation_mode, runner=claude_runner, clock=now, pid_alive=None, log_events=True):
        self.root = root
        self.dir = cp.state_dir(root) / "dispatcher"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.inputs, self.prs, self.problems, self.mode = inputs, prs, problems, mode
        self.runner, self.clock = runner, clock
        self.pid_alive = pid_alive or _pid_alive
        self.log_events = log_events
        reg = cp.load_json(root, "control/projects.json")["projects"]
        self.repo = reg[cp.DEFAULT_PROJECT]["repository"]

    # state
    def load(self) -> dict:
        try:
            return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"handled": {}, "last_problem": None}

    def save(self, state: dict) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "state.json")

    def log(self, entry: dict) -> None:
        entry = {"ts": self.clock().isoformat(), **entry}
        with (self.dir / "log.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry) + "\n")

    def event(self, category: str, note: str) -> None:
        if self.log_events:
            cp.append_event(self.root, {"ts": self.clock().isoformat(), "agent": "dispatcher", "type": "note",
                                        "project": cp.DEFAULT_PROJECT, "category": category, "note": note[:500]})

    # lock
    def acquire(self) -> bool:
        lock = self.dir / "lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                info = json.loads(lock.read_text(encoding="utf-8"))
                started = datetime.fromisoformat(info["started"])
                stale = (not self.pid_alive(int(info["pid"]))
                         or (self.clock() - started).total_seconds() > RUN_TIMEOUT_S + 600)
            except (OSError, ValueError, KeyError):
                stale = True
            if not stale:
                return False
            self.log({"lock": "recovered stale lock"})
            lock.unlink(missing_ok=True)
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": self.clock().isoformat()}, f)
        return True

    def release(self) -> None:
        (self.dir / "lock").unlink(missing_ok=True)

    # one poll
    def poll(self, dry_run: bool = False) -> dict:
        """Return {"action": "idle"|"launched"|"blocked-local"|"busy"|"dry-run", ...}. Raises on network errors."""
        state = self.load()
        problems = self.problems(self.root)
        if problems:
            sig = "; ".join(problems)
            if state.get("last_problem") != sig:
                state["last_problem"] = sig
                self.save(state)
                self.log({"problem": sig})
                self.event("dispatcher-not-launching", sig)
            return {"action": "blocked-local", "problems": problems}
        if state.get("last_problem"):
            state["last_problem"] = None
            self.save(state)
        mode = self.mode(self.root)
        work = find_work(self.inputs(self.root), self.prs(self.repo, self.root), mode)
        new = [w for w in work if w["key"] not in state["handled"]]
        if not new:
            return {"action": "idle", "mode": mode}
        if dry_run:
            return {"action": "dry-run", "mode": mode, "work": [w["key"] for w in new]}
        if not self.acquire():
            return {"action": "busy"}
        try:
            started = self.clock().isoformat()
            for w in new:  # record before launching: a crash must never cause a relaunch
                state["handled"][w["key"]] = {"outcome": "launched", "started": started}
            self.save(state)
            self.log({"wake": [w["key"] for w in new], "mode": mode})
            result = self.runner(PROMPT.format(work="\n".join(w["describe"] for w in new)), self.root)
            denials = result.get("permission_denials") or []
            outcome = "blocked" if denials else ("error" if result.get("is_error") else "done")
            usage = result.get("usage") or {}
            for w in new:
                state["handled"][w["key"]].update(outcome=outcome, ended=self.clock().isoformat(),
                                                  session=result.get("session_id"))
            self.save(state)
            self.log({"end": [w["key"] for w in new], "outcome": outcome, "turns": result.get("num_turns"),
                      "output_tokens": usage.get("output_tokens"), "denials": len(denials)})
            if denials:
                what = ", ".join(sorted({str(d.get("tool_name", "?")) for d in denials}))
                self.event("owner_attention_blocked", f"dispatcher run for {[w['key'] for w in new]} hit "
                                                       f"{len(denials)} denied permission(s): {what}")
            return {"action": "launched", "outcome": outcome, "work": [w["key"] for w in new],
                    "summary": str(result.get("result", ""))[:1000]}
        finally:
            self.release()


def next_delay(failures: int, base: int = BASE_INTERVAL_S, rng=random.random) -> float:
    delay = base if failures == 0 else min(MAX_BACKOFF_S, base * 2 ** failures)
    return delay * (0.9 + 0.2 * rng())


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="local Claude dispatcher (manual mode)")
    ap.add_argument("--root", type=Path, default=cp.ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once", help="poll once; launch Claude if there is new eligible work")
    o.add_argument("--dry-run", action="store_true", help="report what would launch; never launch")
    r = sub.add_parser("run", help="poll in the foreground until Ctrl+C")
    r.add_argument("--interval", type=int, default=BASE_INTERVAL_S)
    sub.add_parser("status")
    rs = sub.add_parser("reset", help="forget one work key so it can launch again, or acknowledge a block")
    rs.add_argument("key")
    args = ap.parse_args(argv)
    d = Dispatcher(args.root)

    if args.cmd == "status":
        state = d.load()
        print(f"repo: {d.repo} | state: {d.dir}")
        if state.get("last_problem"):
            print(f"not launching: {state['last_problem']}")
        for k, v in sorted(state["handled"].items(), key=lambda kv: kv[1].get("started", "")):
            flag = " (acknowledged)" if v.get("acknowledged") else ""
            print(f"  {v.get('outcome', '?'):9} {k} started={v.get('started')} ended={v.get('ended')}{flag}")
        return 0
    if args.cmd == "reset":
        state = d.load()
        entry = state["handled"].get(args.key)
        if not entry:
            print("unknown key")
            return 1
        if entry.get("outcome") == "blocked" and not entry.get("acknowledged"):
            entry["acknowledged"] = True
            print("block acknowledged (the work stays handled; reset again to re-arm it)")
        else:
            del state["handled"][args.key]
            print("re-armed")
        d.save(state)
        return 0
    if args.cmd == "once":
        print(json.dumps(d.poll(dry_run=args.dry_run), indent=2))
        return 0

    failures = 0
    print(f"dispatcher running (manual mode) every ~{args.interval}s; Ctrl+C to stop")
    while True:
        try:
            res = d.poll()
            failures = 0
            if res["action"] != "idle":
                print(f"{now().isoformat()} {json.dumps(res)[:600]}", flush=True)
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
            failures += 1
            d.log({"error": str(e)[:300], "failures": failures})
            if failures == 1:
                print(f"{now().isoformat()} error (backing off): {e}", flush=True)
        try:
            time.sleep(next_delay(failures, args.interval))
        except KeyboardInterrupt:
            print("stopped")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
