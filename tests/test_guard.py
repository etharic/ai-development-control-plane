import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import guard_bash  # noqa: E402


def on(*branches):
    return lambda cwd: set(branches)


class GuardBashTest(unittest.TestCase):
    def blocked(self, cmd, current=("feature",)):
        return guard_bash.check_command(cmd, None, on(*current))

    def test_allows_feature_pushes(self):
        for cmd in ["git push", "git push -u origin claude/W-0005-perms", "git push origin HEAD",
                    "git push origin feature:feature", "git status && git push origin feat",
                    "git -C ../repo push origin topic", "echo push"]:
            self.assertIsNone(self.blocked(cmd), cmd)

    def test_quoted_text_is_not_a_push(self):
        for cmd in ["sed -i 's/\"Bash(git push * main)\", \"x|y\";/z/' f.json",
                    'git commit -m "never git push --force; or | main"',
                    "grep -n 'git push --force' docs/x.md | head"]:
            self.assertIsNone(self.blocked(cmd), cmd)

    def test_sees_through_operators_and_redirects(self):
        self.assertIsNotNone(self.blocked("(cd x && git push --force) 2>&1"))
        self.assertIsNotNone(self.blocked("git push origin main 2>&1 | tail"))
        self.assertIsNotNone(self.blocked("FOO=1 git push -f"))
        self.assertIsNotNone(self.blocked("true\ngit push origin main"))

    def test_unparseable_push_fails_closed(self):
        self.assertIsNotNone(self.blocked("git push origin 'main"))
        self.assertIsNone(self.blocked("echo 'unbalanced push"))

    def test_blocks_force(self):
        for cmd in ["git push --force", "git push -f origin feat", "git push origin feat --force-with-lease",
                    "git push --force-with-lease=feat:abc origin feat", "git push -uf origin feat",
                    "git push origin +feat", "git -C x push --force origin feat"]:
            self.assertIn("force", self.blocked(cmd), cmd)

    def test_blocks_protected_targets(self):
        for cmd in ["git push origin main", "git push -u origin main", "git push origin HEAD:main",
                    "git push origin feat:refs/heads/master", "git add . && git commit -m x && git push origin production"]:
            self.assertIn("protected", self.blocked(cmd), cmd)

    def test_blocks_implicit_push_from_protected_branch(self):
        self.assertIn("protected", self.blocked("git push", current=("main",)))
        self.assertIn("protected", self.blocked("git push origin", current=("main",)))
        self.assertIn("protected", self.blocked("git push origin HEAD", current=("main",)))
        # feature branch whose push target is main
        self.assertIn("protected", self.blocked("git push", current=("feat", "main")))

    def test_blocks_delete_and_bulk(self):
        for cmd in ["git push --delete origin feat", "git push origin -d feat", "git push origin :feat",
                    "git push --prune origin", "git push --all", "git push --mirror origin"]:
            self.assertIsNotNone(self.blocked(cmd), cmd)

    def test_hook_entrypoint(self):
        def run(payload):
            return subprocess.run([sys.executable, str(REPO / "scripts" / "guard_bash.py")],
                                  input=json.dumps(payload), capture_output=True, text=True)
        bad = run({"tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": str(REPO)})
        self.assertEqual(bad.returncode, 2)
        self.assertIn("force", bad.stderr)
        ok = run({"tool_name": "Bash", "tool_input": {"command": "python -m unittest"}, "cwd": str(REPO)})
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(run({"tool_name": "Read", "tool_input": {}}).returncode, 0)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.perms = self.settings["permissions"]

    def test_hard_safeguards_present(self):
        deny = set(self.perms["deny"])
        for rule in ["Bash(git push --force*)", "Bash(git push origin main)", "Bash(git push --delete *)",
                     "Bash(git credential *)", "Bash(gh auth token*)", "Bash(gh secret *)",
                     "Bash(gh repo delete *)", "Bash(gh repo edit *)", "Read(**/.env)", "Read(**/*.key)",
                     "Read(~/.ssh/**)", "Read(~/.git-credentials)", "Edit(./.git/**)"]:
            self.assertIn(rule, deny)
        ask = set(self.perms["ask"])
        self.assertNotIn("Bash(gh pr merge *)", ask)  # merge policy lives in merge_guard (W-0007)
        for rule in ["Bash(gh release create *)", "Bash(npm publish*)",
                     "Bash(cargo publish*)", "Bash(git branch -D *)", "Edit(./.claude/**)"]:
            self.assertIn(rule, ask)

    def test_guard_hook_wired(self):
        hooks = self.settings["hooks"]["PreToolUse"]
        self.assertTrue(any(h.get("matcher") == "Bash" and "guard_bash.py" in c["command"]
                            for h in hooks for c in h["hooks"]))

    def test_no_rule_in_two_lists(self):
        lists = [set(self.perms[k]) for k in ("allow", "ask", "deny")]
        self.assertFalse(lists[0] & lists[1] or lists[0] & lists[2] or lists[1] & lists[2])

    def test_rule_syntax(self):
        import re
        pattern = re.compile(r"^(Bash|Read|Edit|WebFetch|WebSearch)(\(.+\))?$")
        for k in ("allow", "ask", "deny"):
            for rule in self.perms[k]:
                self.assertRegex(rule, pattern)
                self.assertFalse(rule.endswith(":*)"), f"trailing `:*` is legacy prefix syntax: {rule}")


def decide(perms, command):
    """Model of Claude Code's documented precedence for Bash rules: deny > ask > allow > prompt."""
    import fnmatch
    for kind in ("deny", "ask", "allow"):
        for rule in perms[kind]:
            if rule.startswith("Bash(") and fnmatch.fnmatchcase(command, rule[5:-1]):
                return kind
    return "prompt"


class PrecedenceTest(unittest.TestCase):
    def test_specific_rules_beat_broad_allows(self):
        perms = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]
        table = {
            "gh pr create --draft --fill": "allow", "gh pr view 4": "allow", "gh pr merge 4 --squash": "allow",  # rules allow; scripts/merge_guard.py asks unless qualified
            "gh pr merge 4 --admin": "allow",
            "git push -u origin claude/W-0006-x": "allow", "git push --force origin x": "deny",
            "git push origin main": "deny", "git branch feature": "allow", "git branch -D feature": "ask",
            "npm install": "allow", "npm publish": "ask", "npm token list": "deny",
            "cargo test": "allow", "cargo publish": "ask", "python -m unittest": "allow",
            "python -m twine upload dist/x": "ask", "gh auth status": "allow", "gh auth token": "deny",
            "gh repo view": "prompt", "gh repo view x": "allow", "gh repo delete x --yes": "deny",
            "gh api repos/x/y": "ask", "git credential fill": "deny", "rm -rf /": "deny",
        }
        for cmd, want in table.items():
            self.assertEqual(decide(perms, cmd), want, cmd)


class ProjectPolicyTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp())
        (self.root / "control").mkdir()
        (self.root / "control" / "projects.json").write_text(json.dumps({"schema_version": 1, "projects": {
            "app": {"repository": "o/app", "role": "r", "production": True,
                    "protected_branches": ["stable", "release/*"]}}}), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_registered_branches_are_protected(self):
        prot, warn = guard_bash.load_protected("app", self.root)
        self.assertIsNone(warn)
        for cmd in ["git push origin stable", "git push origin HEAD:release/2.0", "git push origin main"]:
            self.assertIn("protected", guard_bash.check_command(cmd, None, on("feat"), prot), cmd)
        self.assertIn("protected", guard_bash.check_command("git push", None, on("stable"), prot))
        self.assertIsNone(guard_bash.check_command("git push origin releases-notes", None, on("feat"), prot))

    def test_unknown_project_keeps_baseline_and_warns(self):
        prot, warn = guard_bash.load_protected("nope", self.root)
        self.assertEqual(prot, guard_bash.PROTECTED)
        self.assertIn("not registered", warn)
        prot, warn = guard_bash.load_protected("app", self.root / "missing")
        self.assertEqual(prot, guard_bash.PROTECTED)
        self.assertIn("cannot read", warn)


if __name__ == "__main__":
    unittest.main()
