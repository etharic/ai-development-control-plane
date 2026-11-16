#!/usr/bin/env python3
"""Exact Claude Code token accounting from local session transcripts.

Source: ~/.claude/projects/<project>/<session>.jsonl (and nested subagent
transcripts). Each assistant API response carries `message.usage`; a single
response can be split over several transcript lines that repeat the same
`message.id` and usage, so messages are de-duplicated by id.

This reports token counts only. It never converts tokens into an allowance
percentage: Anthropic does not publish the weighting, so any conversion would
be an inference, not a measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_owner_prompt(entry: dict) -> bool:
    """A prompt typed by the human, not a tool result or harness-injected message."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "tool_result" not in kinds and "text" in kinds
    return False


def iter_entries(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partially written trailing line
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def session_files(transcript: Path) -> list[Path]:
    """The main transcript plus any nested transcripts (e.g. subagents) in <session>/."""
    files = [transcript]
    nested = transcript.with_suffix("")
    if nested.is_dir():
        files.extend(sorted(nested.rglob("*.jsonl")))
    return files


def new_totals() -> dict:
    return {f: 0 for f in TOKEN_FIELDS}


def summarize(files, since: datetime | None = None, seen: set | None = None) -> dict:
    """Aggregate de-duplicated usage. `seen` lets callers de-duplicate across sessions."""
    seen = set() if seen is None else seen
    totals, by_model = new_totals(), {}
    owner_prompts = messages = 0
    sessions, first, last, cwd = set(), None, None, None
    for path in files:
        for e in iter_entries(path):
            ts = parse_ts(e.get("timestamp"))
            if since and (ts is None or ts < since):
                continue
            if is_owner_prompt(e):
                owner_prompts += 1
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            usage = msg.get("usage")
            key = msg.get("id") or e.get("requestId") or e.get("uuid")
            if not isinstance(usage, dict) or key is None or key in seen:
                continue
            if msg.get("model") == "<synthetic>":
                continue
            seen.add(key)
            messages += 1
            model = msg.get("model") or "unknown"
            m = by_model.setdefault(model, new_totals())
            for f in TOKEN_FIELDS:
                v = usage.get(f) or 0
                if isinstance(v, int):
                    totals[f] += v
                    m[f] += v
            if e.get("sessionId"):
                sessions.add(e["sessionId"])
            cwd = cwd or e.get("cwd")
            if ts:
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
    return {
        "messages": messages,
        "owner_prompts": owner_prompts,
        "tokens": totals,
        "by_model": by_model,
        "sessions": sorted(sessions),
        "cwd": cwd,
        "first_ts": first.isoformat() if first else None,
        "last_ts": last.isoformat() if last else None,
    }


def report(since: datetime | None, root: Path | None = None) -> dict:
    """All projects, de-duplicated globally (resumed sessions can repeat messages)."""
    root = root or projects_dir()
    seen: set = set()
    out = {}
    for project in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        files = sorted(project.rglob("*.jsonl"))
        s = summarize(files, since, seen)
        if s["messages"] or s["owner_prompts"]:
            s["sessions"] = len(s["sessions"])
            out[project.name] = s
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("session", help="summarize one session transcript")
    s.add_argument("transcript", type=Path)
    r = sub.add_parser("report", help="all projects, globally de-duplicated")
    r.add_argument("--since", help="ISO timestamp, e.g. the last weekly reset")
    r.add_argument("--root", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "session":
        result = summarize(session_files(args.transcript))
    else:
        since = parse_ts(args.since) if args.since else None
        if args.since and since is None:
            ap.error(f"bad --since: {args.since}")
        result = report(since, args.root)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
