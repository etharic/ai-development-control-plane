# Agent identity and enforced independent review (W-0009 redesign, W-0014)

Status: **design, awaiting owner setup.** Until it is applied, every merge stays owner-confirmed (`gh pr merge` is an ask rule).

## Problem
Today Claude's `gh` and GPT-5.6's ChatGPT connector both act as the owner's account `etharic`, which is an admin. GitHub therefore can't:
- tell the owner and the two agents apart;
- stop an agent from approving its own work;
- stop Claude from changing rulesets with the admin token.

Local hooks only add friction: allowed interpreters can bypass them (W-0008). The earlier marker-block policy (W-0007) is an attestation, not proof of identity.

## Rejected: an Actions `merge-gate` status check
A required check computed by a workflow looks attractive, but on this repository:
- `pull_request` and `pull_request_review` workflows run the **PR's own** workflow files, so a PR can add or alter a job that reports `merge-gate` as passing. Same-repository PRs can request `checks: write`.
- Only `pull_request_target` and `issue_comment` run trusted default-branch definitions, and neither fires on review submission.
- Preventing workflow edits would need code-owner review. Under a single shared account nobody can give that, because GitHub forbids self-approval.
- It needs Actions enabled, which widens the attack surface (`docs/SECURITY.md`).

## Design: native review enforcement with one machine account
GitHub's terms allow one free machine account per person, and one is enough:

| Principal | GitHub identity | Rights |
|---|---|---|
| Owner | `etharic` (personal, admin) | Everything, including rulesets. Only the owner uses this account for administration. |
| GPT-5.6 | `etharic`, via the ChatGPT connector (unchanged) | Opens PRs from `gpt/*` branches and reviews Claude's PRs. |
| Claude Code | **new machine account**, e.g. `etharic-claude` | Collaborator with **Write**, not Admin, so it can't edit rulesets or settings. Claude's `gh` logs in as this account; the owner's token is removed from Claude's environment. |

Ruleset `protect-main` changes:
- `required_approving_review_count: 1`
- `require_last_push_approval: true`
- keep: dismiss stale reviews, conversation resolution, no force push, no deletion, **no bypass actors**.

Resulting guarantees, enforced by GitHub whatever process calls the API:
- **Claude-authored PR:** needs an approval from `etharic`, meaning GPT-5.6's connector review or the owner. Claude can't approve its own PR (GitHub forbids self-approval), and with last-push approval its own later push can't ride on an older approval.
- **GPT-authored PR** (author `etharic`): needs an approval from Claude's machine account after an independent review, or from the owner.
- **Claude can't change rulesets or repository settings:** it has no admin right. The local deny rules become a second layer instead of the only one.
- **Production projects:** don't give Claude's machine account Write; give it Read or Triage, or add an owner-only CODEOWNERS approval rule. Owner merges stay structurally required there.

What changes afterwards:
- `gh pr merge` can move from ask to allow in non-production projects, because GitHub now enforces the independent review that W-0007 wanted.
- `scripts/merge_guard.py` stays as early feedback.
- The marker block becomes optional.
- W-0007 is satisfied without a local trust anchor.

Residual risks:
- GPT-5.6 and the owner share `etharic`. An approval from that account is "owner or GPT-5.6"; both are trusted principals for non-production work.
- The owner's personal account stays the single admin, and its session security matters as much as before.
- Commit identity: the machine account must use its own noreply address (the private-email setting on that account too).

## Owner steps (once; account-level, so only the owner can do them)
1. Create the machine account (`etharic-claude`). Enable email privacy on it, and turn on 2FA.
2. On `etharic/ai-development-control-plane` (and future non-production project repositories), invite it as a collaborator with **Write**; accept the invitation from the machine account.
3. In Claude's environment: `gh auth logout` for `etharic`, then `gh auth login` as the machine account. The owner types this; Claude never handles tokens.
4. Tell Claude it's done. Claude then (a) changes `protect-main` as above, which needs the owner's approval because Claude no longer has admin, so the owner applies it or authorizes it before step 3; (b) demonstrates on a throwaway PR that self-approval and unapproved merges are refused by GitHub; (c) moves `gh pr merge` to allow for non-production projects through a reviewed PR.

Cost: zero. No Actions, no paid features.
