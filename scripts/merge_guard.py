"""Qualified agent merge policy for `gh pr merge` (docs/OPERATING_MODEL.md "Qualified agent merge").

STATUS: advisory. GPT-5.6's W-0008 review showed a Bash PreToolUse hook cannot be an authorization
boundary while interpreters are allowed (they can run gh as a child process), so `gh pr merge` stays an
owner-confirmed ask rule and this module only informs that prompt. W-0009 tracks a non-bypassable
enforcement point (separate, non-admin Claude identity + GitHub-side required check).

Called by guard_bash.py. The guard only restricts: a qualifying merge passes through to the normal
permission rules; anything else returns a decision of "ask" (owner approves the Red action
in the prompt) or "deny" (never allowed). Every uncertainty fails closed to "ask".

A merge qualifies only when ALL hold:
  - flags: no --admin (deny); no --auto or --delete-branch (ask); --match-head-commit <sha> present,
    so GitHub refuses the merge if the head moves after this check;
  - the PR's repository is registered in control/projects.json with production: false and agent_merge: true;
  - the PR targets one of that project's protected_branches;
  - the PR is not draft, mergeable == MERGEABLE, mergeStateStatus is CLEAN or HAS_HOOKS,
    no `owner-merge` label, and no failing or pending status checks;
  - --match-head-commit equals the PR head;
  - an independent review (PR review or comment) carries the marker block below for that exact
    head, from an agent that authored none of the PR's commits (`Agent:` trailers; a commit
    without one counts as an unknown author and blocks). Commits by the reviewer that change only
    operational state (OPERATIONAL_PATHS, checked against the commit's real file list) do not
    break independence.

Marker block (each on its own line, in one review/comment body):
  Merge-Recommendation: merge
  Reviewer-Agent: <agent id, e.g. gpt-5.6-sol>
  Reviewed-Head: <full head sha>
  Verification: passed
  Acceptance: met
  Red-Actions: none
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

PR_FIELDS = ("number,url,isDraft,mergeable,mergeStateStatus,baseRefName,headRefOid,"
             "labels,statusCheckRollup,commits,reviews,comments")
OWNER_LABEL = "owner-merge"
MARKER_KEYS = ("Merge-Recommendation", "Reviewer-Agent", "Reviewed-Head", "Verification", "Acceptance", "Red-Actions")
REQUIRED = {"Merge-Recommendation": "merge", "Verification": "passed", "Acceptance": "met", "Red-Actions": "none"}
FLAGS_WITH_VALUE = {"-R", "--repo", "-t", "--subject", "-b", "--body", "-F", "--body-file",
                    "-A", "--author-email", "--match-head-commit"}
GOOD_CHECK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
AGENT_TRAILER = re.compile(r"^Agent:\s*(\S+)\s*$", re.M)
OPERATIONAL_PATHS = ("control/queue/*", "STATE.md", ".ai/STATE.md")


def commit_files(repo: str, oid: str) -> list[str] | None:
    try:
        out = subprocess.run(["gh", "api", f"repos/{repo}/commits/{oid}", "--jq", "[.files[].filename]"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def operational_only(files: list[str] | None) -> bool:
    return bool(files) and all(any(fnmatch.fnmatchcase(f, pat) for pat in OPERATIONAL_PATHS) for f in files)


def parse_merge_args(args: list[str]) -> dict:
    out = {"selector": None, "repo": None, "match_head": None, "flags": set()}
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name in FLAGS_WITH_VALUE:
            if not eq:
                i += 1
                val = args[i] if i < len(args) else ""
            if name in ("-R", "--repo"):
                out["repo"] = val
            elif name == "--match-head-commit":
                out["match_head"] = val
        elif a.startswith("-"):
            out["flags"].add(name)
        elif out["selector"] is None:
            out["selector"] = a
        i += 1
    return out


def fetch_pr(selector: str | None, repo: str | None, cwd: str | None) -> dict | None:
    cmd = ["gh", "pr", "view"] + ([selector] if selector else []) + (["-R", repo] if repo else [])
    try:
        out = subprocess.run(cmd + ["--json", PR_FIELDS], cwd=cwd or None, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def repo_of(url: str) -> str | None:
    m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/pull/\d+", url or "")
    return m.group(1).lower() if m else None


def markers(body: str) -> dict:
    found = {}
    for key in MARKER_KEYS:
        m = re.search(rf"^\s*{re.escape(key)}:\s*(\S+)\s*$", body or "", re.M | re.I)
        if m:
            found[key] = m.group(1)
    return found


SELF_AGENT = "claude-code"


def impersonates(text: str, self_agent: str = SELF_AGENT) -> bool:
    """True if a body Claude is about to post carries a marker block not signed as Claude itself."""
    if not re.search(r"^\s*(Merge-Recommendation|Reviewer-Agent|Reviewed-Head):", text or "", re.M | re.I):
        return False
    return markers(text).get("Reviewer-Agent", "").lower() != self_agent


def evaluate(args: list[str], cwd: str | None, registry: dict, fetch=fetch_pr,
             files=commit_files) -> tuple[str, str] | None:
    """None if the merge qualifies, else (decision, reason) with decision in {"ask", "deny"}."""
    p = parse_merge_args(args)
    if "--admin" in p["flags"]:
        return "deny", "`--admin` bypasses branch protection and is never used by agents"
    if "--auto" in p["flags"]:
        return "ask", "`--auto` merges later against state this guard cannot check"
    if p["flags"] & {"-d", "--delete-branch"}:
        return "ask", "deleting the head branch is a Red action"
    if not p["match_head"]:
        return "ask", "add `--match-head-commit <reviewed head sha>` so GitHub enforces the reviewed head"

    pr = fetch(p["selector"], p["repo"], cwd)
    if not pr:
        return "ask", "could not read the PR through authenticated gh"
    repo = repo_of(pr.get("url", ""))
    project = next((n for n, e in (registry.get("projects") or {}).items()
                    if str(e.get("repository", "")).lower() == repo), None)
    if not project:
        return "ask", f"repository {repo!r} is not a registered project"
    entry = registry["projects"][project]
    if entry.get("production") is not False:
        return "ask", f"project {project!r} is production (or unspecified); owner merges"
    if entry.get("agent_merge") is not True:
        return "ask", f"project {project!r} does not enable agent_merge"
    base = pr.get("baseRefName") or ""
    if not any(fnmatch.fnmatchcase(base, pat) for pat in entry.get("protected_branches") or []):
        return "ask", f"base `{base}` is not a registered protected branch of {project!r}"
    if pr.get("isDraft"):
        return "ask", "PR is a draft"
    if pr.get("mergeable") != "MERGEABLE":
        return "ask", f"GitHub reports mergeable={pr.get('mergeable')}"
    if pr.get("mergeStateStatus") not in ("CLEAN", "HAS_HOOKS"):
        return "ask", f"GitHub reports mergeStateStatus={pr.get('mergeStateStatus')}"
    if any((lb.get("name") or "").lower() == OWNER_LABEL for lb in pr.get("labels") or []):
        return "ask", f"PR carries the `{OWNER_LABEL}` label"
    for c in pr.get("statusCheckRollup") or []:
        state = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if state not in GOOD_CHECK:
            return "ask", f"check {c.get('name') or c.get('context')!r} is {state or 'unknown'}"
    head = pr.get("headRefOid") or ""
    if not head or p["match_head"] != head:
        return "ask", "--match-head-commit does not equal the PR head"

    authors: dict[str, list[str]] = {}
    for c in pr.get("commits") or []:
        found = AGENT_TRAILER.findall(c.get("messageBody") or "")
        if not found:
            return "ask", f"commit {(c.get('oid') or '')[:7]} has no Agent: trailer, so independence is unknown"
        for a in found:
            authors.setdefault(a.lower(), []).append(c.get("oid") or "")

    problems = []
    for item in (pr.get("reviews") or []) + (pr.get("comments") or []):
        m = markers(item.get("body") or "")
        if not m.get("Merge-Recommendation"):
            continue
        missing = [k for k in MARKER_KEYS if k not in m]
        if missing:
            problems.append(f"marker block missing {missing}")
            continue
        wrong = [k for k, v in REQUIRED.items() if m[k].lower() != v]
        if wrong:
            problems.append(f"marker values not satisfied: {wrong}")
            continue
        if m["Reviewed-Head"].lower() != head.lower():
            problems.append("review is for an older head")
            continue
        own = authors.get(m["Reviewer-Agent"].lower(), [])
        if own and not all(operational_only(files(repo, oid)) for oid in own):
            problems.append(f"reviewer {m['Reviewer-Agent']} authored non-operational commits")
            continue
        return None
    detail = "; ".join(problems) if problems else "no review carries the merge marker block"
    return "ask", f"no qualifying independent review for head {head[:7]} ({detail})"


def body_texts(args: list[str], cwd: str | None) -> list[str]:
    """Bodies a `gh pr comment|review|create|edit` command would post, including --body-file contents."""
    texts, i = [], 0
    while i < len(args):
        name, eq, val = args[i].partition("=")
        if name in ("-b", "--body", "-F", "--body-file"):
            if not eq:
                i += 1
                val = args[i] if i < len(args) else ""
            if name in ("-F", "--body-file"):
                try:
                    path = Path(val) if Path(val).is_absolute() else Path(cwd or ".") / val
                    texts.append(path.read_text(encoding="utf-8"))
                except OSError:
                    texts.append("")
            else:
                texts.append(val)
        i += 1
    return texts
