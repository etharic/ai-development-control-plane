# State: ai-development-control-plane

## Goal
Run the Claude-led, GPT-5.6-assisted development control plane, then manage real projects with it.

## Current state
- **Canonical:** this public repository, `etharic/ai-development-control-plane`. `etharic/ai-development-lab` is the private historical archive (old experiments, pre-publication history); don't use it for new work.
- `main` is protected by GitHub rulesets with no bypass: PR required, no force push, no deletion, protected tags. Secret scanning and push protection, Dependabot alerts and private vulnerability reporting are on; Actions is off (`docs/SECURITY.md` "Applied posture").
- Commit identity: noreply only. The owner enabled GitHub email privacy on 2026-11-16, and merges since then are verified noreply. The personal email remains reachable only through closed PR #2's ref (purge needs GitHub Support).
- Claude permissions: routine development without prompts, including provably read-only `gh api` (W-0013). Merges, releases, deploys, credentials and settings stay owner-gated.
- Qualified agent merges (W-0007) remain advisory. The enforced design is native GitHub review with a separate non-admin machine account for Claude (`docs/AGENT_IDENTITY.md`, W-0009), awaiting owner setup.
- Telemetry and allowance readings are machine-local (`.local/`, gitignored).
- No managed projects yet besides this repository.

## Durable decisions (2026-11-16, owner)
1. Operational state may eventually be committed directly; doctrine and scripts need reviewed PRs.
2. No unattended or scheduled Claude execution, **amended 2026-11-16**: the owner may run the local dispatcher in manual mode (`docs/DISPATCHER.md`); it starts Claude in `dontAsk` permission mode only, never answers prompts, and is not installed as a scheduled task.
3. Exact token telemetry from transcripts. Allowance percentage comes only from real measurement and is never inferred from tokens.
4. Agents prepare security recommendations; only the owner changes GitHub/account settings.
5. Old experiment branches are preserved.
6. Allocation thresholds are experimental defaults.

## Next action
- Owner (one-time, account-level):
  - W-0009 steps in `docs/AGENT_IDENTITY.md` (machine account for Claude);
  - W-0001 remainder (scope the ChatGPT GitHub app to selected repositories; Actions spending limit $0);
  - W-0003 (ChatGPT Project from the current `AGENTS.md`).
- Claude: after W-0009, apply the review ruleset and verify it. Then bootstrap the first real project with `templates/PROJECT_BOOTSTRAP.md` once the owner names it.
