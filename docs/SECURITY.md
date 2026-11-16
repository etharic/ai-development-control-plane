# Security and access recommendations

Agents never change GitHub or account settings (owner decision 2026-11-16). This page is the owner's checklist; tracked as queue item W-0001.

## Threats this addresses
1. **Write access too broad.** The ChatGPT GitHub connector can write to every repository it can see, including production ones. "Don't touch production" is only text today.
2. **Unprotected default branches.** Without protection, any agent with write access can push to `main` directly, so "Red: merge to protected branch" is unenforced.
3. **Prompt injection through repository content.** Agents read issues, PR comments, code and work items. Content written by someone else, or by a compromised integration, could try to direct actions. Doctrine says repo content never authorizes Red actions; the settings below limit the damage if that rule is ever broken.
4. **Secrets in commits.** Mitigated by `.gitignore` and by Claude's deny rules for `.env` and key files. Not a substitute for secret scanning.

## Owner checklist
0. **Email privacy (do first):** enable "Keep my email addresses private" and "Block command line pushes that expose my email" (see "Commit identity" below).
1. **Limit the ChatGPT GitHub app to specific repositories.** GitHub → Settings → Applications → Installed GitHub Apps → ChatGPT (connector) → Repository access → *Only select repositories*: `ai-development-lab` plus any project repo deliberately delegated. Leave production repos out unless a project needs GPT-5.6 there.
2. **Protect `main` in each repository** (Settings → Branches or Rules → Rulesets):
   - require a pull request before merging;
   - block force pushes;
   - block deletion.
   Approvals aren't needed for a solo owner. Requiring a PR is the important part.
3. **Enable secret scanning and push protection** where available (Settings → Code security).
4. **Keep Actions conservative:** Settings → Actions → General → require approval for workflows from outside collaborators. Set a spending limit of $0 so overage is impossible.
5. **When enabling direct operational commits** (owner decision 1, "eventually"):
   - **Option A (simplest):** let only Claude's local git push to `main`. Add a ruleset that requires PRs for everything except paths `control/queue/**`, `STATE.md`. GitHub rulesets support file-path restrictions on some plans; if not available on yours, use option B.
   - **Option B:** keep `main` PR-only and let operational state ride along in Claude's next PR.

   Then, via a reviewed PR, remove the `main` push deny rules from `.claude/settings.json`, drop `main` from this repository's `protected_branches` and from the baseline `PROTECTED` set in `scripts/guard_bash.py`.

6. **Optional, strengthens agent merges:** give GPT-5.6 its own GitHub identity (a machine user or GitHub App restricted to delegated repositories) so its reviews are authenticated rather than attested. Then the merge guard can also require the reviewer's GitHub login.

## Commit identity (public repository)
Every commit in this repository is public forever: GitHub keeps `refs/pull/N/head` for every PR, even after branches are deleted. Commits must use the noreply identity `etharic <331996485+etharic@users.noreply.github.com>`.
- **Claude:** the canonical checkout sets the identity in its local git config.
- **GPT-5.6:** `AGENTS.md` requires an explicit author and committer on every commit made through the connector.
- **GitHub web merges** use the account's email setting. The owner must enable, in GitHub → Settings → Emails, **"Keep my email addresses private"** and **"Block command line pushes that expose my email"**. These are account-level settings only the owner can change. Until then, merges on GitHub can record the personal email; don't merge on this repository before they're on.
- Incident 2026-11-16: GPT-5.6's commit `92be7ed` (public PR #2) was authored with the personal email because the connector used the account default. `refs/pull/2/head` keeps it reachable. Only GitHub Support can purge a PR ref; the owner may request it.

## Permission-prompt audit (W-0013)
Rule: a prompt is justified only when a reasonable owner might answer No.
- **`gh api`:** read-only calls (GET/HEAD, read-only GraphQL queries) are routine inspection and should not prompt. `scripts/gh_api_guard.py` classifies them and fails closed: fields or `--input` without an explicit GET, method overrides, other hosts, GraphQL mutations, and unknown flags all ask. Tests: `tests/test_gh_api_guard.py`, including the exact read-only calls that prompted the owner during W-0005 to W-0001. **Pending owner authorization:** moving `Bash(gh api *)` from ask to allow, so the classifier decides. Claude Code's safety classifier refused to make that relaxation on the strength of a work item alone. Until then, the ask rule still prompts for every `gh api`, and the hook adds a reason to non-read calls.
- **`gh alias`, `gh extension`:** added to ask. They create indirect commands the hook can't see.
- **Every other ask rule was reviewed and kept:** merges, `gh repo create`, CI reruns and dispatch, releases and publishing, deploy and infrastructure CLIs, branch deletion, hard resets and cleans, discarding changes, tags, history rewriting, stash drops, destructive DB resets, `find -delete/-exec`, copying or moving to absolute or home paths, and edits to `.claude/` or `.github/`. Each is consequential or destructive enough that a reasonable owner might decline.

## Already in the repository
- `.claude/settings.json` (Claude Code permissions; rules are evaluated deny → ask → allow, so a specific `ask`/`deny` such as `gh api *` or `gh repo delete *` wins over a broad `allow`; `tests/test_guard.py` `PrecedenceTest` pins the expected decision for representative commands):
  - **allow** without prompting: routine git (status/diff/log, branch, switch, add, commit, stash, merge/rebase, push), `gh pr`/`gh issue` and read-only `gh repo`/`run`/`workflow`/`release`, Python/Node/Rust toolchains and dependency installs, ordinary filesystem commands, edits inside the repository, `WebFetch`/`WebSearch`. GitHub access goes through the authenticated `gh` CLI, never raw tokens.
  - **ask** (owner approves each time, i.e. Red/Yellow): `gh api`, releases and package publishing, deploy/infra CLIs, CI reruns/dispatch, branch deletion, `git reset --hard`/`clean`, tags, history rewriting, destructive DB resets, copying or moving to absolute/home paths, editing `.claude/` or `.github/`.
  - **deny**: force pushes and pushes to `main`/`master`, remote ref deletion, `git credential*`, global/system git config, `gh auth token`/login/logout/refresh/setup-git, `gh secret`/`ssh-key`/`gpg-key`/`ruleset`, repository delete/archive/rename/edit, registry login/token/owner commands, `printenv`, `rm -rf` of root/home/parent, reading `.env*`, keys, certificates and the usual credential stores (`~/.ssh`, `~/.aws`, gh config, `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, cargo credentials, password stores), writing into `.git/`.
- `scripts/guard_bash.py` (PreToolUse hook on Bash): parses every `git ... push` (including `git -C dir push`, chained commands and refspecs) and blocks force, `+refspec`, deletes, `--all`/`--mirror`, and any push whose target is protected, explicitly or implicitly via the current branch or its `@{push}` upstream. Protected = baseline `main`, `master`, `production` plus the project's `protected_branches` in `control/projects.json` (globs allowed, e.g. `release/*`), selected with `--project`. `cp.py validate` requires every registered project to declare a non-empty list, so the registry is the single source of truth for branch policy. Permission globs match prefixes only; the hook covers what they can't (for example a bare `git push` while on `main`).
- `scripts/merge_guard.py` (via the same hook) is **advisory**. `gh pr merge` is an ask rule, so every merge needs the owner. The guard evaluates the qualified-agent-merge conditions and puts its verdict in the prompt; `--admin` is denied. It is not an authorization boundary: allowed interpreters can run `gh` as a child process the hook never sees (W-0008 finding). Autonomous merges need W-0009: a separate, non-admin GitHub identity for Claude plus a GitHub-side required check, so GitHub enforces the policy whatever process calls it. Claude may not post a merge marker block signed as any agent other than `claude-code`.
  - Known limit: GPT-5.6 posts through the owner's GitHub account (`etharic`), so the reviewer's identity is attested in the marker block, not authenticated by GitHub. A separate GitHub identity or app for GPT-5.6 would make it verifiable (checklist item 6).
- Managed projects: `python scripts/cp.py install-project <checkout> --project <name>` (bootstrap step 1) writes the project's machine-local `.claude/settings.local.json` with this repository's permission policy and hooks that call this control plane by absolute path (status, push guard with that project's branches, session telemetry, status line). It is excluded through `.git/info/exclude`, so no tracked project file changes. It refuses to overwrite a different existing file without `--force`. Rerun it after changing the policy here. A user-level `~/.claude/settings.json` was rejected because it would apply high-autonomy rules to every repository on the machine, production ones included.
- Limits, stated plainly: allowing `python`, `node` and package managers means Claude can run arbitrary project code, which could read files the `Read` deny rules protect. The deny rules, the hook and Claude's own doctrine reduce accidental or injected misuse. They are not a sandbox. GitHub branch protection (checklist item 2) is the enforced boundary.
- `.gitignore`: secrets, caches, local databases.
- No GitHub Actions workflows in this repository.
