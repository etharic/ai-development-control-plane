# State: ai-development-lab

## Goal
Run the Claude-led, GPT-5.6-assisted development control plane, then manage real projects with it.

## Current state
- Control plane v2 is on `main` (PR #4 merged 2026-11-16, f37d1fd, owner-authorized). It includes high-autonomy Claude permissions, the git-push guard, managed-project install and the advisory merge guard.
- `gh` CLI installed and authenticated (`etharic`). Claude opens and updates PRs itself.
- Qualified agent merges (W-0007): policy is implemented but advisory. GPT-5.6's W-0008 review showed a local hook can't enforce it, so every merge stays owner-confirmed. Real enforcement is W-0009 (owner: separate non-admin Claude identity, then a GitHub-side check).
- Preserved, untouched: `experiment/5.6-sol-capability-audit` (draft PR #3, the v1 experiment control plane and lab service), `experiment/3-sustained-engineering`, `experiment/first-development-cycle`. v2 deliberately shares no files with v1.
- No managed projects yet besides this repository.
- Public release (W-0011, owner-confirmed 2026-11-16): the audited, rewritten history is on branch `public/main` and goes to a new public repository after GPT-5.6's audit (W-0012). This repository stays private. Telemetry and allowance readings are machine-local (`.local/`).

## Durable decisions (2026-11-16, owner)
1. Operational state may eventually be committed directly; doctrine and scripts need reviewed PRs.
2. No unattended or scheduled Claude execution for now.
3. Exact token telemetry from transcripts. Allowance percentage comes only from real measurement and is never inferred from tokens.
4. Agents prepare security recommendations; only the owner changes GitHub/account settings.
5. Old experiment branches are preserved.
6. Allocation thresholds are experimental defaults.

## Next action
Owner: W-0001 (GitHub security settings, including protecting `main`), W-0003 (ChatGPT Project from the current `AGENTS.md`), W-0009 decision (enforced agent merges, or keep merges owner-confirmed). Claude: bootstrap the first real project with `templates/PROJECT_BOOTSTRAP.md`.
