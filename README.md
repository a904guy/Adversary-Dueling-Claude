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

The package has no runtime dependencies beyond the standard library. It installs two names for the same command, `adversary` and `claude-adversary`; use whichever you like.

## Usage

```sh
adversary run "Implement X so all tests pass and document it"
```

That is all that's required. It works in the current directory. Every flag below is optional.

| Flag | Effect |
|---|---|
| `--repo PATH` | Work in `PATH` instead of the current directory. |
| *(default)* | The worker runs in Claude Code's auto mode: its classifier approves what's safe, and every prompt that remains goes to the adversary, which must call `approve_tool` or `deny_tool`. The worker sees the deny reason. |
| `--worker-permission-mode manual` | Every worker permission prompt goes to the adversary. |
| `--skip-permissions` | The worker runs with `--dangerously-skip-permissions`, so there is no approval traffic. |
| `--worker-permission-mode acceptEdits` | Edits are auto-approved and only the remaining prompts (for example Bash) go to the adversary. This is cheaper, because each prompt costs a full adversary turn. |
| `--strict-approvals` | Ignore user-level settings for the worker, so no personal allow rule lets a tool call skip the adversary. |
| `--worktree` | Work in a fresh `git worktree` on branch `adversary/<timestamp>` (git repos only). |
| `--max-exchanges N` | Loop guard: after this many round trips between the agents without me (default 30), the worker is put on hold. The count restarts whenever I type to either side and on resume, and the run is never ended by it. Permission requests don't count. |
| `--stall-after SECONDS` | Treat it as a stall when both agents sit idle this long while the run is unfinished (default 120, `0` turns it off): it's logged and the supervisor is woken to get the run moving. |
| `--worker-model`, `--adversary-model` | Pick the model for each side. Both default to `opus`, Claude Code's alias for the latest Opus, whatever the account's default model is. |
| `--worker-effort LEVEL`, `--adversary-effort LEVEL` | Effort level for each side (`low`, `medium`, `high`, `xhigh`, `max`; passed as `claude --effort`). Defaults: worker `high`, adversary `xhigh`. Also accepted by `adversary resume` to change them; otherwise a resume keeps the run's levels. |
| `--adversary-mcp SPEC` | Give the supervisor an MCP server by name (repeatable; by default it gets the worker's browser servers read-only: every Chrome DevTools server, and Claude in Chrome when it's on by default). `NAME` is the server's key under `mcpServers` in your MCP config — the name `claude mcp list` shows — and gives all its tools; `NAME:readonly` a look-don't-touch preset for browser servers (Chrome DevTools, Claude in Chrome: navigate, snapshot, screenshot, console/network — no clicking, typing or running script); `NAME:tool1,tool2` exact tools. `claude-in-chrome` turns on Claude in Chrome. On resume it replaces the run's choice; `none` clears it. |
| `--adversary-permission-mode auto` | The default. The supervisor may run ordinary commands to check the worker's claims about the machine as well as the code (`lsusb`, `ps`, `systemctl status`, `journalctl`, `curl` against the app), and Claude Code's auto-mode classifier stops risky ones. It still can't Edit or Write, and its prompt tells it to look, not change. |
| `--adversary-permission-mode strict` | The supervisor gets only its allowlist (`dontAsk`): Read/Grep/Glob, web research and read/verify Bash prefixes. Anything else is refused. |
| `--verify-cmd "just test"` | Extra command prefix the adversary may run (repeatable). In strict mode, this is how to allow another command. |
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
- **Settings can change on resume:** `resume` takes the same setting flags as `run` — `--worker-permission-mode` / `--skip-permissions`, `--adversary-permission-mode`, `--strict-approvals` (or `--no-strict-approvals`), `--max-exchanges`, `--stall-after`, `--worker-model`, `--adversary-model`, `--worker-effort`, `--adversary-effort`, `--verify-cmd` (added to the run's existing ones) and `--no-attach`. Anything not given keeps what the run used; anything given is saved for later resumes too, and the launcher prints what changed. Only the task, `--task-file` and `--worktree` are `run`-only.
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

At the close the stats are printed to the bridge pane, written to `stats.txt` in the run dir, and — when the run was started in the foreground (attached) — **reprinted in the terminal you launched from once tmux exits**, so they land in your scrollback and aren't lost when the window closes. (Opened in a new window, or started with `--no-attach`, there's no launching terminal to return to, so the bridge pane holds its readout open until a keypress instead.) The stats also go into `report.md`. When the run was resumed they show this session's numbers alongside the lifetime total; on a first run they are just the totals. Metrics are exchanges relayed, worker and supervisor turns, permission requests (and how many approved/denied), holds, `message_worker` nudges, `read_worker` peeks, stalls, human interjections, runtime, approval rate, tokens written this session (model output; input is mostly repeated cache reads, so it isn't counted), and the diff size since the run started: files, lines added and lines removed, counting new files a worker hasn't committed. In a folder that isn't one git repo, each git repo inside it gets its own line, counted with git from the commit it was on when the run started, so its `.gitignore` applies. Everything else is counted from the snapshot, with the biggest top-level folders named, so scratch output such as screenshots shows up as such and doesn't hide the code changes. Binary files count as files but add no lines. A run from before starting commits were recorded gets them on `resume`: the last commit before the run began.

The first run in a project opens a short "how this works" popup over the panes, and the first time I select the worker pane, a tip points me back to the adversary pane. Each is shown once per project; which ones I've seen is kept in `~/.local/state/adversary/projects/<folder>.json` (delete it to see them again). A key closes them.

**I talk to the run through the adversary pane on the right.** It has the keyboard when a run starts or resumes: the bridge selects its window and pane once its session has loaded. I can type into it at any time, for example to add features or change the task. Its reply to me isn't relayed, but it passes the work on with its `message_worker` tool, which reaches the worker straight away. If the worker is busy, the message is typed into its pane and Claude Code folds it into the running turn. Delivery waits while a permission request is pending, and while I have a message drafted in the worker's prompt box. The same goes for the adversary's ordinary replies (what it says at the end of a turn in answer to the worker): they're sent to the worker immediately too, folded into its running turn if it's busy, rather than held until the worker finishes.

The worker pane on the left is for watching, copying output, or stopping the worker with Esc. Anything typed there is passed to the adversary as an amendment to the task, and it takes precedence over the original.

The bridge pane along the bottom is kept 4 rows high. It shows the status line (`[running]`/`[PAUSED]`, exchanges since I last spoke against the cap, approvals and denials, pending permissions, whether each side is busy or idle, "worker on hold", and "waiting for your draft") and a log of each relay. The keys above work when it has focus.

When the worker has nothing to do — because I need to decide something, or because it is waiting on a background subagent or task it launched — the adversary calls `hold`. Its reply that turn isn't forwarded and the worker stays idle, rather than the two trading "still waiting" / "leaving it be" messages while nothing happens. The hold ends when the worker's next real turn arrives (a subagent finishing brings it back on its own), when the adversary calls `message_worker`, or when I type to the worker, and a held run isn't logged as a stall.

If the adversary doesn't hold and instead keeps replying to a worker that does nothing back (the classic subagent-wait loop), the bridge steps in: after a couple of worker turns in a row that take no action after an adversary reply **while background work the worker launched is still out** (a `run_in_background` command or a background subagent/teammate that hasn't reported back), it puts the worker on hold itself, naming what it's waiting on, and tells the adversary to stop replying. With nothing in the background, a reply in words is conversation, not a wait loop — "I don't understand X", "I'll get to that after Y" — so it is always relayed, and the supervisor is prompted to re-explain with concrete detail or settle the order of work rather than repeat itself. A genuine back-and-forth loop is still caught by `--max-exchanges`. Nothing the worker says during an auto-hold is dropped: it's kept (the status line shows `worker on hold (N msg kept)`) and delivered to the supervisor ahead of the worker's next message, so it reads the whole chain in order.

A hold on background work has a **check-in deadline**: background tasks can hang, or finish without the notification that wakes the worker, and the run would sit parked forever. If the worker isn't back within 10 minutes, the bridge sends the supervisor a check-in — how long it's been, what is still running (or that nothing is), and anything kept back — and the supervisor decides whether to keep waiting (`hold` again) or get the worker moving (`message_worker`). Unanswered, it asks again at double the interval, up to an hour. The supervisor can set its own interval with `hold(check_after_minutes=N)` (0 turns it off); a hold that waits on you rather than on background work has no check-in unless it asks for one. Messages a subagent sends back to the worker ("Another Claude session sent a message…") are treated as background notifications, not as me typing, so they don't reset the loop guard.

**The run repairs itself where it can.**

- **An agent that exits** (the kernel killed it for running out of the run's memory, a crash, `/exit`) is started again in a new pane with its conversation, after 5 seconds, doubling with each restart. The supervisor is told what happened, and when it was killed for memory, it's asked to have the worker keep memory use down. An agent that exits five times within an hour is left down; a managed run closes then, so the manager can start it fresh.
- **A stall** (both agents idle with the run unfinished, nothing on its way) wakes the supervisor to check the worker and give it its next step, up to three times until the worker moves again. A side whose turn ended without a Stop event (an Esc interrupt) is treated as idle. A card that's waiting on people isn't woken; it parks.
- **A message that never went in** (no submit, nothing running, after 30 seconds) is settled from the session log: delivered if it's there, sent again if not.
- **A compacted supervisor** gets its role and the task back straight away. Its role file is the system prompt and survives compaction, but the brief (the task verbatim, the repo and baseline, how its Bash works) was its first message and is only summarised. So the SessionStart hook that fires after the compaction returns a reminder of both as `additionalContext`. This doesn't need a turn or typing into the pane, so it works mid-turn too. A person's `/clear` is left alone.
- **A dialog over the prompt** holds deliveries back, like a draft. In a pane nobody is attached to it's closed with Esc after a minute. Both agents run with Claude Code's `/auto-mode-setup` prompt switched off (`skillOverrides`), since it opens by itself and takes the keyboard.

**`finish` isn't final.** When the adversary marks a run complete and I come back later with a new task or a follow-up, I just tell the adversary in its pane; its `message_worker` call picks the finished run back up and starts the worker again (the report already on disk stays as the record of that round). I can also restart the worker by typing straight into its pane. This is lighter than `adversary resume`, which is for picking a run up in a fresh tmux session after the window is gone.

Each run writes to `~/.local/state/adversary/runs/<timestamp>/`:

- `transcript.md`: every relayed message, permission decision and human input
- `report.md`: outcome, the adversary's summary, the run's metrics (exchanges, worker/supervisor turns, permission decisions, holds, nudges, peeks, stalls, runtime, tokens, diff size — this session alongside the lifetime total on a resumed run) and the files changed since the run started
- `stalls.jsonl`: one record per stall, written when both agents have been idle for `--stall-after` seconds with the run unfinished. Nothing moves the run forward at that point, so it is almost always a relay failure. Each record holds both sides' last turn, queued messages, pending permissions and last message. It is also shown in the bridge pane and the transcript, and appended to `~/.local/state/adversary/stalls.jsonl` across all runs.
- `snapshot/`: the folder's original contents (folders without git only)
- `state.json`: counters, runtime and token totals carried over by `adversary resume`
- `stats.txt`: the end-of-run stats block, written at close and reprinted in the launching terminal
- the generated settings, MCP config and launch scripts

## Managing a pool from a 4ga board

`adversary manage` watches a [4ga Boards](https://github.com/RARgames/4gaBoards) project and keeps up to `--pool` runs working its cards. Runs started by hand (`adversary run`, `adversary resume`) are unchanged: everything below applies only to runs the manager launches.

```sh
adversary manage --project "AnySite" --folder /mnt/projects/AnySite -- --cpu-quota auto
```

Flags after `--` go to every run it starts. `--pool` (runs at once) defaults to the number of CPU threads available, so with `--cpu-quota 100%` (one core's worth per run, see below) a full pool fills the machine without any run taking more than its share.

**CPU and memory limits.** `--cpu-quota 100%` and `--memory-max 3G` (on `run`, `resume`, or after `manage --`) put a run in its own systemd slice, `adversary-run_<id>.slice`. Its worker, supervisor and bridge each join the slice as they start (`systemd-run --scope`), so everything they launch (Bash commands, MCP servers, test runners) is held to the run's limits by the kernel. The tmux server is shared, so the processes join the slice themselves; `tmux attach` works as before. `--cpu-quota auto` gives a run its share of the CPU with one thread kept free for the OS: (threads − 1) ÷ the pool size, so 4 runs on a 4-thread machine get 75% each (3 cores in all), or threads − 1 for a single `adversary run`. `--memory-max auto` gives a run its share of the machine: (RAM − 1 GB) ÷ the pool size, or RAM − 1 GB for a single `adversary run`. `manage` fills that in by itself for runs given `--cpu-quota` without a memory limit. The slice is a unit under `/run`: gone at reboot, written again on resume. Running as root it's a system unit; otherwise a user unit, which needs cgroup delegation for CPU limits.

**Several projects at once.** `adversary manage --config FILE` runs every project in a TOML file from one process, sharing one pool, so CPU and memory shares (`auto`) are worked out for the machine as a whole:

```toml
pool = "auto"                      # runs at once across all projects (auto = CPU threads)
interval = 15
members = ["you@example.com"]      # kept on every project
run_args = ["--cpu-quota", "auto"]

[[project]]
name = "Website"                   # found by name, or created with its board
folder = "/srv/website"

[[project]]
id = "1887993708561827225"         # an existing project, by its 4ga id (the bot must be a project manager)
folder = "/mnt/projects/AnySite"
pool = 2                           # optional: this project's cap within the shared pool
# run_args = [...]                 # optional: replaces the shared run_args
# members = [...]                  # optional: added to the shared members
```

Each project keeps its own lock, so a project can't be managed by two managers at once. A project that fails (for example, an id the bot can't see) logs it once and doesn't hold up the others. Command-line flags (`--pool`, `--member`, `--interval`, flags after `--`) override the file.

**Credentials.** Create a dedicated bot user in 4ga, give it access to the project, and create an API client while logged in as it (Settings → API). Comments only record a user, so the bot's own comments are told from people's by its user id. Put the credentials in `~/.config/adversary/4ga.env`, readable only by you (`chmod 600`), or pass `--board-env FILE`; `FOURGA_*` environment variables override the file:

```
FOURGA_URL=https://kabana.example.com
FOURGA_CLIENT_ID=...
FOURGA_CLIENT_SECRET=...
```

**The board.** The manager finds the project by name. If there's none, it creates one, then a board. It re-reads the project's boards on every pass, so boards people add later are picked up within one pass.
- **The bot joins every board as an editor.** As a project manager it can see every board but can't change one it isn't a member of.
- **A new board gets the six columns:** **Upcoming**, **Suggested**, **Approved To Do**, **In Progress**, **Needs Feedback** and **Done**. A new board is one with no cards and either no columns, or only the Done column 4ga's "Empty" template starts it with; that Done becomes the manager's Done.
- **A board with columns of its own** gets whichever of Suggested, Approved To Do, In Progress, Needs Feedback and Done it's missing, each placed just before the next of them it has (so a Done it already has stays last). Its own columns stay where they are; the manager never moves or uses them. Upcoming is for people to think out loud in; the manager never touches it. Creating a project needs the bot to be an admin, or project creation turned on for everyone; otherwise create the project yourself and add the bot as a project manager.

**A card's life.**
1. **Claimed.** While the pool has room, the top card in Approved To Do moves to In Progress (the manager reads it back to check it stayed). It gets a workspace and a detached run. The run's task is the card's title, description and comments so far.
2. **Worked.** The card shows the whole conversation. Every message between the agents is posted on it, headed with its direction:
   - **Message from Supervisor to Worker:** the plan, replies and `message_worker` messages.
   - **Message from Worker to Supervisor:** each of the worker's turn-ends.
   - **Message from Supervisor:** what it says to people (to a person, while the worker is on hold, after the run finished).
   - **Message from the bridge to Worker:** what the bridge sends itself, like a merge conflict to fix.

   A message over 8,000 characters is split into numbered parts, at paragraph or line breaks; a code block cut by a split is closed and reopened. 4ga itself takes comments of a million characters, so the split is for reading. Permission requests aren't posted.

   People's comments are relayed to the supervisor every 15 seconds, like typing to it.
   - **Attachments.** Files people attach are downloaded into the run's `attachments/` folder, which both agents may read (images too). Files already on the card go to the supervisor with its first message, or with the resume message on a resumed run.
   - A file attached later arrives in the same message as the comments found with it, or on its own.
   - The bot's own attachments aren't relayed, and anything over 50 MB is skipped with a note.
3. **Needs Feedback.** The supervisor and the worker both have `request_feedback(question)`. It posts the question and moves the card to Needs Feedback. A person's reply closes the request (any comment counts as the answer to every open request; the supervisor is told to ask again if it doesn't) and moves the card back to In Progress. A run with nothing to do but wait closes after 15 minutes, freeing its slot, and the manager resumes it when someone replies.
4. **Done.** The supervisor's `report_done(summary)` is the only way to finish. It's refused while a feedback request has no answer. It merges the card's branches into main, posts the summary (with the merge results and diff) and moves the card to Done. The run then closes, and the manager removes the workspace. `finish(cannot_complete)` moves the card to Needs Feedback with the supervisor's reasons.
5. **Suggested.** The supervisor's `suggest_task(title, description, tasks)` files follow-up work in Suggested, for people to approve.
6. **Follow-ups on Done.** A comment on a Done card, within 30 days of it being finished (`--done-window-days`), resumes its run with its full context: the plan, the answers people gave, what was verified.
   - The workspace is rebuilt at the same path from current `main`, so both sessions pick up where they were. Follow-ups wait behind new work.
   - The supervisor's reply goes on the card; the worker isn't woken by a question.
   - If the comment asks for changes, the supervisor gives the worker the work, the card moves back to In Progress, and `report_done` merges the revision.
   - An answered follow-up closes after 2 idle minutes. A card whose sessions are gone gets a fresh run with its comments in the task.

**Workspaces: several cards on one project at once.** Each card works in its own workspace (by default under `~/.local/state/adversary/workspaces/<project>/card-<id>`):
- Every git repo in the project folder becomes a worktree on the branch `adversary/card-<id>`, so the cards' code changes stay apart. This covers the folder itself, or repos inside it a level or two down.
- Everything else is a symlink to the real file or folder, shared by all cards: scratch folders, `.claude` settings, and inside each repo its ignored files (`.env`, `.venv`, `node_modules`, data). A fresh worktree lacks those, and the app wouldn't run without them. The links are listed in the repo's `info/exclude`, so `git add -A` can't commit them.
- At `report_done`, main's newer commits, and the remote's (fetched first, for anything pushed from elsewhere), are merged into the card branch, then main fast-forwards. Uncommitted work, or a conflict with either, is refused and goes straight to the worker to fix; the supervisor checks the fix and calls `report_done` again. A main checkout that can't fast-forward needs a person. Main is the branch each repo's own checkout is on.
- **Then it's pushed.** Each repo whose main has a remote (its upstream, else `origin`, else its only remote) pushes main there when it's ahead. The Done comment lists what was pushed. A push that fails (no credentials, or the remote moved on in between) never prompts or hangs: the card still goes to Done, merged locally, but it's marked Needs attention and the comment says what to push by hand. A repo with no remote stays local.
- The isolation is code-only. Ignored data (SQLite files, `data/`, `.venv`) is linked, so cards running side by side share one database and one environment.
- A run whose session doesn't come up within two minutes (stuck on a startup dialog the bridge can't answer) closes itself, so it doesn't hold a pool slot; the manager retries it.

**Restarts and failures.** The manager keeps no state of its own: each run's config records its card, so a restarted manager picks up where it left off. One manager runs per project; a lock enforces it.
- A run that closes while its card is still In Progress is resumed.
- A run whose card is moved back to Upcoming or Suggested, or deleted, is stopped.
- A card that fails to start or resume 3 times in 30 minutes moves to Needs Feedback with the error.

**What the board shows.**
- **Active** (green label): on each card with a run attached, kept in step on every pass, so it corrects itself within one pass after a crash. It's for display only: resuming goes by `card.json` (below), which doesn't depend on the board being reachable when a run closes.
- **The card timer:** runs while the card's run is live and pauses when it's parked, done or cut off, so it adds up the time agents spent on the card. Only cards the manager runs are touched; people's own timers are left alone.
- **Needs attention** (red label): on a card that needs a person to act before it can move on. That's either one the manager gave up on (3 failed starts or resumes in 30 minutes), or finished work that can't be merged without someone, because the main checkout is on another branch or has local changes in the way. The label comes off when the card's run starts again or the card is done.
- **Suggested by Agent** (purple label): on cards the supervisor files with `suggest_task`. Those are the only cards an agent writes a description and checklist on; people's descriptions and checklists are never touched. A board's label from before, "Suggested by agent", is renamed to match.
- **Suggested by Human** (blue label): on any card in Approved To Do without the Agent label. It was a person's idea, so the manager labels it as soon as it sees it there, including cards still waiting for room in the pool.
- **Done comment:** the summary, what was merged, the card's commits, and how long it ran, the tokens written, how many feedback requests it made, and the diff.
- **Cover image:** for visible work (a page, a UI), a supervisor with a browser tool (it has the worker's, read-only) screenshots the result and attaches it as the card's cover with `attach_image`.

**Surviving restarts.** Each card run records in its `card.json` that it's live, and clears that only when it closes on purpose: parked waiting on people, reported done, or a follow-up answered. On startup, and on every pass after, a card whose run isn't running is picked back up:
- **In Progress:** resumed, or started fresh if its sessions were never recorded.
- **Needs Feedback:** resumed if it was cut off while live (it may have been working on what didn't depend on the answer). If it was parked, it waits for a reply.
- **Done (within the follow-up window):** resumed if it was cut off mid-follow-up, or when someone comments.

Resumes come before new work in the pool. To start the manager at boot and restart it if it dies, use `examples/adversary-manage@.service`, one instance per project. Its `KillMode=process` makes restarting the manager leave the runs' tmux sessions alone.

**Runs side by side.** Run ids carry a random `-####` after the start time, so runs started in the same second don't share a run directory, socket or tmux session. Session recovery (`resume`) never takes a session another run has recorded.

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
- **Repos owned by another user still work.** On a server where the agents run as root over files another user owns, git refuses them as "dubious ownership" — no `log`, no `status` — until they're `safe.directory`. The launch scripts mark the working folder and every git repo found inside it safe for the two sessions (and the bridge), through git's `GIT_CONFIG_COUNT`/`KEY`/`VALUE` environment variables rather than anyone's `~/.gitconfig`. The supervisor may also run `git config --global --add safe.directory <path>` itself, for a repo that appears mid-run.
- **The supervisor can be given MCP servers by name.** Its launch skips user settings (so your allow rules can't widen its read-only toolset), which also stops Claude Code loading your MCP servers for it — so they're handed over. **By default it gets the worker's browsers, read-only:** every Chrome DevTools MCP server you have (with the same definition, so the same browser: a `--browser-url` server reaches the same Chrome and profile as the worker) and Claude in Chrome when it's on by default, each with the look-don't-touch preset, so it can check the worker's claims in the browser with no flags. `--adversary-mcp` replaces that choice and `--adversary-mcp none` turns it off. Each server named with `--adversary-mcp` is looked up where Claude Code would find it (`~/.claude.json` user and local scope, approved servers in the repo's `.mcp.json`; the more specific scope wins), copied into the supervisor's `adversary-mcp.json` (readable only by you: definitions can hold API keys) and its tools allowed; `claude-in-chrome` adds `--chrome`. An unknown name fails at launch with the list of servers you have. The role prompt lists them and says they're for verifying the worker's claims (load the page, screenshot it, read console errors), not for doing the work. claude.ai connectors and plugin-provided servers can't be named. A browser server's full tool set can click, type and submit forms — use `NAME:readonly` to keep the supervisor to looking. Your answers to Claude Code's "New MCP servers found" dialog for the repo's `.mcp.json` are saved in local settings the supervisor doesn't read, so they're copied into its own settings: it doesn't ask again every launch, and only servers you already approved load (rejected ones stay off; nothing new is enabled).
- **Subagents are visible too.** When the worker spawns a subagent (the Task/Agent tool), that subagent's own tool calls still reach the adversary as permission requests, and the request names the subagent and its agent id (a subagent request is marked as such, not confused with the main worker). Work delegated to a subagent shows in the worker's own log only as that one call and its result, so `read_worker(agent="<id>")` reads the subagent's own transcript — the id comes from its permission request, or from the list a plain `read_worker` appends of every subagent the worker spawned. `list_agents` lists the worker (busy/idle) and each subagent this session with its id, type, task, foreground/background, and whether it is still running or finished (`all=true` adds those from before a resume). A **deny reason goes back to the subagent that asked**, not the main worker, since the permission hook fires in that subagent's context and its decision returns there.
- **The adversary looks and verifies; it doesn't change the project.**
  - By default it runs in auto mode: ordinary commands run, so it can see for itself whether a device is plugged in, a service is up or a page responds, and Claude Code's classifier stops risky ones. This isn't a hard read-only guarantee: Bash could still write (a `touch` in /tmp passes), so the role prompt forbids editing, installing, restarting or fixing the worker's work. Use `--adversary-permission-mode strict` for the enforced allowlist (`--permission-mode dontAsk`).
  - Its allowlist (pre-approved in auto mode, the only thing allowed in strict mode) is Read/Grep/Glob, WebFetch/WebSearch, and read and verify Bash prefixes (git diff/log/status, plain `diff` for comparing any two files or folders, test runners and similar). Web access lets it study a reference the task names — a GitHub repo, docs, an API — and plan from it itself, so it isn't blind to anything outside the local repo.
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
  cli.py          adversary run / resume / manage: run directory, snapshot, tmux layout
  manager.py      adversary manage: the pool, claiming and resuming cards
  board.py        the 4ga Boards API client
  cardmode.py     a card run's side of the board: comments, columns, card prompts
  workspace.py    per-card workspaces (worktrees + links) and the merge into main
  bridge.py       socket server, relay logic, permission routing, caps, status
  hook.py         hook command used by both sessions
  mcp_server.py   approve_tool / deny_tool / message_worker / hold / read_worker / list_agents / changed_files / finish (+ request_feedback / report_done / suggest_task on card runs)
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
