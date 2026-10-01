# Adversary: Dueling Claude: Automating Claude Code

I run one Claude Code session (the **worker**) on a task and a second Claude Code session (the **adversary**) as its supervisor, side by side in tmux. The adversary:

- answers the worker's "shall I proceed?" questions,
- approves or denies the worker's permission prompts,
- reviews the work against the original task when the worker says it is done. It reads the code and runs the tests itself, and sends back anything missing or broken until the task is actually finished.

Both sides are the regular `claude` CLI, so usage comes out of the logged-in Claude plan, not an API key.

```
┌─ WORKER · does the task ───┬─ ADVERSARY · reviews ──────┐
│ real `claude` session      │ real `claude` session      │
│ (watch it, or type into it)│ (read-only + MCP tools)    │
├────────────────────────────┴────────────────────────────┤
│ BRIDGE · exchange 3/30 · approvals 7 · worker idle      │
└─────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.11+
- tmux
- the `claude` CLI, logged in
- git (optional, see [Without git](#without-git))

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The package has no runtime dependencies beyond the standard library.

## Usage

```sh
adversary run "Implement X so all tests pass and document it"
```

That is all that's required. It works in the current directory. Every flag below is optional.

| Flag | Effect |
|---|---|
| `--repo PATH` | Work in `PATH` instead of the current directory. |
| *(default)* | Every worker permission prompt goes to the adversary, which must call `approve_tool` or `deny_tool`. The worker sees the deny reason. |
| `--skip-permissions` | The worker runs with `--dangerously-skip-permissions`, so there is no approval traffic. |
| `--worker-permission-mode acceptEdits` | Edits are auto-approved and only the remaining prompts (for example Bash) go to the adversary. This is cheaper, because each prompt costs a full adversary turn. |
| `--strict-approvals` | Ignore user-level settings for the worker, so no personal allow rule lets a tool call skip the adversary. |
| `--worktree` | Work in a fresh `git worktree` on branch `adversary/<timestamp>` (git repos only). |
| `--max-exchanges N` | Cap on worker/adversary round trips (default 30). Permission requests don't count. |
| `--stall-after SECONDS` | Log a stall when both agents sit idle this long while the run is unfinished (default 120, `0` turns it off). |
| `--worker-model`, `--adversary-model` | Pick the model for each side. |
| `--verify-cmd "just test"` | Extra command prefix the adversary may run (repeatable). |
| `--task-file FILE` | Read the task from a file. |
| `--no-attach` | Start the tmux session detached. |

### Resuming

```sh
adversary resume
```

This picks up the last run in the current directory (or the folder it was started from, for `--worktree` runs). It works after a crash, a closed terminal, a stall, or a run that already finished.

- **Both sessions come back:** each Claude Code session is reopened with `claude --resume`, with its full history.
- **The adversary decides what's next:** it is told the run was resumed and given the worker's last message. It checks the current state, then sends the worker what to do next, or calls `finish`.
- **New instructions are optional:** `adversary resume "Also add a --verbose flag"` passes them to the adversary as an amendment to the task.
- **Counters carry over:** exchange, approval, denial and stall counts, and permission request numbers.

| Flag | Effect |
|---|---|
| `--repo PATH` | Resume the last run in `PATH` instead of the current directory. |
| `--run ID` | Resume a specific run, by timestamp (`20261001-143802`) or run directory. |
| `--kill` | If the run is still open in its tmux session, close it first. Without this, I'm asked to confirm. |
| `--no-attach` | Start the tmux session detached. |

Runs record their session IDs as they start. For older runs, the sessions are found in Claude Code's session logs for the folder.

Keys in the bridge pane:

| Key | Action |
|---|---|
| `p` | Pause or resume relaying, to take over by hand |
| `s` | Print status |
| `q` | Stop the bridge |

While relaying is paused I can type into the worker pane. Anything typed there is passed to the adversary as an amendment to the task, and it takes precedence over the original.

I can also type into the adversary pane at any time, for example to add features or change the task. Its reply to me isn't relayed, but it passes the work on with its `message_worker` tool, which reaches the worker straight away. If the worker is busy, the message is typed into its pane and Claude Code folds it into the running turn. Delivery waits only while a permission request is pending.

Each run writes to `~/.local/state/adversary/runs/<timestamp>/`:

- `transcript.md`: every relayed message, permission decision and human input
- `report.md`: outcome, the adversary's summary, and the files changed since the run started
- `stalls.jsonl`: one record per stall, written when both agents have been idle for `--stall-after` seconds with the run unfinished. Nothing moves the run forward at that point, so it is almost always a relay failure. Each record holds both sides' last turn, queued messages, pending permissions and last message. It is also shown in the bridge pane and the transcript, and appended to `~/.local/state/adversary/stalls.jsonl` across all runs.
- `snapshot/`: the folder's original contents (folders without git only)
- `state.json`: counters carried over by `adversary resume`
- the generated settings, MCP config and launch scripts

## Without git

The working folder (the current directory, or `--repo`) can be any folder. If it isn't a git repo with at least one commit, the folder is copied to `snapshot/files/` in the run directory before the worker starts, along with a SHA-256 manifest of every file.

- **Skipped:** dependency and build directories (`node_modules`, `.venv`, `dist`, `build`, `target`, caches and so on). Files over 5 MB are hashed but not copied, and copying stops at 500 MB in total.
- **Reviewing:** the adversary is told there is no git. Its `changed_files` tool lists the added, modified and deleted files from the manifest. It can also `diff -ru` against the snapshot, or read both copies.
- **Undoing:** the snapshot doubles as a backup. Copying files back from `snapshot/files/` undoes the worker's changes.

In a git repo, `changed_files` reports `git status` and `git diff --stat` against the starting commit instead.

## How it works

- **Hooks report what happened.** Per-run `--settings` files add hooks to both sessions:
  - `SessionStart` signals that the UI is ready.
  - `Stop` sends the session's final message for the turn to the bridge.
  - `UserPromptSubmit` tells the bridge whether it or a person submitted the prompt.
  - `PermissionRequest` (worker only) blocks until the adversary decides.

  The hook command (`adversary/hook.py`) talks to the bridge over a Unix socket.
- **The bridge speaks by typing into tmux.** Each message is a typed one-line header, then the body as a bracketed paste, then Enter. The header matters: Claude Code wraps long pastes in `<pasted_content>` and won't follow pasted instructions unless the typed part of the message asks it to.
- **Messages are free-form text.** The bridge never parses what either side says. The only structured signals are the adversary's MCP tool calls: `approve_tool`, `deny_tool`, `message_worker`, `changed_files` and `finish`. They are served by `adversary/mcp_server.py`, a dependency-free stdio server.
- **The adversary cannot change the project.**
  - It runs with `--permission-mode dontAsk`.
  - Its allowlist is Read/Grep/Glob plus read and verify Bash prefixes (git diff/log/status, test runners and similar).
  - Edit, Write and NotebookEdit are disallowed.
  - `--setting-sources project` keeps personal allow rules from widening its tools.
- **Startup dialogs are accepted automatically.** The bridge accepts the folder-trust dialog (and the skip-permissions confirmation, if shown) for the working folder.

## Caveats

- **Pre-approved commands skip the adversary.** In default mode, a command already allowed by user settings (for example a `Bash(curl:*)` rule in `~/.claude/settings*.json`) runs without reaching the adversary. `--strict-approvals` prevents this.
- **Answering on screen:** if a worker permission prompt is answered on screen, the adversary's pending request expires when the worker's turn ends.
- **Typing over a delivery:** if the bridge delivers a message while someone is typing in that pane, the two get mixed together. Press `p` to pause before typing.

## Layout

```
adversary/
  cli.py          adversary run: run directory, snapshot, tmux layout
  bridge.py       socket server, relay logic, permission routing, caps, status
  hook.py         hook command used by both sessions
  mcp_server.py   approve_tool / deny_tool / message_worker / changed_files / finish
  config.py       run config, per-run settings, MCP config and launch scripts
  sessions.py     finding the run and Claude Code sessions to resume
  snapshot.py     folder snapshot and change detection without git
  transcript.py   transcript.md and report.md
  tmux.py         paste, key and capture helpers
  prompts/adversary_role.md   the adversary's appended system prompt
examples/toy-repo-template/   small package with failing tests for trying a run
tests/
```

## Tests

```sh
.venv/bin/pytest
```

To try a full run, copy `examples/toy-repo-template/` somewhere, `cd` into it (with or without `git init`) and run `adversary run "Implement calc.py so all tests pass"`.
