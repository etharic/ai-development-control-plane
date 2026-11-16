# Public-readiness audit (W-0011)

Date: 2026-11-16. Performed by Claude Code; independent audit by GPT-5.6 is W-0012.

## Decision
The owner chose a **fresh public repository** instead of making `etharic/ai-development-lab` public. GitHub keeps read-only `refs/pull/N/head` refs for every PR, and nobody can rewrite or delete them; only GitHub Support can purge them. In-place rewriting would therefore leave the original commits reachable. The private repository stays private and untouched. Publishing only ever adds a new repository.

## Scope of the published history
Only the `main` lineage plus the W-0011 commits: 24 commits at the time of the audit. Excluded and never published: the `experiment/*` branches, PR heads #1–#6, `control-plane-v2` and the post-review state branch.

## Classification
| Finding | Class | Action |
|---|---|---|
| Author/committer email `<personal Gmail>` on 23 commits | PERSONAL_INFORMATION | Rewritten to the GitHub noreply address `331996485+etharic@users.noreply.github.com`. GitHub's own merge-commit committer `noreply@github.com` kept. |
| `telemetry/events.jsonl` (session tokens, owner-prompt counts, allowance readings) | OPERATIONAL_SENSITIVE | Removed from every commit; now machine-local in `.local/` (gitignored). |
| Exact Claude allowance readings and reset times in `control/resources.json` | OPERATIONAL_SENSITIVE | Nulled in every historical version; readings now live in `.local/measurements.json`. `cp.py validate` rejects committed readings. |
| GitHub username `etharic`, repository name | SAFE_PUBLIC (intended) | Kept. |
| Deny-rule paths (`~/.ssh`, `~/AppData/Roaming/GitHub CLI`, ...) and words like token/secret/password in doctrine and tests | SAFE_PUBLIC | These are generic policy text, not values. |
| Credentials: tokens (`gh*_`, `sk-`, AWS keys), private keys, passwords | SECRET | **None found** in any reachable file version or commit message. |
| Machine names, Windows user paths | PERSONAL_INFORMATION | **None found** in history. |
| Business or third-party confidential material | PROJECT_CONFIDENTIAL | **None found**: the repository holds only control-plane code, doctrine and work items. |

## Method
1. `git grep` over every tree in `main`'s history, then, after the rewrite, every reachable blob (`git rev-list --objects --all`) and every commit message. Patterns: personal email fragments, `gh[pousr]_…`, `sk-…`, `AKIA…`, `PRIVATE KEY`, `DESKTOP-`, Windows user paths, password/secret/token (manually reviewed), allowance numbers in prose and messages.
2. Rewrite in an isolated scratch clone with no remote: `git filter-branch` with an email `--env-filter` and a tree filter. The tree filter deletes `telemetry/` and nulls measurement fields in `control/resources.json`. Backup refs and reflogs were dropped and the clone garbage-collected before the post-rewrite scan.
3. Checks on the rewritten history:
   - the tip tree is byte-identical to the private branch tip;
   - `Agent:` and `Work-Item:` trailers are preserved (21 and 16);
   - commit order and dates are kept;
   - `python -m unittest` and `cp.py validate` pass at the tip.

## Residual exposure (accepted)
- Commit timestamps reveal the timezone offset.
- Work items, `STATE.md` and doctrine describe the owner's AI-assisted workflow and plan tier ("Claude Pro allowance"). That is intended content.
- Commit messages and queue items refer to PR numbers (#1–#6) that exist only in the private repository.
- Future telemetry and readings are private only while they stay in `.local/`. The validator and `.gitignore` enforce this for `resources.json` and `.local/`. Any new data file needs the same review.

## Rollback
Nothing destructive is involved. The private repository and its history are unchanged. If publishing goes wrong, the owner deletes or privatizes the new public repository.

## Publishing steps (after W-0012 passes)
1. Owner creates an **empty** public repository (no README or license from GitHub) and gives Claude its name.
2. Claude pushes the audited branch as `main`. That's a plain push to an empty repository, not a force push.
3. Claude verifies the remote matches the audited commit and reruns the scan against a fresh clone.
4. Owner decides which repository is canonical from then on (recommended: the public one, with this private one archived).
