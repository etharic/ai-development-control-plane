# ai-development-lab

Control plane for AI-assisted development: Claude Code leads, GPT-5.6 Sol implements, GitHub coordinates, and the owner is involved as little as possible.

| Path | Purpose |
|---|---|
| `CLAUDE.md` / `AGENTS.md` | Entry instructions for Claude Code / GPT-5.6 |
| `docs/OPERATING_MODEL.md` | Doctrine (single source) |
| `docs/SECURITY.md` | Owner security checklist |
| `control/queue/` | Work items (`W-NNNN.md`) |
| `control/resources.json` | Measured scarce-resource state and allocation policy |
| `control/projects.json` | Managed repositories |
| `telemetry/events.jsonl` | Append-only telemetry |
| `scripts/cp.py` | `status`, `validate`, `mode`, `measure`, `log`, `record-session` |
| `scripts/claude_usage.py` | Exact token accounting from Claude Code transcripts |
| `scripts/statusline.py` | Status line that records measured plan-usage limits |
| `templates/` | Project bootstrap and state templates |

Check: `python scripts/cp.py validate && python -m unittest`
