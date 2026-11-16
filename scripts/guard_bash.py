#!/usr/bin/env python3
"""PreToolUse guard for Bash: refuse git pushes that permission globs cannot reliably catch.

Claude Code permission rules match command prefixes, so `git push` (bare, while on
main), `git -C dir push --force`, `git push origin HEAD:main` or `+branch` refspecs
slip past simple deny patterns. This hook parses each `git ... push` in the command
and blocks (exit 2, reason on stderr) when it would:
  - force (--force, -f, --force-with-lease, +refspec),
  - delete remote refs (--delete, -d, :ref, --prune),
  - push everything (--all, --mirror),
  - target a protected branch (explicitly, or implicitly via the current branch or @{push}).

Protected branches = the baseline below plus the project's `protected_branches` in
control/projects.json (`--project NAME`; glob patterns such as `release/*` allowed).
An unknown project or unreadable registry keeps the baseline and says so on stderr.

It is defence in depth next to GitHub branch protection (docs/SECURITY.md), not a sandbox.
Any other command is allowed through to the normal permission rules.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_guard  # noqa: E402

PROTECTED = frozenset({"main", "master", "production"})  # baseline, always protected
LAB_ROOT = Path(os.environ.get("CP_ROOT") or Path(__file__).resolve().parents[1])
FORCE_FLAGS = {"--force", "-f", "--force-with-lease", "--force-if-includes"}
DELETE_FLAGS = {"--delete", "-d", "--prune"}
BULK_FLAGS = {"--all", "--mirror", "--branches"}
# git global options that take a separate value argument
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# push options that take a separate value argument
PUSH_OPTS_WITH_VALUE = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
OPERATOR_CHARS = "();<>|&\n"


def _branch(ref: str) -> str:
    ref = ref.strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _git(cwd: str | None, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def implicit_targets(cwd: str | None) -> set[str]:
    """Branches a refspec-less push (or `HEAD`) would update: the current branch and its push target."""
    targets = set()
    current = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if current and current != "HEAD":
        targets.add(current)
    push = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
    if push:
        targets.add(_branch(push.split("/", 1)[1] if "/" in push else push))
    return targets


def load_protected(project: str | None, root: Path = LAB_ROOT) -> tuple[frozenset, str | None]:
    """Baseline plus the registered project's protected branches; second value is a warning."""
    if not project:
        return PROTECTED, None
    try:
        reg = json.loads((root / "control" / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return PROTECTED, f"cannot read project registry ({e}); baseline protection only"
    entry = (reg.get("projects") or {}).get(project)
    if not entry:
        return PROTECTED, f"project {project!r} not registered; baseline protection only"
    extra = [b for b in entry.get("protected_branches") or [] if isinstance(b, str) and b]
    return PROTECTED | frozenset(extra), None


def _hits(targets: set[str], protected) -> list[str]:
    return sorted(t for t in targets if any(fnmatch.fnmatchcase(t, pat) for pat in protected))


def check_push(args: list[str], cwd: str | None, resolve=implicit_targets,
               protected=PROTECTED) -> str | None:
    """Return a refusal reason for `git push <args>`, or None if allowed."""
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in PUSH_OPTS_WITH_VALUE:
            skip = True
            continue
        flag = a.split("=", 1)[0]
        if flag in FORCE_FLAGS or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]):
            return "force push is blocked"
        if flag in DELETE_FLAGS or (a.startswith("-") and not a.startswith("--") and "d" in a[1:]):
            return "deleting remote refs is blocked"
        if flag in BULK_FLAGS:
            return f"`{flag}` pushes every branch, including protected ones"
        if a.startswith("-"):
            continue
        positional.append(a)

    refspecs = positional[1:]  # first positional is the remote
    if not refspecs:
        hit = _hits(resolve(cwd), protected)
        if hit:
            return f"refspec-less push would update protected branch `{hit[0]}`"
        return None
    for spec in refspecs:
        if spec.startswith("+"):
            return f"forced refspec `{spec}` is blocked"
        src, _, dst = spec.partition(":")
        if spec.startswith(":"):
            return f"deleting remote ref `{spec}` is blocked"
        dst = dst or src
        targets = resolve(cwd) if _branch(dst) == "HEAD" else {_branch(dst)}
        hit = _hits(targets, protected)
        if hit:
            return f"direct push to protected branch `{hit[0]}` is blocked; open a PR"
    return None


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, respecting quotes (raises ValueError if unbalanced)."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=OPERATOR_CHARS)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    out, cur = [], []
    for tok in lex:
        if tok and set(tok) <= set(OPERATOR_CHARS):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _simple_commands(parsed: list[list[str]], cwd: str | None):
    """Yield (words, cwd) per simple command, tracking `cd` and stripping env assignments."""
    for words in parsed:
        if words[:1] == ["cd"] and len(words) == 2:
            cwd = words[1]
            continue
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if words:
            yield words, cwd


def _git_subcommand(words: list[str], cwd: str | None) -> tuple[int, str | None]:
    i, seg_cwd = 1, cwd
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(words):
            if opt == "-C":
                seg_cwd = words[i + 1]
            i += 2
        else:
            i += 1
    return i, seg_cwd


def check_command(command: str, cwd: str | None, resolve=implicit_targets,
                  protected=PROTECTED) -> str | None:
    """Push policy only. Return a refusal reason, or None."""
    if "push" not in command:
        return None
    try:
        parsed = segments(command)
    except ValueError:
        if re.search(r"(^|[\s;&|(])git(\.exe)?\s+(-\S+\s+)*push\b", command):
            return "could not parse a git push command; run it in a simpler form"
        return None
    for words, seg in _simple_commands(parsed, cwd):
        if words[0] not in ("git", "git.exe"):
            continue
        i, seg_cwd = _git_subcommand(words, seg)
        if i < len(words) and words[i] == "push":
            reason = check_push(words[i + 1:], seg_cwd, resolve, protected)
            if reason:
                return reason
    return None


def check_gh(command: str, cwd: str | None, registry: dict, fetch=merge_guard.fetch_pr) -> tuple[str, str] | None:
    """Merge policy and reviewer-marker protection. Return (decision, reason) or None."""
    if "gh" not in command:
        return None
    try:
        parsed = segments(command)
    except ValueError:
        if re.search(r"(^|[\s;&|(])gh(\.exe)?\s+pr\s+(merge|comment|review|create|edit)\b", command):
            return "ask", "could not parse a gh pr command"
        return None
    for words, seg in _simple_commands(parsed, cwd):
        if words[0] not in ("gh", "gh.exe") or words[1:2] != ["pr"] or len(words) < 3:
            continue
        sub, rest = words[2], words[3:]
        if sub == "merge":
            result = merge_guard.evaluate(rest, seg, registry, fetch)
            if result:
                return result
        elif sub in ("comment", "review", "create", "edit"):
            if any(merge_guard.impersonates(b) for b in merge_guard.body_texts(rest, seg)):
                return "deny", ("a merge marker block posted by Claude must be signed "
                                f"`Reviewer-Agent: {merge_guard.SELF_AGENT}`; never review as another agent")
    return None


def load_registry(root: Path) -> dict:
    try:
        return json.loads((root / "control" / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PreToolUse git-push and merge guard")
    ap.add_argument("--project", help="registered project whose protected_branches apply")
    ap.add_argument("--root", type=Path, default=LAB_ROOT, help="control-plane repository")
    args = ap.parse_args(argv)
    protected, warning = load_protected(args.project, args.root)
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd")
    reason = check_command(command, cwd, protected=protected)
    if reason and warning:
        reason += f" ({warning})"
    elif warning and "push" in command:
        print(f"control-plane guard: {warning}", file=sys.stderr)
    if reason:
        print(f"Blocked by control-plane guard (scripts/guard_bash.py): {reason}. "
              "Protected-branch merges and destructive pushes are Red actions for the owner.", file=sys.stderr)
        return 2
    gh = check_gh(command, cwd, load_registry(args.root))
    if gh:
        decision, why = gh
        if decision == "deny":
            print(f"Blocked by control-plane guard (scripts/guard_bash.py): {why}.", file=sys.stderr)
            return 2
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"Not a qualified agent merge: {why}. Owner approval required."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
