# Project bootstrap

Claude runs this once when a repository becomes a managed project.

1. **Register** the project in `control/projects.json`: repository, role, `production`, `protected_branches` (required; the local push guard enforces exactly these plus `main`/`master`/`production`). Owner item if the ChatGPT connector or branch protection needs changing (`docs/SECURITY.md`).
   Then wire Claude sessions in the project's local checkout to the control plane: `python scripts/cp.py install-project <path-to-checkout> --project <name>`. This writes a machine-local `.claude/settings.local.json` (status hook, push guard, telemetry, status line, permission policy) and keeps it out of git. Start a Claude session there and confirm the status banner says `managed project: <name>`.
2. **Boundaries:** list the Red actions specific to this project (deploy targets, data stores, paid services) in `.ai/STATE.md`.
3. **Scarce resources:** add any project-specific finite, paid, rate-limited, reset-based or human-dependent resource to `control/resources.json` with `"scope": "project:<name>"`. Leave values `null` until measured.
4. **Verification map:** for each major requirement, name the cheapest representative environment. Mark requirements the ChatGPT sandbox cannot verify (dependencies, network, Windows, devices); these shape delegation.
5. **State:** create `.ai/STATE.md` from `templates/PROJECT_STATE.md`. Add a line to the project's `CLAUDE.md`: "Work items for this repo live in etharic/ai-development-lab `control/queue/` (project: <name>)."
6. **Plan:** write the first work items (`project: <name>`) and assign them using the delegation rule.
