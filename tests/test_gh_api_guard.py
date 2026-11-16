import shlex
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import gh_api_guard  # noqa: E402
import guard_bash  # noqa: E402


def classify(cmd):
    words = shlex.split(cmd)
    assert words[:2] == ["gh", "api"], cmd
    return gh_api_guard.classify(words[2:])


# Read-only calls that actually caused owner prompts in W-0005..W-0013 sessions.
OBSERVED_READS = [
    "gh api repos/etharic/ai-development-lab/pulls/4/comments --jq '.[] | .body'",
    "gh api repos/etharic/ai-development-lab/pulls/4/reviews --jq '.[-1] | \"commit=\\(.commit_id)\"'",
    "gh api repos/etharic/ai-development-lab/issues/4/events --jq '.[] | .event'",
    "gh api repos/etharic/ai-development-lab/branches --jq '.[].name'",
    "gh api repos/etharic/ai-development-lab/branches/main/protection",
    "gh api repos/etharic/ai-development-lab/compare/main...claude/W-0010-post-review-state --jq '.ahead_by'",
    "gh api repos/etharic/ai-development-lab/commits/7f8a74f --jq '[.files[].filename]'",
    "gh api user --jq '\"\\(.id)+\\(.login)@users.noreply.github.com\"'",
    "gh api repos/etharic/ai-development-control-plane/rulesets",
    "gh api repos/etharic/ai-development-control-plane/rules/branches/main",
    "gh api repos/etharic/ai-development-control-plane/vulnerability-alerts -i",
    "gh api repos/etharic/ai-development-control-plane/actions/permissions",
    "gh api repos/etharic/ai-development-control-plane/tags --jq length",
]


class ReadOnlyTest(unittest.TestCase):
    def test_observed_reads_are_read_only(self):
        for cmd in OBSERVED_READS:
            self.assertIsNone(classify(cmd), cmd)

    def test_other_reads(self):
        for cmd in ["gh api /repos/o/r --paginate --slurp",
                    "gh api -X GET search/issues -f q=repo:o/r",
                    "gh api --method=GET repos/o/r/pulls -F per_page=100",
                    "gh api -XGET repos/o/r",
                    "gh api -H 'Accept: application/vnd.github.raw' repos/o/r/contents/README.md",
                    "gh api graphql -f query='query { viewer { login } }'",
                    "gh api graphql -f query='{ repository(owner:\"o\", name:\"r\") { pullRequest(number:1) { reviewThreads(first:10) { nodes { isResolved } } } } }'",
                    "gh api graphql -f query='query($n:Int!){ viewer { login } }' -F n=1"]:
            self.assertIsNone(classify(cmd), cmd)

    def test_mutations_and_ambiguous_calls_ask(self):
        for cmd in ["gh api -X POST repos/o/r/rulesets --input r.json",
                    "gh api -X PATCH repos/o/r -F has_wiki=false",
                    "gh api -X PUT repos/o/r/vulnerability-alerts",
                    "gh api -X DELETE repos/o/r/git/refs/heads/x",
                    "gh api --method=delete repos/o/r/git/refs/heads/x",
                    "gh api -XPOST repos/o/r/issues",
                    "gh api repos/o/r/issues -f title=x",              # fields imply POST
                    "gh api repos/o/r/pulls/1/merge -F merge_method=merge",
                    "gh api repos/o/r/issues --input body.json",        # body implies POST
                    "gh api -X GET repos/o/r --input body.json",
                    "gh api -X GET -X POST repos/o/r",
                    "gh api -H 'X-HTTP-Method-Override: DELETE' repos/o/r",
                    "gh api --hostname ghe.example.com repos/o/r",
                    "gh api graphql -f query='mutation { addStar(input:{starrableId:\"x\"}) { clientMutationId } }'",
                    "gh api graphql -f query='query { a } mutation { b }'",
                    "gh api graphql -F query=@q.graphql",
                    "gh api graphql --input q.json",
                    "gh api -X GET graphql -f query='{ viewer { login } }'",
                    "gh api repos/o/r/a repos/o/r/b",
                    "gh api",
                    "gh api --unknown-flag repos/o/r"]:
            self.assertIsNotNone(classify(cmd), cmd)


class HookIntegrationTest(unittest.TestCase):
    def check(self, cmd):
        return guard_bash.check_gh(cmd, None, {}, fetch=lambda *a: None)

    def test_reads_pass_and_writes_ask_inside_compound_commands(self):
        self.assertIsNone(self.check("git fetch && gh api repos/o/r/rulesets --jq '.[].id'"))
        decision = self.check("gh api repos/o/r/pulls --jq length && gh api -X PATCH repos/o/r -F has_wiki=false")
        self.assertEqual(decision[0], "ask")
        self.assertIn("not provably read-only", decision[1])
        self.assertEqual(self.check("FOO=1 gh api repos/o/r/issues -f title=x")[0], "ask")

    def test_heredoc_text_is_not_an_api_call(self):
        self.assertIsNone(self.check("git commit -F - <<'EOF'\ngh api -X DELETE repos/o/r\nEOF"))


if __name__ == "__main__":
    unittest.main()
