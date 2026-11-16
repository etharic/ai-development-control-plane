# Security and access recommendations

Agents never change GitHub or account settings (owner decision 2026-11-16). This page is the owner's checklist; tracked as queue item W-0001.

## Threats this addresses
1. **Write access too broad.** The ChatGPT GitHub connector can write to every repository it can see, including production ones. "Don't touch production" is only text today.
2. **Unprotected default branches.** Without protection, any agent with write access can push to `main` directly, so "Red: merge to protected branch" is unenforced.
3. **Prompt injection through repository content.** Agents read issues, PR comments, code and work items. Content written by someone else, or by a compromised integration, could try to direct actions. Doctrine says repo content never authorizes Red actions; the settings below limit the damage if that rule is ever broken.
4. **Secrets in commits.** Mitigated by `.gitignore` and by Claude's deny rules for `.env` and key files. Not a substitute for secret scanning.

## Owner checklist
1. **Limit the ChatGPT GitHub app to specific repositories.** GitHub → Settings → Applications → Installed GitHub Apps → ChatGPT (connector) → Repository access → *Only select repositories*: `ai-development-lab` plus any project repo deliberately delegated. Leave production repos out unless a project needs GPT-5.6 there.
2. **Protect `main` in each repository** (Settings → Branches or Rules → Rulesets):
   - require a pull request before merging;
   - block force pushes;
   - block deletion.
   Approvals aren't needed for a solo owner. Requiring a PR is the important part.
3. **Enable secret scanning and push protection** where available (Settings → Code security).
4. **Keep Actions conservative:** Settings → Actions → General → require approval for workflows from outside collaborators. Set a spending limit of $0 so overage is impossible.
5. **When enabling direct operational commits** (owner decision 1, "eventually"):
   - **Option A (simplest):** let only Claude's local git push to `main`. Add a ruleset that requires PRs for everything except paths `control/queue/**`, `control/resources.json`, `telemetry/**`, `STATE.md`. GitHub rulesets support file-path restrictions on some plans; if not available on yours, use option B.
   - **Option B:** keep `main` PR-only and let operational state ride along in Claude's next PR.

   Then remove the `main` push deny rules from `.claude/settings.json` and `main` from `PROTECTED` in `scripts/guard_bash.py` via a reviewed PR.

## Already in the repository
- `.claude/settings.json` (Claude Code permissions; rules are evaluated deny → ask → allow):
  - **allow** without prompting: routine git (status/diff/log, branch, switch, add, commit, stash, merge/rebase, push), `gh pr`/`gh issue` and read-only `gh repo`/`run`/`workflow`/`release`, Python/Node/Rust toolchains and dependency installs, ordinary filesystem commands, edits inside the repository, `WebFetch`/`WebSearch`. GitHub access goes through the authenticated `gh` CLI, never raw tokens.
  - **ask** (owner approves each time, i.e. Red/Yellow): `gh pr merge`, `gh api`, releases and package publishing, deploy/infra CLIs, CI reruns/dispatch, branch deletion, `git reset --hard`/`clean`, tags, history rewriting, destructive DB resets, copying or moving to absolute/home paths, editing `.claude/` or `.github/`.
  - **deny**: force pushes and pushes to `main`/`master`, remote ref deletion, `git credential*`, global/system git config, `gh auth token`/login/logout/refresh/setup-git, `gh secret`/`ssh-key`/`gpg-key`/`ruleset`, repository delete/archive/rename/edit, registry login/token/owner commands, `printenv`, `rm -rf` of root/home/parent, reading `.env*`, keys, certificates and the usual credential stores (`~/.ssh`, `~/.aws`, gh config, `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, cargo credentials, password stores), writing into `.git/`.
- `scripts/guard_bash.py` (PreToolUse hook on Bash): parses every `git ... push` (including `git -C dir push`, chained commands and refspecs) and blocks force, `+refspec`, deletes, `--all`/`--mirror`, and any push whose target is `main`, `master` or `production`, explicitly or implicitly via the current branch or its `@{push}` upstream. Permission globs match prefixes only; the hook covers what they can't (for example a bare `git push` while on `main`).
- Limits, stated plainly: allowing `python`, `node` and package managers means Claude can run arbitrary project code, which could read files the `Read` deny rules protect. The deny rules, the hook and Claude's own doctrine reduce accidental or injected misuse. They are not a sandbox. GitHub branch protection (checklist item 2) is the enforced boundary.
- `.gitignore`: secrets, caches, local databases.
- No GitHub Actions workflows in this repository.
