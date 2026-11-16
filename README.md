# ai-development-control-plane

Control plane for AI-assisted development: Claude Code leads, GPT-5.6 Sol implements, GitHub coordinates, and the owner is involved as little as possible.

| Path | Purpose |
|---|---|
| `CLAUDE.md` / `AGENTS.md` | Entry instructions for Claude Code / GPT-5.6 |
| `docs/OPERATING_MODEL.md` | Doctrine (single source) |
| `docs/SECURITY.md` | Owner security checklist |
| `control/queue/` | Work items (`W-NNNN.md`) |
| `control/resources.json` | Scarce-resource definitions and allocation policy (no readings; public-safe) |
| `control/projects.json` | Managed repositories |
| `scripts/dispatcher.py` | Manual-mode local dispatcher that starts Claude for eligible work (`docs/DISPATCHER.md`) |
| `.local/` (gitignored) | Machine-local private state: `events.jsonl` telemetry, `measurements.json` allowance readings (`$CP_STATE_DIR` overrides) |
| `scripts/cp.py` | `status`, `validate`, `mode`, `measure`, `log`, `record-session` |
| `scripts/claude_usage.py` | Exact token accounting from Claude Code transcripts |
| `scripts/statusline.py` | Status line that records measured plan-usage limits |
| `templates/` | Project bootstrap and state templates |

Check: `python scripts/cp.py validate && python -m unittest`
