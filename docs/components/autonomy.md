# Autonomy engine — self-directed runs

The mesh is *reactive* by default: an agent answers a message and goes idle.
Nothing keeps it working on its own over many turns. The **autonomy engine**
(`autonomy/`) adds the missing piece — a way to hand the fleet a goal once and
have it decompose, delegate, build, review and converge on a finished,
tested, documented result without a human in the loop, while staying inside hard
guardrails.

It is **default-on but dormant**: the Stop hook is installed on every
participating agent, but it does nothing until a *run* is active and the agent
is one of its participants. Normal interactive mesh life is untouched.

## The three bricks

### 1. Shared task board — `board.py`

The work graph is an **append-only event log** at `<root>/.loom/board.jsonl`.
The state of every task is obtained by folding the events. This buys three
things at once:

- **Nothing to corrupt** — append-only; a crash loses at most one line.
- **The log *is* the making-of** — who did what, in order (see below).
- **Trivially serializable** — a dashboard/API is a thin view over the same fold.

Task lifecycle:

```
todo ─assign→ assigned ─claim/start→ in_progress ─submit→ in_review ─review_passed→ done
                            ▲   │              │                        │
                     unblock│   │block   review_failed              reopen (coordinator)
                            │   ▼              │                        │
                          blocked              └─→ in_progress ◀────────┘  (to=in_progress)
                                                       done ─reopen(to=in_review)→ in_review
any non-done ─abandon→ abandoned   (terminal)
```

An agent keeps working while it owns a task in `{assigned, in_progress}` whose
dependencies are met. `in_review` and `blocked` hand control to the coordinator.

The terminal states are `done` and `abandoned`. Two coordinator-driven
transitions close the loop mechanically (they are events on the log, never a
reinterpretation of the fold — replaying the log always yields the same state):

- **`reopen`** — a `done` task that skipped its Fable review is reopened
  (`done → in_review`, owner/branch kept) and routed back to a Fable reviewer. A
  reopen on a non-`done` task is a stale event and is ignored.
- **`unassign`** / **`abandon`** — a stalled task returns to the pool
  (`assigned|in_progress → todo`, `reassignments += 1`); a task that can never
  finish is `abandon`ed (terminal). Both are covered under *Convergence* below.

### 2. Self-continuation — `autonomy/hooks/work_drain_stop.py`

This is the heart of the engine, modelled on the mesh `mesh-inbox-drain-stop.py`
anti-skip hook: same protocol (`{"decision": "block", "reason": ...}` keeps the
turn going; bare `exit 0` lets it stop), fail-open everywhere.

While an agent owns open work during an active run, the hook **refuses to let it
stop** and feeds it a prompt to keep going. Convergence comes from the agent
itself calling `loom-task submit`/`done` — the task leaves `open_for`, the hook
allows the stop.

Boundary guards (the sandbox): no `LOOM_ROOT` → no-op; no active run → no-op;
agent not a participant → no-op.

It carries two safety mechanisms:

- **Kill-switch** — before asking the agent to continue, it consults the fleet
  usage guard (below). On a `KILL` verdict it stops instead, records `usage_kill`
  on the board, and lets the coordinator pause the run.
- **Anti-thrash backstop** — if the work signature (tasks + progress count)
  hasn't moved for `MAX_STALE` turns, it escalates: marks the tasks blocked and
  lets the agent stop, so the coordinator can intervene.

### 3. Coordinator — `coordinator.py`

The coordinator is itself an agent (it needs judgement: how to decompose a goal,
whether a review really passes, whether a blocker needs the human). This module
gives it the *mechanism* so its loop stays small and deterministic:

- `assign_ready(board, agent_scopes)` — assign ready unowned tasks to the
  least-loaded eligible agent, by scope.
- `situation(board, run, usage_verdict)` — one structured snapshot per tick.
- `next_actions(situation)` — the to-do list that snapshot implies (review these,
  triage those blockers, assign these, halt on kill, wrap up on convergence).

## Coordinator playbook

The coordinator runs a thin loop: **call `coordinator_tick`, act on the report,
repeat.** `orchestrator.py` does the bookkeeping; the agent supplies judgement.

A full run, end to end:

1. **Decompose** (judgement). Read the goal, break it into tasks with a `scope`,
   `deps`, and `acceptance` criteria. Create them: `loom task new <id> --title …
   --scope … --deps … --acceptance …`. This is the only step that genuinely needs
   the model.
2. **Start the run**: `loom run start <id> --participants a,b,… --goal "…"
   --cap 6700000`. The Stop hook is now live for those participants.
3. **Loop** — each iteration:
   ```python
   from autonomy.orchestrator import coordinator_tick
   rep = coordinator_tick(root, {"worker-a": ["py"], "worker-b": ["frontend"]},
                          usage_dir=f"{root}/.loom/usage")
   ```
   - `rep.halt` → stop (run ended, usage kill-switch, or the `max_iterations` cap
     tripped). The cap counts **coordinator ticks**, not agent turns; on the
     `N+1`-th tick the run is *ended* (`run.active()` → false), which disarms every
     participant's Stop hook. `loom run status` shows the live `iterations` count.
     Don't push on.
   - `rep.done` → every task done: generate the making-of, end the run (below).
   - `rep.assigned` → tasks just handed out. `rep.kick` → participants now holding
     open work; **nudge each** to look at the board (mesh `mesh-send <agent> "run
     `loom mine` — you have open tasks"`, or a send-keys push). Once nudged, the
     work-drain hook keeps them going on their own. A worker starts by claiming
     its task and creating its isolated worktree: `loom task claim <id>` then
     `cd "$(loom task worktree <id> --base main)"`.
   - `rep.review` → tasks `in_review`. **Judge** each: are the acceptance criteria
     met, tests green, audit clean? `loom task review <id> --pass` (then integrate
     the branch) or `--fail -m "…"` (it returns to the worker). The **Fable gate**
     is mechanical: if a task reaches `done` without a Fable `review_passed`, the
     tick **reopens** it (`done → in_review`) and routes it to a Fable reviewer via
     `rep.review_routing` — no manual "re-open and route" step, and convergence
     stays held until a Fable signs off. `rep.gate_violations` lists what was
     reopened; the reopen is emitted once (the reopened task is no longer `done`,
     so the next tick sees no violation).
   - `rep.triage` → blocked tasks. Resolve, or `unblock`, or escalate to the human
     if it needs a decision the fleet can't make (ambiguity, cost, destructive).
4. **Integrate** (judgement + git). On `--pass`, merge the task's branch into the
   integration branch once tests are green. Merge is left to the agent (it is
   environment-specific and must not be automated past a red test); record it with
   `loom log commit --field sha=… --field message=…` so it lands in the making-of.
5. **Converge**: when `rep.done`, run `python3 -m autonomy.making_of --root <root>
   -o MAKING_OF.md`, deliver it alongside the software, and `loom run end
   --reason converged`.

The loop itself is whatever keeps the coordinator alive between ticks — a Claude
Code `/loop`, a cron-driven wake, or the coordinator's own work-drain. Escalate to
the human **only** on halt, ambiguity, or a destructive/expensive decision;
everything else stays inside the fleet.

## Guardrails

- **Per-task worktrees** (`worktree.py`) — several agents on one host must not
  share a checkout. Each task gets its own `git worktree` under
  `<repo>/.loom/worktrees/<task-id>` on its own branch (`loom/<task-id>` by
  default), branched from a shared base. The worker does `loom task worktree <id>`
  (prints the path to `cd` into) and works there in isolation; the coordinator
  merges the branch on review-pass and removes the worktree. This is the same
  isolation cross-host runs get from separate branches, extended to one host.
  `ensure` self-heals a worktree whose directory was deleted by hand (prunes the
  stale registration and recreates it). After convergence, `loom run gc --into
  <branch>` removes the run's worktrees and deletes the `loom/*` branches fully
  merged into the integration branch (never an unmerged one).
- **Usage guard** (`usage_guard.py`) — fleet-wide rate-limit budgeting over
  [`ccusage`](https://github.com/ryoppippi/ccusage) (pinned version, never
  `@latest`; override with `LOOM_CCUSAGE_VERSION`). Each host writes its active
  5-hour-window snapshot to `<root>/.loom/usage/<source>.json`; the guard
  aggregates them and judges **OK / WARN / KILL / UNKNOWN** against an
  operator-calibrated cap — killing on usage **or** projected end-of-window. It
  budgets on `input + output + cacheCreation` tokens (cache reads are excluded;
  they dominate the raw count but barely weigh on the limit). A real 429 is the
  hard backstop the caller should also watch for. **Fail-closed**: the absence of
  data is never read as "all good". An idle host writes an *idle marker* (a fresh,
  deliberate 0), not a deleted file; a probe failure writes nothing and surfaces
  as staleness. `collect()` classifies coverage into `fresh/idle/stale/missing`
  (`missing` needs the expected host labels — `loom run start --usage-sources
  primary,secondary`), and any `stale`/`missing` host yields **UNKNOWN**, a *soft pause*: the
  coordinator stops assigning new work and the Stop hook stops force-relaunching
  agents (their tasks stay open, never blocked), resuming automatically when data
  returns. A partial aggregate that already breaches kill still kills — unknown
  never masks a real breach.
- **Per-run limits** travel in `run.json` (`window_token_cap`, `max_iterations`,
  `stall_after_seconds`, `max_reassignments`, `stuck_ticks_halt`), not the code.
- **Quality gate** — a task only reaches `done` through `review_passed`; the
  convention is *no merge without green tests + an audit pass*.
- **Wedge detectors** — no task can stay invisible-and-immobile forever. Each
  tick runs three mechanical checks:
  - **Unsatisfiable deps** — a dependency that is missing, `abandoned`, or on a
    cycle makes its dependent *wedged* (`situation().wedged`, surfaced in
    `triage`). A dependency is only satisfied by a real `done`; an abandoned dep
    never satisfies. Remediate with `loom task deps <id> --deps …` or
    `loom task abandon <id>`.
  - **Agent stall** — an owned task with no event for `stall_after_seconds`
    (default 1800s; the work-drain hook logs `progress` every turn, so silence
    means a dead agent) is `unassign`ed back to the pool and reassigned to a peer
    (the dead owner is excluded that tick, not handed its own task back), up to
    `max_reassignments` (default 2), then escalated to `blocked`.
  - **Stuck run** — nothing open, ready, in review, or auto-fixable, yet not
    converged (only wedged/blocked tasks remain): `Actions.stuck` is set with an
    actionable note; after `stuck_ticks_halt` consecutive stuck ticks (default 3)
    the tick logs `run_wedged` and halts the loop **without ending the run** (the
    coordinator-agent or human breaks the deadlock; `max_iterations` is the hard
    backstop).
- **Convergence** — a run converges when every task is terminal (`done` or
  `abandoned`). All `done` = success; any `abandoned` = *converged with failures*,
  reported as such in the tick note. The work-drain hook's anti-thrash `MAX_STALE`
  backstop covers a *live* agent spinning in place; the stall detector covers a
  *dead* one — the two are complementary.

## The making-of — `making_of.py`

The brief was explicit: alongside the software and its docs, produce a narrative
of *how it was built*. Because the board is an event log, this writes itself —
`render(root)` folds `board.jsonl` into prose: the run summary, what was built
(tasks, branches, artifacts), the tooling the agents created for themselves
(skills/hooks/MCP servers, via `loom log skill_created ...`), blockers and
decisions, logged discussion, and a full timeline.

To capture the agents' *real* back-and-forth (not just what they logged
explicitly), pass the message bus transcript:
`render(root, discussion=load_jsonl_messages(inbox_paths))`, or the CLI
`--mesh <inbox.jsonl>` option. The messages are filtered to the run's window and
participants and rendered as a **Conversation (mesh)** section. The loader only
relies on generic `from`/`to`/`ts`/`body` fields, so any bus works and the repo
stays free of a hardcoded mesh location.

## The CLI — `cli.py`

One entry point agents and the coordinator drive from the shell:

```
python3 -m autonomy.cli run   start|end|status ...
python3 -m autonomy.cli task  new|assign|claim|start|progress|block|unblock|submit|review|done|reopen|abandon|deps|list ...
python3 -m autonomy.cli mine                       # tasks open for me right now
python3 -m autonomy.cli log   <kind> --field k=v   # free making-of event
```

The project root is `$LOOM_ROOT`; the acting agent is `$MESH_AGENT` (the tmux
session name wins inside a real pane, matching the rest of the mesh). Wrap it as
`loom-task` / `loom-run` for the short forms the hook references.

## Wiring (default-on but dormant)

Install the Stop hook on every participating agent's Claude Code settings,
alongside the existing mesh drain hook:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "python3 /path/to/loom-mesh/bridges/matrix/hooks/mesh-inbox-drain-stop.py" },
        { "type": "command", "command": "python3 /path/to/loom-mesh/autonomy/hooks/work_drain_stop.py" }
      ] }
    ]
  }
}
```

Set `LOOM_ROOT` in each agent's environment to the shared project checkout. With
no active run the hook is inert. To start a run:

```
LOOM_ROOT=/path/to/project python3 -m autonomy.cli run start <id> \
    --participants worker-a,worker-b --goal "…" --cap 6700000
```

## Cross-host runs

Within a host, appends serialize through an advisory `flock`. Across hosts, the
board syncs through the same git/transport the project uses (each host commits
its branch; the coordinator integrates). The usage directory syncs the same way,
or each remote agent pushes its snapshot file to the coordinator over the mesh.
```
