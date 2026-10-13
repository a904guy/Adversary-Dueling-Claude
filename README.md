# Adversary: Dueling Claude: Automating Claude Code

I run one Claude Code session (the **worker**) on a task and a second Claude Code session (the **adversary**) as its supervisor, side by side in tmux. The adversary:

- is given the task first, studies the repo, and hands the worker a plan — the worker is never given the task directly, so the two don't start from competing plans,
- answers the worker's "shall I proceed?" questions,
- approves or denies the worker's permission prompts,
- reviews the work against the original task when the worker says it is done. It reads the code and runs the tests itself, and sends back anything missing or broken until the task is actually finished.

Both sides are the regular `claude` CLI, so usage comes out of the logged-in Claude plan, not an API key.

```
┌─ WORKER · does the task ───┬─ ADVERSARY · reviews ──────┐
│ real `claude` session      │ real `claude` session      │
│ (watch it, copy, Esc to    │ (read-only + MCP tools)    │
│  stop it)                  │ ◀ type here: it has focus  │
├────────────────────────────┴────────────────────────────┤
│ BRIDGE · [running] exchange 3/30 · approvals 7 · …      │
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
| `--max-exchanges N` | Loop guard: after this many round trips between the agents without me (default 30), the worker is put on hold. The count restarts whenever I type to either side and on resume, and the run is never ended by it. Permission requests don't count. |
| `--stall-after SECONDS` | Log a stall when both agents sit idle this long while the run is unfinished (default 120, `0` turns it off). |
| `--worker-model`, `--adversary-model` | Pick the model for each side. |
| `--worker-effort LEVEL`, `--adversary-effort LEVEL` | Effort level for each side (`low`, `medium`, `high`, `xhigh`, `max`; passed as `claude --effort`). Defaults: worker `high`, adversary `xhigh`. Also accepted by `adversary resume` to change them; otherwise a resume keeps the run's levels. |
| `--verify-cmd "just test"` | Extra command prefix the adversary may run (repeatable). |
| `--task-file FILE` | Read the task from a file. |
| `--no-attach` | Start the tmux session detached. |

### Resuming

```sh
adversary resume
```

This picks up the last run in the current directory (or the folder it was started from, for `--worktree` runs). It works after a crash, a closed terminal, a stall, or a run that already finished.

- **Both sessions come back:** each Claude Code session is reopened with `claude --resume`, with its full history.
- **The adversary decides what's next:** it is told the run was resumed and given the worker's last message (or told the worker's last turn left none). It checks the current state, then sends the worker what to do next, calls `hold`, or calls `finish`. This is the same hand-off as at the start of a run: the worker stays idle until the adversary's reply reaches it.
- **New instructions are optional:** `adversary resume "Also add a --verbose flag"` passes them to the adversary as an amendment to the task, and the worker starts on them first. Without them, the adversary is told to carry on with the task as it stood, including changes asked for earlier.
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
| `s` | Print the status line and the full run stats (the same block shown at close, with current figures) |
| `q` | End the run: marks it ABORTED and shuts both Claude Code sessions down (SIGTERM, then SIGKILL after 10 s). `adversary resume` picks it up again. |

Closing the run from outside tmux does the same orderly shutdown — saving state and recording the final numbers — without marking the run ABORTED (a bare close is an interruption, not a finished run). `adversary resume --kill` closes the old session (SIGHUP) on its way to relaunching, and a `kill` on the bridge (SIGTERM) also ends it cleanly.

At the close the stats are printed to the bridge pane, written to `stats.txt` in the run dir, and — when the run was started in the foreground (attached) — **reprinted in the terminal you launched from once tmux exits**, so they land in your scrollback and aren't lost when the window closes. (Opened in a new window, or started with `--no-attach`, there's no launching terminal to return to, so the bridge pane holds its readout open until a keypress instead.) The stats also go into `report.md`. When the run was resumed they show this session's numbers alongside the lifetime total; on a first run they are just the totals. Metrics are exchanges relayed, worker and supervisor turns, permission requests (and how many approved/denied), holds, `message_worker` nudges, `read_worker` peeks, stalls, human interjections, runtime, approval rate, tokens written this session (model output; input is mostly repeated cache reads, so it isn't counted), and the diff size since the run started.

The first run in a project opens a short "how this works" popup over the panes, and the first time I select the worker pane, a tip points me back to the adversary pane. Each is shown once per project; which ones I've seen is kept in `~/.local/state/adversary/projects/<folder>.json` (delete it to see them again). A key closes them.

**I talk to the run through the adversary pane on the right.** It has the keyboard when a run starts or resumes: the bridge selects its window and pane once its session has loaded. I can type into it at any time, for example to add features or change the task. Its reply to me isn't relayed, but it passes the work on with its `message_worker` tool, which reaches the worker straight away. If the worker is busy, the message is typed into its pane and Claude Code folds it into the running turn. Delivery waits while a permission request is pending, and while I have a message drafted in the worker's prompt box.

The worker pane on the left is for watching, copying output, or stopping the worker with Esc. Anything typed there is passed to the adversary as an amendment to the task, and it takes precedence over the original.

The bridge pane along the bottom is kept 4 rows high. It shows the status line (`[running]`/`[PAUSED]`, exchanges since I last spoke against the cap, approvals and denials, pending permissions, whether each side is busy or idle, "worker on hold", and "waiting for your draft") and a log of each relay. The keys above work when it has focus.

When the worker has nothing to do — because I need to decide something, or because it is waiting on a background subagent or task it launched — the adversary calls `hold`. Its reply that turn isn't forwarded and the worker stays idle, rather than the two trading "still waiting" / "leaving it be" messages while nothing happens. The hold ends when the worker's next real turn arrives (a subagent finishing brings it back on its own), when the adversary calls `message_worker`, or when I type to the worker, and a held run isn't logged as a stall.

If the adversary doesn't hold and instead keeps replying to a worker that does nothing back (the classic subagent-wait loop), the bridge steps in: after a couple of worker turns in a row that take no action after an adversary reply **while background work the worker launched is still out** (a `run_in_background` command or a background subagent/teammate that hasn't reported back), it puts the worker on hold itself, naming what it's waiting on, and tells the adversary to stop replying. With nothing in the background, a reply in words is conversation, not a wait loop — "I don't understand X", "I'll get to that after Y" — so it is always relayed, and the supervisor is prompted to re-explain with concrete detail or settle the order of work rather than repeat itself. A genuine back-and-forth loop is still caught by `--max-exchanges`. Nothing the worker says during an auto-hold is dropped: it's kept (the status line shows `worker on hold (N msg kept)`) and delivered to the supervisor ahead of the worker's next message, so it reads the whole chain in order.

A hold on background work has a **check-in deadline**: background tasks can hang, or finish without the notification that wakes the worker, and the run would sit parked forever. If the worker isn't back within 10 minutes, the bridge sends the supervisor a check-in — how long it's been, what is still running (or that nothing is), and anything kept back — and the supervisor decides whether to keep waiting (`hold` again) or get the worker moving (`message_worker`). Unanswered, it asks again at double the interval, up to an hour. The supervisor can set its own interval with `hold(check_after_minutes=N)` (0 turns it off); a hold that waits on you rather than on background work has no check-in unless it asks for one. Messages a subagent sends back to the worker ("Another Claude session sent a message…") are treated as background notifications, not as me typing, so they don't reset the loop guard.

**`finish` isn't final.** When the adversary marks a run complete and I come back later with a new task or a follow-up, I just tell the adversary in its pane; its `message_worker` call picks the finished run back up and starts the worker again (the report already on disk stays as the record of that round). I can also restart the worker by typing straight into its pane. This is lighter than `adversary resume`, which is for picking a run up in a fresh tmux session after the window is gone.

Each run writes to `~/.local/state/adversary/runs/<timestamp>/`:

- `transcript.md`: every relayed message, permission decision and human input
- `report.md`: outcome, the adversary's summary, the run's metrics (exchanges, worker/supervisor turns, permission decisions, holds, nudges, peeks, stalls, runtime, tokens, diff size — this session alongside the lifetime total on a resumed run) and the files changed since the run started
- `stalls.jsonl`: one record per stall, written when both agents have been idle for `--stall-after` seconds with the run unfinished. Nothing moves the run forward at that point, so it is almost always a relay failure. Each record holds both sides' last turn, queued messages, pending permissions and last message. It is also shown in the bridge pane and the transcript, and appended to `~/.local/state/adversary/stalls.jsonl` across all runs.
- `snapshot/`: the folder's original contents (folders without git only)
- `state.json`: counters, runtime and token totals carried over by `adversary resume`
- `stats.txt`: the end-of-run stats block, written at close and reprinted in the launching terminal
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
- **Messages are free-form text.** The bridge never parses what either side says. The only structured signals are the adversary's MCP tool calls: `approve_tool`, `deny_tool`, `message_worker`, `hold`, `read_worker`, `list_agents`, `changed_files` and `finish`. They are served by `adversary/mcp_server.py`, a dependency-free stdio server.
- **The adversary can look over the worker's shoulder.** Only the worker's turn-end messages are relayed, and its Read/Grep/Glob calls never raise a permission prompt, so `read_worker` lets the adversary read the worker's own last N responses and tool calls (with each result) straight from its session log — to check a "done" claim against what the worker actually ran. It is read-only and sends nothing to the worker.
- **Subagents are visible too.** When the worker spawns a subagent (the Task/Agent tool), that subagent's own tool calls still reach the adversary as permission requests, and the request names the subagent and its agent id (a subagent request is marked as such, not confused with the main worker). Work delegated to a subagent shows in the worker's own log only as that one call and its result, so `read_worker(agent="<id>")` reads the subagent's own transcript — the id comes from its permission request, or from the list a plain `read_worker` appends of every subagent the worker spawned. `list_agents` lists the worker (busy/idle) and each subagent this session with its id, type, task, foreground/background, and whether it is still running or finished (`all=true` adds those from before a resume). A **deny reason goes back to the subagent that asked**, not the main worker, since the permission hook fires in that subagent's context and its decision returns there.
- **The adversary cannot change the project, but it can research.**
  - It runs with `--permission-mode dontAsk`.
  - Its allowlist is Read/Grep/Glob, WebFetch/WebSearch, and read and verify Bash prefixes (git diff/log/status, test runners and similar). Web access lets it study a reference the task names — a GitHub repo, docs, an API — and plan from it itself, so it isn't blind to anything outside the local repo.
  - When the working folder isn't itself one git repo but contains some (an app repo beside its framework repo, say), each is found at launch and the adversary may inspect its history with `git -C <subdir> log/status/diff/show/ls-files`. Both the subdir and the verb are pinned in the allow rules, so this stays read-only.
  - Edit, Write and NotebookEdit are disallowed.
  - `--setting-sources project` keeps personal allow rules from widening its tools.
- **Startup dialogs are accepted automatically.** The bridge accepts the folder-trust dialog (and the skip-permissions confirmation, if shown) for the working folder.

## Caveats

- **Pre-approved commands skip the adversary.** In default mode, a command already allowed by user settings (for example a `Bash(curl:*)` rule in `~/.claude/settings*.json`) runs without reaching the adversary. `--strict-approvals` prevents this.
- **Answering on screen:** if a worker permission prompt is answered on screen, the adversary's pending request expires when the worker's turn ends.
- **Typing while the bridge delivers:** the bridge never types into a prompt box holding a draft, a pane in Claude Code's shell mode (`!`, where Enter would run the message as a command), or a pane in tmux copy mode. Shell and copy mode are read from tmux; a draft comes from a mod. A small mod (`adversary/draftmod/`) is loaded into both sessions with `--plugin-dir`; it reports to `draft-<role>.json` in the run dir whether the box holds unsent text, and the bridge waits until I send or clear it, rechecking every second. This needs a Claude Code with mods (plugin hook modules); on one without them nothing is reported and the bridge delivers as before, so press `p` before typing there. A turn is routed by the bridge's message only once Claude Code confirms that message went in as a prompt; a turn without that confirmation (a `!` command, say) is the person's, its reply isn't relayed, and the message is sent again. Enter is pressed again only while the box still starts with the bridge's message. If tmux refuses a delivery, it's retried after 5 seconds. If a typed prompt is submitted before the bridge's message goes in, the bridge recognises it as typed (it lacks the bridge's header), treats the turn as the person's, and resends its own message afterwards.

## Layout

```
adversary/
  cli.py          adversary run: run directory, snapshot, tmux layout
  bridge.py       socket server, relay logic, permission routing, caps, status
  hook.py         hook command used by both sessions
  mcp_server.py   approve_tool / deny_tool / message_worker / hold / read_worker / list_agents / changed_files / finish
  help/           text of the one-time popups (intro, worker-pane tip)
  draftmod/       Claude Code mod reporting whether a prompt box holds an unsent draft
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
