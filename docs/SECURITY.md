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

   Then remove the `git push origin main` deny rules from `.claude/settings.json` via a reviewed PR.

## Already in the repository
- `.claude/settings.json`: denies force-push, pushing to `main`, and reading `.env*`, `*.pem`, `*.key`; allows only read-only git and the control-plane scripts without prompting.
- `.gitignore`: secrets, caches, local databases.
- No GitHub Actions workflows in this repository.
