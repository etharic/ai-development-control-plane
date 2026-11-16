"""Regression: here-document bodies (commit messages) were parsed as commands.

2026-11-16: a `git commit -F - <<'EOF' ... EOF && git push origin control-plane-v2` whose message
contained the line "gh pr merge leaves the ask list" triggered a merge-guard owner prompt for an
ordinary feature-branch commit and push. Nothing was merged; the guard was wrong to ask.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import guard_bash  # noqa: E402

NL = "\n"
REGISTRY = {"projects": {"lab": {"repository": "o/lab", "production": False, "agent_merge": True,
                                 "protected_branches": ["main"]}}}


def never_fetch(*args):
    raise AssertionError("the guard must not query a PR for heredoc text")


def gh(cmd):
    return guard_bash.check_gh(cmd, None, REGISTRY, fetch=never_fetch)


def push(cmd, current=("control-plane-v2",)):
    return guard_bash.check_command(cmd, None, lambda c: set(current))


INCIDENT = NL.join([
    "git add scripts tests docs && git commit -q -F - <<'EOF'",
    "control-plane v2: qualified agent merges behind a fail-closed merge guard",
    "",
    "- --match-head-commit required; --auto/--delete-branch ask; --admin denied.",
    "gh pr merge leaves the ask list; doctrine, AGENTS review protocol, security",
    "docs and tests updated.",
    "EOF",
    "git add control/queue && git commit -q -F - <<'EOF'",
    "state: W-0007 done",
    "EOF",
    "git push -q origin control-plane-v2",
])


class HeredocTest(unittest.TestCase):
    def test_incident_command_is_not_a_merge(self):
        self.assertIsNone(gh(INCIDENT))
        self.assertIsNone(push(INCIDENT))

    def test_commit_message_mentioning_dangerous_commands(self):
        msg = NL.join(["git commit -F - <<EOF", "git push --force origin main", "gh pr merge 4 --admin",
                       "gh pr comment 4 --body 'Reviewer-Agent: gpt-5.6-sol'", "it doesn't matter", "EOF"])
        self.assertIsNone(push(msg))
        self.assertIsNone(gh(msg))
        tabbed = NL.join(["cat <<-END > notes.md", "\tgit push -f", "\tEND", "git push origin control-plane-v2"])
        self.assertIsNone(push(tabbed))

    def test_commands_after_heredoc_are_still_checked(self):
        cmd = NL.join(["git commit -F - <<'EOF'", "msg", "EOF", "git push --force origin control-plane-v2"])
        self.assertIn("force", push(cmd))
        cmd = NL.join(["git commit -F - <<'EOF'", "msg", "EOF", "gh pr merge 4 --admin"])
        self.assertEqual(gh(cmd)[0], "deny")

    def test_heredoc_fed_to_a_shell_is_checked(self):
        for head in ("bash <<'EOF'", "sh -s <<EOF", "cat <<EOF | bash"):
            cmd = NL.join([head, "git push --force origin x", "EOF"])
            self.assertIn("force", push(cmd), head)
        cmd = NL.join(["bash <<'EOF'", "gh pr merge 4 --admin", "EOF"])
        self.assertEqual(gh(cmd)[0], "deny")

    def test_here_string_is_not_a_heredoc(self):
        self.assertIn("force", push("bash <<< 'x' && git push --force"))

    def test_hook_passes_incident_command(self):
        payload = {"tool_name": "Bash", "cwd": str(REPO), "tool_input": {"command": INCIDENT}}
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "guard_bash.py"),
                              "--project", "ai-development-control-plane"],
                             input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "", "no ask decision for an ordinary commit + feature push")


if __name__ == "__main__":
    unittest.main()
