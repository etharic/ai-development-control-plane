# Local Claude dispatcher (manual mode)

`scripts/dispatcher.py` stops you having to say "go" to Claude. It watches the canonical control plane and starts Claude Code only when there is Claude work to do. You start it; it is **not** installed as a scheduled task (owner decision 2026-11-16, W-0015). Scheduling would be a separate decision.

## Start, stop, status
```
cd <your checkout of ai-development-control-plane>
git switch main && git pull --ff-only
python scripts/dispatcher.py run            # foreground; polls every ~5 min; Ctrl+C to stop
python scripts/dispatcher.py once --dry-run # what would it start now? never launches
python scripts/dispatcher.py once           # poll once, launch if there is new work
python scripts/dispatcher.py status         # handled work, outcomes, why it isn't launching
python scripts/dispatcher.py reset KEY      # acknowledge a blocked run; run again to re-arm the work
```
`python scripts/cp.py status` (the session-start banner) also shows any dispatcher run that stopped on a permission.

## What wakes Claude
- A `ready` item with `assignee: claude` whose dependencies are `done` and which no branch claims.
- An open PR from a `gpt/*` branch at a head Claude hasn't been started for.
- In CONSERVE allocation mode: only priority-1 items and PR reviews.

Nothing else does. Idle polling is `git fetch` plus `gh pr list`, so it uses no Claude tokens.

## Safety
- Claude runs as `claude -p … --permission-mode dontAsk`. Anything the repository's permission rules don't already allow is **denied, not asked**. The dispatcher never answers a prompt, supplies credentials or approves a merge. Merges, releases, settings and other owner-gated actions still need you, in a normal session.
- When a run hits a denied permission, it is recorded as `owner_attention_blocked` (local telemetry), shown once in `cp.py status`, and **not** relaunched.
- Each piece of work has a key (item id plus a content hash, or PR number plus head SHA) and a saved launch state: `reserved` → `started` → `done` / `blocked` / `error` / `ambiguous`, or `spawn_failed`.
  - Only `spawn_failed`, meaning Claude provably never started (CLI missing, OS refused), is retried automatically, with growing backoff.
  - Anything that may have run, such as a timeout, lost output, or a crash or reboot mid-run, becomes `ambiguous` and is **never** relaunched automatically. `blocked`, `error` and `ambiguous` are shown in `cp.py status` until you `reset` them.
- One coherent snapshot: queue items come from `origin/main` and claims from `origin/*` branches, both from the same `git fetch --prune`. Local branches and the local working tree are ignored, so a claim counts only once its branch is pushed.
- Single-flight lock in `.local/dispatcher/lock`; a dead or over-age lock is recovered.
- It won't launch when the checkout isn't on a clean `main`, or when `cp.py validate` fails. It logs the reason once, not every poll.
- Network or GitHub errors back off exponentially, with jitter, up to an hour.
- Repository content (issues, PRs, items) only identifies work. It never grants authority.

## Files
All machine-local and gitignored, under `.local/dispatcher/`: `state.json` (handled keys), `log.jsonl` (wake reason, work keys, start and end, outcome, turns, output tokens, denials), `lock`.

## Troubleshooting
- **"not launching: checkout is on …":** run `git switch main && git pull --ff-only`, and commit or stash local changes.
- **Busy forever:** if no Claude run is in progress, delete `.local/dispatcher/lock`. The dispatcher also recovers it automatically once its process is gone.
- **A run was blocked:** look at `status` and the item's Result, do the owner step in a normal session, then `reset KEY` twice to re-arm.
- **`ambiguous`:** Claude may have done part of the work. Check the item, branches and PRs; when it is safe to start over, `reset KEY` twice.
- **`spawn_failed`:** usually `claude` isn't on `PATH`. Fix that; the dispatcher retries by itself.
- **`claude` not found:** the Claude Code CLI must be on `PATH` in the terminal that runs the dispatcher.
