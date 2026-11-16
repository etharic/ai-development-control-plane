#!/usr/bin/env python3
"""Claude Code status line that also records measured plan-usage limits.

Claude Code pipes a JSON payload to the status line command. For Claude.ai
subscribers it includes `rate_limits.five_hour` / `rate_limits.seven_day`
({used_percentage, resets_at}) taken from the API's own rate-limit reporting.
That is a measurement, not an estimate, so it is recorded verbatim to a
machine-local snapshot (default ~/.claude/control-plane/limits.json) that
`scripts/cp.py` reads. Nothing is recorded when the fields are absent.

Must never fail loudly: any error just prints a minimal line.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

WINDOWS = ("five_hour", "seven_day")


def limits_file() -> Path:
    override = os.environ.get("CP_LIMITS_FILE")
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "control-plane" / "limits.json"


def extract(payload: dict) -> dict | None:
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out = {}
    for w in WINDOWS:
        v = rl.get(w)
        if isinstance(v, dict) and isinstance(v.get("used_percentage"), (int, float)) and isinstance(v.get("resets_at"), (int, float)):
            out[w] = {
                "used_percent": round(float(v["used_percentage"]), 1),
                "resets_at": datetime.fromtimestamp(v["resets_at"], timezone.utc).isoformat(),
            }
    return out or None


def record(windows: dict, path: Path, now: datetime) -> None:
    snapshot = {"measured_at": now.isoformat(), "source": "claude-code-statusline", "windows": windows}
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("windows")
    except (OSError, ValueError):
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    if previous != windows:  # history only on change, for burn-rate analysis
        with path.with_name("limits_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")


def render(payload: dict, windows: dict | None) -> str:
    parts = [((payload.get("model") or {}).get("display_name")) or "Claude"]
    ctx = (payload.get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        parts.append(f"ctx {ctx:.0f}%")
    if windows:
        if "five_hour" in windows:
            parts.append(f"5h {windows['five_hour']['used_percent']:.0f}%")
        if "seven_day" in windows:
            w = windows["seven_day"]
            reset = datetime.fromisoformat(w["resets_at"]).astimezone().strftime("%a %H:%M")
            parts.append(f"wk {w['used_percent']:.0f}% (resets {reset})")
    return " | ".join(parts)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        windows = extract(payload)
        if windows:
            try:
                record(windows, limits_file(), datetime.now(timezone.utc))
            except OSError:
                pass
        print(render(payload, windows))
    except Exception:  # noqa: BLE001 - a status line must not crash
        print("Claude")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
