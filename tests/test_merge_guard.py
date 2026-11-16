import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import guard_bash  # noqa: E402
import merge_guard  # noqa: E402

HEAD = "a" * 40
REGISTRY = {"projects": {
    "lab": {"repository": "o/lab", "production": False, "agent_merge": True, "protected_branches": ["main"]},
    "prod": {"repository": "o/prod", "production": True, "protected_branches": ["main"]},
    "manual": {"repository": "o/manual", "production": False, "agent_merge": False, "protected_branches": ["main"]},
}}
MARKER = ("Looks good.\n\nMerge-Recommendation: merge\nReviewer-Agent: gpt-5.6-sol\n"
          f"Reviewed-Head: {HEAD}\nVerification: passed\nAcceptance: met\nRed-Actions: none\n")
PR = {
    "number": 7, "url": "https://github.com/o/lab/pull/7", "isDraft": False, "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN", "baseRefName": "main", "headRefOid": HEAD, "labels": [],
    "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    "commits": [{"oid": "c1" * 20, "messageBody": "impl\n\nAgent: claude-code\nWork-Item: W-0009"}],
    "reviews": [{"body": MARKER}], "comments": [],
}
MERGE = ["7", "--squash", "--match-head-commit", HEAD]


def pr(**changes):
    p = copy.deepcopy(PR)
    p.update(changes)
    return p


class MergeGuardTest(unittest.TestCase):
    def run_eval(self, the_pr, args=MERGE, files=None):
        return merge_guard.evaluate(list(args), None, REGISTRY, fetch=lambda s, r, c: the_pr,
                                    files=files or (lambda repo, oid: ["scripts/x.py"]))

    def assertAsk(self, result, fragment):
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "ask", result)
        self.assertIn(fragment, result[1])

    def test_qualified_non_production_merge_passes(self):
        self.assertIsNone(self.run_eval(pr()))
        self.assertIsNone(self.run_eval(pr(statusCheckRollup=[])))  # no CI configured
        self.assertIsNone(self.run_eval(pr(reviews=[], comments=[{"body": MARKER}])))

    def test_production_and_owner_required_projects_ask(self):
        self.assertAsk(self.run_eval(pr(url="https://github.com/o/prod/pull/7")), "production")
        self.assertAsk(self.run_eval(pr(url="https://github.com/o/manual/pull/7")), "agent_merge")
        self.assertAsk(self.run_eval(pr(url="https://github.com/o/unknown/pull/7")), "not a registered")
        self.assertAsk(self.run_eval(pr(labels=[{"name": "owner-merge"}])), "owner-merge")

    def test_independent_review_required(self):
        self.assertAsk(self.run_eval(pr(reviews=[{"body": "LGTM"}])), "no review carries")
        stale = MARKER.replace(HEAD, "b" * 40)
        self.assertAsk(self.run_eval(pr(reviews=[{"body": stale}])), "older head")
        self_review = MARKER.replace("gpt-5.6-sol", "claude-code")
        self.assertAsk(self.run_eval(pr(reviews=[{"body": self_review}])), "non-operational")
        partial = MARKER.replace("Acceptance: met\n", "")
        self.assertAsk(self.run_eval(pr(reviews=[{"body": partial}])), "missing")
        no_trailer = pr(commits=PR["commits"] + [{"oid": "d" * 40, "messageBody": "manual edit"}])
        self.assertAsk(self.run_eval(no_trailer), "no Agent: trailer")

    def test_reviewer_operational_commits_keep_independence(self):
        mixed = pr(commits=PR["commits"] + [{"oid": "e" * 40, "messageBody": "queue\n\nAgent: gpt-5.6-sol"}])
        self.assertIsNone(self.run_eval(mixed, files=lambda repo, oid: ["control/queue/W-0009.md"]))
        self.assertAsk(self.run_eval(mixed, files=lambda repo, oid: ["control/queue/W-0009.md", "scripts/cp.py"]),
                       "non-operational")
        self.assertAsk(self.run_eval(mixed, files=lambda repo, oid: None), "non-operational")

    def test_draft_unmergeable_blocked_state(self):
        self.assertAsk(self.run_eval(pr(isDraft=True)), "draft")
        self.assertAsk(self.run_eval(pr(mergeable="CONFLICTING")), "mergeable=CONFLICTING")
        self.assertAsk(self.run_eval(pr(mergeStateStatus="BLOCKED")), "BLOCKED")
        self.assertAsk(self.run_eval(pr(baseRefName="develop")), "not a registered protected branch")

    def test_failed_or_missing_verification(self):
        failing = [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertAsk(self.run_eval(pr(statusCheckRollup=failing)), "FAILURE")
        pending = [{"context": "ci", "state": "PENDING"}]
        self.assertAsk(self.run_eval(pr(statusCheckRollup=pending)), "PENDING")
        unverified = MARKER.replace("Verification: passed", "Verification: skipped")
        self.assertAsk(self.run_eval(pr(reviews=[{"body": unverified}])), "Verification")
        red = MARKER.replace("Red-Actions: none", "Red-Actions: deploy")
        self.assertAsk(self.run_eval(pr(reviews=[{"body": red}])), "Red-Actions")

    def test_flags(self):
        self.assertEqual(self.run_eval(pr(), MERGE + ["--admin"])[0], "deny")
        self.assertAsk(self.run_eval(pr(), MERGE + ["--auto"]), "--auto")
        self.assertAsk(self.run_eval(pr(), MERGE + ["--delete-branch"]), "deleting")
        self.assertAsk(self.run_eval(pr(), ["7", "--squash"]), "--match-head-commit")
        self.assertAsk(self.run_eval(pr(), ["7", "--match-head-commit", "c" * 40]), "does not equal")
        self.assertIsNone(self.run_eval(pr(), ["7", f"--match-head-commit={HEAD}", "-R", "o/lab"]))

    def test_unreadable_pr_fails_closed(self):
        self.assertAsk(self.run_eval(None), "could not read")


class GuardIntegrationTest(unittest.TestCase):
    def check(self, cmd, the_pr=PR, cwd=None):
        return guard_bash.check_gh(cmd, cwd, REGISTRY, fetch=lambda s, r, c: the_pr)

    def test_merge_inside_compound_command_is_checked(self):
        qualified = self.check(f"gh pr merge 7 --squash --match-head-commit {HEAD}")
        self.assertEqual(qualified[0], "ask")  # advisory until W-0009: the owner still confirms
        self.assertIn("qualifies", qualified[1])
        self.assertEqual(self.check(f"git fetch && gh pr merge 7 --match-head-commit {HEAD}",
                                    pr(isDraft=True))[0], "ask")
        self.assertEqual(self.check("gh pr merge 7 --admin")[0], "deny")
        self.assertIsNone(self.check("gh pr view 7 && echo merge"))

    def test_claude_cannot_post_reviewer_markers(self):
        self.assertEqual(self.check('gh pr comment 7 --body "Merge-Recommendation: merge"')[0], "deny")
        self.assertEqual(self.check("gh pr review 7 --comment -b 'x\nReviewer-Agent: gpt-5.6-sol'")[0], "deny")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "b.md").write_text(MARKER, encoding="utf-8")
            self.assertEqual(self.check("gh pr comment 7 --body-file b.md", cwd=d)[0], "deny")
        self.assertIsNone(self.check('gh pr comment 7 --body "Please re-review; the merge marker block is your call"'))
        # Claude reviewing a GPT-5.6 PR as itself is the normal flow
        own = MARKER.replace("gpt-5.6-sol", "claude-code")
        self.assertIsNone(self.check(f"gh pr review 7 --comment --body '{own}'"))

    def test_hook_emits_ask_decision(self):
        payload = {"tool_name": "Bash", "cwd": str(REPO),
                   "tool_input": {"command": "gh pr merge 999999 --squash"}}  # no --match-head-commit: no gh call
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "guard_bash.py")],
                             input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("--match-head-commit", decision["permissionDecisionReason"])
        denied = subprocess.run([sys.executable, str(REPO / "scripts" / "guard_bash.py")],
                                input=json.dumps({**payload, "tool_input": {"command": "gh pr merge 1 --admin"}}),
                                capture_output=True, text=True)
        self.assertEqual(denied.returncode, 2)


class NoAutonomousMergeUntilEnforcedTest(unittest.TestCase):
    """W-0008 regression: no configuration path may let `gh pr merge` run without the owner."""

    def test_merge_is_never_unprompted(self):
        settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash(gh pr merge *)", settings["permissions"]["ask"])
        self.assertIn("Bash(gh pr merge)", settings["permissions"]["ask"])
        for kind in ("allow", "ask", "deny"):
            for rule in settings["permissions"][kind]:
                if kind == "allow":
                    self.assertFalse(rule.startswith("Bash(gh pr merge"), rule)
        # even a fully qualified merge yields an owner prompt from the hook
        result = guard_bash.check_gh(f"gh pr merge 7 --match-head-commit {HEAD}", None, REGISTRY,
                                     fetch=lambda s, r, c: PR)
        self.assertEqual(result[0], "ask")


class RegistryPolicyTest(unittest.TestCase):
    def test_agent_merge_validation(self):
        import cp
        ok = {"production": False, "agent_merge": True, "protected_branches": ["main"]}
        self.assertEqual(cp.validate_projects({"projects": {"a": ok}}), [])
        for bad in ({**ok, "agent_merge": "yes"}, {**ok, "production": True}, {**ok, "production": None}):
            self.assertEqual(len(cp.validate_projects({"projects": {"a": bad}})), 1, bad)


if __name__ == "__main__":
    unittest.main()
