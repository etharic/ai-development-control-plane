#!/usr/bin/env python3
"""Control-plane tool: validate state, print status, compute allocation mode,
record measurements/telemetry. Standard library only.

    python scripts/cp.py status            # what to do next (SessionStart hook)
    python scripts/cp.py validate          # integrity check; non-zero on error
    python scripts/cp.py mode              # Claude allocation mode + reasoning
    python scripts/cp.py measure --from-statusline | --weekly-used N --weekly-reset ISO ...
    python scripts/cp.py log intervention --weight 1 --note "..."
    python scripts/cp.py log work_item --item W-0002 --outcome done --evidence "PR #4"
    python scripts/cp.py record-session --hook   # SessionEnd hook (reads stdin)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_usage  # noqa: E402
import statusline  # noqa: E402

ROOT = Path(os.environ.get("CP_ROOT") or Path(__file__).resolve().parents[1])
DEFAULT_PROJECT = "ai-development-lab"

ASSIGNEES = {"claude", "gpt56", "gpt6", "owner"}
STATUSES = {"ready", "blocked", "done", "cancelled"}
BRANCH_PREFIX = {"claude": "claude/", "gpt56": "gpt/", "gpt6": "gpt/"}
EVENT_TYPES = {"session", "measurement", "intervention", "work_item", "ci", "note"}
OUTCOMES = {"done", "failed", "blocked", "cancelled"}
FAILURE_CLASSES = {
    "MODEL_REASONING", "MODEL_IMPLEMENTATION", "TOOL_UNAVAILABLE", "TOOL_PERMISSION",
    "ENVIRONMENT", "NETWORK", "EXTERNAL_SERVICE", "USER_REQUIRED", "UNKNOWN",
}
CLAUDE_LIMITS = {"claude_five_hour": "five_hour", "claude_weekly": "seven_day"}
ITEM_RE = re.compile(r"^W-\d{4}$")
STALE_CLAIM = timedelta(hours=48)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    return claude_usage.parse_ts(value) if isinstance(value, str) else None


# ---------------------------------------------------------------- loading

def load_json(root: Path, rel: str):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def parse_item(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("missing --- frontmatter ---")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"bad frontmatter line: {line!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, m.group(2)


def load_queue(root: Path) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    qdir = root / "control" / "queue"
    for path in sorted(qdir.glob("W-*.md")):
        try:
            meta, _ = parse_item(path)
        except (ValueError, OSError) as e:
            errors.append(f"{path.name}: {e}")
            continue
        meta["_file"] = path.name
        items.append(meta)
    return items, errors


def load_events(root: Path) -> tuple[list[dict], list[str]]:
    events, errors = [], []
    p = root / "telemetry" / "events.jsonl"
    if not p.exists():
        return events, ["missing telemetry/events.jsonl"]
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError as ex:
            errors.append(f"events.jsonl:{n}: {ex}")
            continue
        if not isinstance(e, dict):
            errors.append(f"events.jsonl:{n}: not an object")
            continue
        e["_line"] = n
        events.append(e)
    return events, errors


# ---------------------------------------------------------------- validation

def validate_resources(res: dict) -> list[str]:
    errors = []
    for name, r in (res.get("resources") or {}).items():
        for k in ("scope", "kind", "unit"):
            if k not in r:
                errors.append(f"resources.{name}: missing {k}")
        if name in CLAUDE_LIMITS:
            used = r.get("used_percent")
            if used is not None:
                if not isinstance(used, (int, float)) or not 0 <= used <= 100:
                    errors.append(f"resources.{name}: used_percent must be 0..100 or null")
                if not (parse_ts(r.get("measured_at")) and r.get("source")):
                    errors.append(f"resources.{name}: a value requires measured_at and source")
                if r.get("source") not in (None, "claude-code-statusline", "owner-usage-command"):
                    errors.append(f"resources.{name}: source must be a measurement, not an estimate")
            for k in ("measured_at", "resets_at"):
                if r.get(k) is not None and parse_ts(r.get(k)) is None:
                    errors.append(f"resources.{name}: {k} is not ISO-8601")
    pol = res.get("allocation_policy") or {}
    for k in ("spend_gap_points", "conserve_gap_points", "harvest_window_hours", "harvest_min_remaining_percent"):
        if not isinstance(pol.get(k), (int, float)):
            errors.append(f"allocation_policy.{k} missing or not a number")
    return errors


def validate_queue(items: list[dict], projects: set[str]) -> list[str]:
    errors, ids = [], {}
    for it in items:
        f = it["_file"]
        iid = it.get("id", "")
        if not ITEM_RE.match(iid) or f != f"{iid}.md":
            errors.append(f"{f}: id must be W-NNNN and match the filename")
        if iid in ids:
            errors.append(f"{f}: duplicate id {iid}")
        ids[iid] = it
        for k in ("title", "assignee", "status", "priority", "project", "created"):
            if not it.get(k):
                errors.append(f"{f}: missing {k}")
        if it.get("assignee") and it["assignee"] not in ASSIGNEES:
            errors.append(f"{f}: assignee must be one of {sorted(ASSIGNEES)}")
        if it.get("status") and it["status"] not in STATUSES:
            errors.append(f"{f}: status must be one of {sorted(STATUSES)}")
        if it.get("priority") and it["priority"] not in {"1", "2", "3", "4"}:
            errors.append(f"{f}: priority must be 1 (highest) .. 4")
        if it.get("project") and it["project"] not in projects:
            errors.append(f"{f}: project {it['project']!r} not in control/projects.json")
        if it.get("created") and parse_ts(it["created"]) is None:
            errors.append(f"{f}: created is not ISO-8601")
    for it in items:
        for dep in filter(None, (d.strip() for d in it.get("depends_on", "").split(","))):
            if dep not in ids:
                errors.append(f"{it['_file']}: depends_on unknown item {dep}")
    return errors


def validate_events(events: list[dict]) -> list[str]:
    errors = []
    for e in events:
        where = f"events.jsonl:{e['_line']}"
        if parse_ts(e.get("ts")) is None:
            errors.append(f"{where}: ts missing or not ISO-8601")
        if not e.get("agent"):
            errors.append(f"{where}: agent missing")
        t = e.get("type")
        if t not in EVENT_TYPES:
            errors.append(f"{where}: type must be one of {sorted(EVENT_TYPES)}")
        if t == "intervention" and not (isinstance(e.get("weight"), int) and e["weight"] >= 0):
            errors.append(f"{where}: intervention needs integer weight >= 0")
        if t == "work_item":
            if not ITEM_RE.match(str(e.get("item", ""))):
                errors.append(f"{where}: work_item needs item W-NNNN")
            if e.get("outcome") not in OUTCOMES:
                errors.append(f"{where}: outcome must be one of {sorted(OUTCOMES)}")
        if e.get("failure_class") is not None and e["failure_class"] not in FAILURE_CLASSES:
            errors.append(f"{where}: unknown failure_class")
        if t == "measurement" and e.get("source") not in ("claude-code-statusline", "owner-usage-command"):
            errors.append(f"{where}: measurement source must be a real measurement")
        if t == "session" and not isinstance(e.get("tokens"), dict):
            errors.append(f"{where}: session needs tokens")
    return errors


def validate(root: Path) -> list[str]:
    errors = []
    try:
        res = load_json(root, "control/resources.json")
        errors += validate_resources(res)
    except (OSError, ValueError) as e:
        errors.append(f"control/resources.json: {e}")
    try:
        projects = set((load_json(root, "control/projects.json").get("projects") or {}).keys())
    except (OSError, ValueError) as e:
        errors.append(f"control/projects.json: {e}")
        projects = set()
    items, qerr = load_queue(root)
    errors += qerr + validate_queue(items, projects)
    events, eerr = load_events(root)
    errors += eerr + validate_events(events)
    return errors


# ---------------------------------------------------------------- limits & mode

def current_limits(res: dict, snapshot_path: Path | None = None, now: datetime | None = None) -> dict:
    """Freshest real measurement per window: committed state vs local statusline snapshot."""
    now = now or now_utc()
    out = {}
    snap = None
    try:
        snap = json.loads((snapshot_path or statusline.limits_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for name, window in CLAUDE_LIMITS.items():
        r = (res.get("resources") or {}).get(name) or {}
        cands = []
        if r.get("used_percent") is not None:
            cands.append({"used_percent": r["used_percent"], "resets_at": r.get("resets_at"),
                          "measured_at": r.get("measured_at"), "source": r.get("source")})
        if snap and window in (snap.get("windows") or {}):
            w = snap["windows"][window]
            cands.append({"used_percent": w.get("used_percent"), "resets_at": w.get("resets_at"),
                          "measured_at": snap.get("measured_at"), "source": snap.get("source")})
        cands = [c for c in cands if parse_ts(c["measured_at"])]
        best = max(cands, key=lambda c: parse_ts(c["measured_at"]), default=None)
        fresh_min = r.get("freshness_minutes") or 60
        if best:
            age = now - parse_ts(best["measured_at"])
            reset = parse_ts(best["resets_at"])
            best["stale"] = age > timedelta(minutes=fresh_min) or (reset is not None and reset <= now)
            best["age_minutes"] = int(age.total_seconds() // 60)
        out[name] = best
        out[name + "_reset_hint"] = r.get("resets_at")
    return out


def allocation_mode(weekly: dict | None, policy: dict, now: datetime | None = None,
                    reset_hint: str | None = None) -> tuple[str, str]:
    """EXPERIMENTAL thresholds (owner decision 6). Returns (mode, reason)."""
    now = now or now_utc()
    if not weekly or weekly.get("used_percent") is None:
        hint = parse_ts(reset_hint)
        while hint and hint <= now:  # the weekly reset recurs; project the last known one forward
            hint += timedelta(days=7)
        if hint:
            h = (hint - now).total_seconds() / 3600
            if h <= policy["harvest_window_hours"]:
                return "UNKNOWN", (f"no weekly measurement; last known reset is in {h:.0f}h (inside the harvest "
                                   "window) - get a reading (statusline or one-line /usage from owner) before deciding")
        return "UNKNOWN", "no weekly measurement; act as BALANCED and do not guess a percentage"
    if weekly.get("stale"):
        return "UNKNOWN", f"weekly measurement is stale ({weekly['age_minutes']} min old); act as BALANCED"
    used = float(weekly["used_percent"])
    reset = parse_ts(weekly.get("resets_at"))
    if reset is None:
        return "UNKNOWN", "weekly measurement has no reset time"
    left_h = max((reset - now).total_seconds() / 3600, 0)
    elapsed_pct = max(0.0, min(100.0, 100 * (1 - left_h / (7 * 24))))
    remaining = 100 - used
    gap = used - elapsed_pct
    basis = f"weekly used {used:.0f}% vs {elapsed_pct:.0f}% of week elapsed; reset in {left_h:.0f}h"
    if left_h <= policy["harvest_window_hours"] and remaining >= policy["harvest_min_remaining_percent"]:
        return "HARVEST", basis + f"; {remaining:.0f}% would expire - work the Claude queue top-down"
    if gap <= -policy["spend_gap_points"]:
        return "SPEND", basis + "; under-using - Claude executes more itself, pulls queue work forward"
    if gap >= policy["conserve_gap_points"]:
        return "CONSERVE", basis + "; ahead of pace - Claude leads/reviews, delegates implementation"
    return "BALANCED", basis


# ---------------------------------------------------------------- git

def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def claims(root: Path) -> dict[str, list[tuple[str, datetime | None]]]:
    """A work item is claimed iff a branch named <prefix>/W-NNNN-... exists (local or remote)."""
    out: dict[str, list] = {}
    for line in git(root, "for-each-ref", "--format=%(refname:short)|%(committerdate:iso-strict)",
                    "refs/heads", "refs/remotes").splitlines():
        ref, _, date = line.partition("|")
        m = re.search(r"(?:^|/)(?:claude|gpt)/(W-\d{4})(?:-|$)", ref)
        if m:
            out.setdefault(m.group(1), []).append((ref, parse_ts(date)))
    return out


# ---------------------------------------------------------------- commands

def cmd_validate(root: Path) -> int:
    errors = validate(root)
    if errors:
        print("CONTROL PLANE INVALID")
        for e in errors:
            print(" -", e)
        return 1
    items, _ = load_queue(root)
    events, _ = load_events(root)
    print(f"CONTROL PLANE VALID (items={len(items)} events={len(events)})")
    return 0


def cmd_mode(root: Path) -> int:
    res = load_json(root, "control/resources.json")
    lim = current_limits(res)
    mode, reason = allocation_mode(lim["claude_weekly"], res["allocation_policy"],
                                   reset_hint=lim.get("claude_weekly_reset_hint"))
    print(f"{mode} (experimental thresholds): {reason}")
    five = lim["claude_five_hour"]
    if five and not five["stale"] and five["used_percent"] >= 90:
        print("note: 5-hour window >= 90% used - checkpoint work before the window closes")
    return 0


def cmd_status(root: Path, hook: bool) -> int:
    if hook:
        sys.stdin.read()  # hook payload unused; drain it
    now = now_utc()
    lines = ["# Control plane status"]
    errors = validate(root)
    if errors:
        lines.append(f"!! INVALID STATE ({len(errors)} errors) - run `python scripts/cp.py validate`")
    branch = git(root, "branch", "--show-current").strip() or "?"
    dirty = [l[3:] for l in git(root, "status", "--porcelain").splitlines()]
    lines.append(f"branch: {branch}" + (f" | uncommitted: {', '.join(dirty[:6])}" + (" ..." if len(dirty) > 6 else "") if dirty else ""))
    try:
        res = load_json(root, "control/resources.json")
        lim = current_limits(res, now=now)
        mode, reason = allocation_mode(lim["claude_weekly"], res["allocation_policy"], now,
                                       lim.get("claude_weekly_reset_hint"))
        lines.append(f"claude mode: {mode} - {reason}")
        five = lim["claude_five_hour"]
        if five:
            lines.append(f"5h window: {five['used_percent']:.0f}% used ({'stale' if five['stale'] else 'fresh'})")
    except (OSError, ValueError, KeyError) as e:
        lines.append(f"claude mode: UNKNOWN ({e})")
    items, _ = load_queue(root)
    claimed = claims(root)
    open_items = [i for i in items if i.get("status") in ("ready", "blocked")]
    done = {i["id"] for i in items if i.get("status") == "done"}
    by: dict[str, list[str]] = {}
    for it in sorted(open_items, key=lambda i: (i.get("priority", "9"), i.get("id", ""))):
        deps = [d.strip() for d in it.get("depends_on", "").split(",") if d.strip()]
        waiting = [d for d in deps if d not in done]
        tag = it["status"]
        refs = claimed.get(it["id"], [])
        if refs:
            latest = max((d for _, d in refs if d), default=None)
            tag = "claimed:" + ",".join(r for r, _ in refs)
            if latest and now - latest > STALE_CLAIM:
                tag += " (STALE)"
        elif waiting:
            tag = "waiting:" + ",".join(waiting)
        by.setdefault(it.get("assignee", "?"), []).append(f"  - {it['id']} P{it.get('priority')} [{tag}] {it.get('title')}")
    lines.append("queue:" if by else "queue: empty")
    for who in ("claude", "gpt56", "gpt6", "owner"):
        if who in by:
            lines.append(f" {who}:")
            lines += by[who]
    print("\n".join(lines))
    return 0


def append_event(root: Path, event: dict) -> None:
    errs = validate_events([dict(event, _line="new")])
    if errs:
        raise SystemExit("refusing to write invalid event: " + "; ".join(errs))
    with (root / "telemetry" / "events.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def cmd_measure(root: Path, args) -> int:
    path = root / "control" / "resources.json"
    res = json.loads(path.read_text(encoding="utf-8"))
    readings = {}
    if args.from_statusline:
        try:
            snap = json.loads(statusline.limits_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("no statusline snapshot found (is the status line configured and are you a subscriber?)")
            return 1
        for name, window in CLAUDE_LIMITS.items():
            w = (snap.get("windows") or {}).get(window)
            if w:
                readings[name] = (w["used_percent"], w["resets_at"], snap["measured_at"], snap["source"])
    else:
        stamp = now_utc().isoformat()
        for name, used, reset in (("claude_weekly", args.weekly_used, args.weekly_reset),
                                  ("claude_five_hour", args.five_hour_used, args.five_hour_reset)):
            if used is not None:
                if parse_ts(reset) is None:
                    raise SystemExit(f"{name}: a manual reading needs its reset time as ISO-8601")
                readings[name] = (used, parse_ts(reset).isoformat(), stamp, "owner-usage-command")
    if not readings:
        print("nothing to record")
        return 1
    for name, (used, reset, measured, source) in readings.items():
        r = res["resources"][name]
        r.update(used_percent=used, resets_at=reset, measured_at=measured, source=source)
        append_event(root, {"ts": measured, "agent": "claude-code", "type": "measurement",
                            "project": DEFAULT_PROJECT, "resource": name, "used_percent": used,
                            "resets_at": reset, "source": source})
    errs = validate_resources(res)
    if errs:
        raise SystemExit("; ".join(errs))
    path.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("recorded: " + ", ".join(f"{n}={v[0]}%" for n, v in readings.items()))
    return 0


def cmd_record_session(root: Path, args) -> int:
    transcript, session_id = args.transcript, args.session_id
    if args.hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        transcript = transcript or payload.get("transcript_path")
        session_id = session_id or payload.get("session_id")
    if not transcript or not Path(transcript).exists():
        print("no transcript; nothing recorded")
        return 0  # a hook must not fail the session
    s = claude_usage.summarize(claude_usage.session_files(Path(transcript)))
    if not s["messages"]:
        return 0
    append_event(root, {
        "ts": s["last_ts"] or now_utc().isoformat(), "agent": "claude-code", "type": "session",
        "project": args.project, "session_id": session_id or (s["sessions"] or [None])[0],
        "first_ts": s["first_ts"], "messages": s["messages"], "owner_prompts": s["owner_prompts"],
        "tokens": s["tokens"], "by_model": s["by_model"], "source": "transcript",
    })
    print(f"recorded session: {s['messages']} messages, {s['owner_prompts']} owner prompts")
    return 0


def cmd_log(root: Path, args) -> int:
    e = {"ts": now_utc().isoformat(), "agent": args.agent, "type": args.type, "project": args.project}
    for k in ("item", "outcome", "failure_class", "evidence", "category", "note"):
        v = getattr(args, k, None)
        if v is not None:
            e[k] = v
    if args.weight is not None:
        e["weight"] = args.weight
    append_event(root, e)
    print("logged", args.type)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="control-plane tool")
    ap.add_argument("--root", type=Path, default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("mode")
    st = sub.add_parser("status")
    st.add_argument("--hook", action="store_true")
    me = sub.add_parser("measure")
    me.add_argument("--from-statusline", action="store_true")
    me.add_argument("--weekly-used", type=float)
    me.add_argument("--weekly-reset")
    me.add_argument("--five-hour-used", type=float)
    me.add_argument("--five-hour-reset")
    rs = sub.add_parser("record-session")
    rs.add_argument("--hook", action="store_true")
    rs.add_argument("--transcript")
    rs.add_argument("--session-id")
    rs.add_argument("--project", default=DEFAULT_PROJECT)
    lg = sub.add_parser("log")
    lg.add_argument("type", choices=sorted(EVENT_TYPES - {"session", "measurement"}))
    lg.add_argument("--agent", default="claude-code")
    lg.add_argument("--project", default=DEFAULT_PROJECT)
    lg.add_argument("--weight", type=int)
    for k in ("item", "outcome", "failure-class", "evidence", "category", "note"):
        lg.add_argument(f"--{k}")
    args = ap.parse_args(argv)
    root = args.root
    if args.cmd == "validate":
        return cmd_validate(root)
    if args.cmd == "mode":
        return cmd_mode(root)
    if args.cmd == "status":
        return cmd_status(root, args.hook)
    if args.cmd == "measure":
        return cmd_measure(root, args)
    if args.cmd == "record-session":
        return cmd_record_session(root, args)
    return cmd_log(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
