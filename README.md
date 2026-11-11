# ABoard — Agents Board

A professional, keyboard-driven TUI to navigate a tuple space of **tasks**,
**knowledge**, and **skills** shared between humans and agents.

> **Try it:** [`RUNBOOK.md`](./RUNBOOK.md) — install to first chat in five
> minutes, verified end-to-end. What it is and why: this file.
> **Quick reference:** [`CHEATSHEET.md`](./CHEATSHEET.md) — commands, tuples,
> agent mechanics, and verification paths.

ABoard is a **separate project** layered on top of the generic
[`tuple_space`](https://github.com/) substrate. It defines three conventions at
the *user* layer and enforces them via templates + wildcard search — it never
modifies the substrate, which stays schema-flexible underneath.

## The three curated tuples

| Tag | Tuple | Autogen id |
|---|---|---|
| `task` | `("task", t*N*, instruction)` | `t0, t1, …` |
| `knowledge` | `("knowledge", know*N*, name, context, text)` | `know0, know1, …` |
| `skill` | `("skill", skill*N*, name, context, text)` | `skill0, skill1, …` |

- **id** is auto-generated per tag (max index + 1), surviving restarts and
  deletions without collision.
- **Search** locks any field (exact match) and leaves the rest as wildcards —
  the substrate's associative matching. In the UI, a wildcard is called a
  *wildcard*, never `null`.
- **Filter** (the TUI's navigation) is client-side substring over the loaded
  board — pure UX, never part of the WAL or substrate semantics.

## Persistence

Every ABoard session is one substrate *run* (`.tuplespace/runs/<run_id>/`
with its replayable WAL); "which run is the board" is an ABoard-layer
decision, again without touching the substrate:

- **Launch resumes** the latest prior run with a non-empty final state —
  recovery is the substrate's replay contract, and the resumed board becomes
  a new run of its own (the transcript chain grows one run per session).
  Ids continue across restarts (`t0, t1, …` never collide).
- **Empty runs are skipped** (a launch-and-quit session, or a crash before
  the WAL flushed), so a hard crash never shadows the real board.
- **Corruption is loud**: a mid-file seq gap or an unreadable WAL line warns
  and starts a fresh board — never prefix-replayed, never deleted. Prior
  runs stay on disk for manual recovery with the substrate's tools.
- **Quit is durable**: the TUI closes (fsyncs) the WAL on exit, so the next
  resume sees everything.
- `--fresh` starts an empty board for that session only.
- **`/reset`** is the durable reset: a *factory* reset (twice-confirmed)
  that clears everything — items, chat, proposals, and settings — stops
  workers, and archives prior runs so the reset survives restarts. Why
  archiving: resume skips empty runs in favor of older non-empty ones, so
  clearing alone would be resurrected by the next launch. Archived runs
  are intact and reversible — moving one back from `<runs root>/archive/`
  restores that board, settings included. `--fresh` (above) is the softer
  alternative: an empty board for one session, resuming normally after.

## Chat modes

`/chat <text>` (or any bare text) talks to a conversational agent that
shares your board — in one of two modes, switched at runtime with
**`/mode [human|auto]`** (a command, not a launch flag — the board remembers
its own mode):

| Mode | Agent can | Board changes apply via |
|---|---|---|
| `human` (default) | read, **propose** | **you**: `/approve` (or `/reject`) |
| `auto` (opt-in) | read, **write** (`board_add` / `board_claim`) | the agent, directly |

- **Human-driven** keeps the board human-authored: the agent's change lands
  as a staging tuple `("proposal", p*N*, add|claim, tag, fields, source)` —
  the proposal inbox is `/proposals`, `/approve [pN]` applies it through the
  Board *attributed to you*, `/reject` discards. ``source`` is optional
  provenance (``{"tag", "id"}``); on approval it is recorded as a queryable
  `("derived", new_id, tag, id)` relation. A proposal gets exactly one
  decision (the take is atomic — E5's semantics, carried into ABoard), and
  the WAL records the full story: agent proposed at seq N, you decided at
  seq M. Pending proposals survive restarts.
- **Auto** is for boards that are meant to be agent-driven. Same curated
  tools, no gate.
- Either way the agent's only write path is the curated `board_*` tools
  (validated templates, autogen ids) — never raw `out`.
- **Mode lives in the space**: `/mode` stores a `("config", "mode", value)`
  tuple (take+write, so exactly one per key, full history in the WAL), and
  a resumed board restores it — the same replay that restores everything
  else. Switching is live: the next chat turn already runs in the new mode.

## Stock spaces

Shared common knowledge, mounted read-only — at runtime, with **`/stock`**:

```bash
.venv/bin/aboard --run-root ~/.aboard/stock   # curate the stock (it's just a board)
# then, inside any board:
#   /stock ~/.aboard/stock   # mount (or re-mount to refresh the snapshot)
#   /stock                   # show what's mounted
#   /stock off               # unmount
```

- The agent gets `stock_list` / `stock_search` — it can **cite** stock
  items but never write them (or the stock).
- You copy items in with `/import <tag> <id>` — re-id'd for this board,
  attributed to you; the WAL is the provenance record.
- Loading reuses the session rules (latest non-empty run, replay) but is
  lenient: a broken stock warns and loads empty rather than blocking the
  board.
- The mount is a `("config", "stock", path)` tuple — resume re-mounts it
  automatically.

## Task execution

Creating a task is not the end of the story — the operator's loop is
create → plan → work → approve:

- **`/plan [id]`** — an LLM planner decomposes the highlighted (or given)
  task into 2–5 subtasks. The parent is claimed exactly-once (the take is
  the arbitration — no double-planning); subtasks get ids `<parent>.k`,
  outside the autogen scheme, and workers claim them like any other task.
  A failed or degenerate plan re-posts the parent unchanged — no work lost.
- **`/work [n]`** — launches `n` worker agents (default 1). Each claims a
  task atomically (`inp` — exactly-once), executes it with the model
  (grounded by a bounded, newest-first board view plus read-only
  `board_list`/`board_get`/`board_search` retrieval tools, so results don't
  guess in isolation), and proposes the result as a *knowledge add* into the
  existing `/proposals` inbox — **you** approve it onto the board. The
  proposal
  carries the task id as structured provenance and a self-contained context
  (the instruction text), so approved knowledge links back via a `derived`
  relation. A worker that fails re-posts its claimed task; a worker that
  crosses its token budget stops before claiming new work.
- **`/stop`** halts all workers (in-flight tasks finish first);
  **`/status`** shows pending tasks/proposals and worker progress.
- **Named, role-specialized workers** — `/agent add <name> <role> [skills…]`
  defines a worker; `/agent start <name>` runs it. **`/tag <task_id>
  <skill>`** makes a task require a skill (a worker with skills claims only
  untagged or matching tasks), and **`/dep <task_id> <dep1>…`** declares
  task dependencies (workers skip tasks whose `completed` signals are unmet).

  > **Two different “skill” meanings.** The curated `skill` tag
  > (`/add skill …`, stored as `("skill", skill*N*, name, context, text)`)
  > is **board content** — like knowledge, it appears in the bounded board
  > snapshot every worker receives. The `[skills…]` list on `/agent add`
  > (and `/spawn`) is a **routing attribute** stored in the agent tuple and
  > matched against `/tag` requirements. They share a name but do not
  > interact.
- Everything is attributed in the WAL: `worker claimed t0 at seq N,`
  `proposed p1 at seq M, human applied at seq K`.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e /path/to/tuple_space   # the substrate
.venv/bin/pip install -e ".[tui]"               # aboard + textual
.venv/bin/aboard              # resumes the latest board (see Persistence)
.venv/bin/aboard --fresh      # an empty board for this session only
.venv/bin/aboard --model deepseek-v4-pro:cloud --name alice
# mode, stock mount: runtime commands (/mode, /stock) — see below
```

In the TUI (**`?` or `/help`** lists everything below):

- **`/`** focus the filter, type to narrow the active tag; **`enter`**
  returns to the list.
- **arrows / j / k** move the cursor; the detail pane updates instantly.
  **`enter`** on a row shows that tuple in full (replacing any view). With
  the detail pane focused, **arrows / j / k / pgup / pgdn** scroll it —
  long items, chat transcripts and proposal lists are all readable.
- **`tab` / `shift+tab`** move between panels (select → filter → table →
  detail → command). **`[` / `]`** cycle the active tag (task → knowledge →
  skill); **`1` / `2` / `3`** jump to it directly.
- **`n`** new item (pre-fills `/add <tag> `).
- **`d`** delete the highlighted item.
- **`a`** / **`r`** approve / reject the oldest pending proposal — one
  keystroke each (same as `/approve` / `/reject`), with the proposals view
  refreshing live.
- **`/add task …`**, **`/add knowledge name | context | text`**, **`/add skill …`**
  insert (multi-field via `|`).
- **`/search <tag> <text>`** exact wildcard search.
- **`/chat <text>`** (or bare text) chat with the board's agent — see Chat
  modes. **`/chat`** re-opens the transcript. **`/proposals`** the proposal
  inbox; **`/approve [pN]`** / **`/reject [pN]`** decide; **`/import <tag> <id>`**
  copy from stock.
- **`/plan [id]`** decompose a task into 2–5 subtasks (the planner claims
  the parent exactly-once; subtasks get ids like `t0.1`). **`/work [n]`**
  launch n worker agents (claim → execute → propose — you approve).
  **`/stop`** halts workers; **`/status`** the board at a glance.
- **`/agent [list]`** · **`/agent add <name> <role> [skills…]`** ·
  **`/agent start <name>`** named, role-specialized workers. **`/tag <task_id>
  <skill>`** require a skill; **`/dep <task_id> <dep1>…`** declare task
  dependencies.
- **`/spawn <name> <role> [skills…]`** launch a generic LLM agent on any
  tuples; **`/agents`** the live transcript/budget/skills panel.
  **`/review [pN]`** a reviewer writes a verdict per proposal (recommendation
  only). **`/discuss <a> <b> [topic]`** two agents take turns through the
  tuple space.
- **`/architect`** re-opens the active architect transcript.
  **`/architect start <name> <role> [skills…]`** starts (or resumes) a named,
  file-capable agent; **`/architect <text>`** talks to it turn by turn. It
  navigates the tuple space with you (raw `rdp` / `read_all`) and writes
  files/trees via `create_file` / `scaffold_files` under a sandbox;
  **`/files`** shows the projects and files agents have produced.
  File delivery is *enforced* in code, not left to the model: every file a
  turn actually wrote is also recorded as a `knowledge` tuple (name =
  relative path, context = `architect <name>`), so the knowledge tab,
  filter, and search detect delivery without hunting the filesystem.
- **`/mode [human|auto]`** show/switch the agent's mode (persists in the
  board). **`/stock [path|off]`** mount/show/unmount common knowledge.
- **`/help`** (or **`?`**) the full command reference.
- **`ctrl+q`** quit.

## Layout

```
src/aboard/
  models.py     task/knowledge/skill templates, id autogen, wildcard templates
  board.py      Board: insert / search / filter / remove / import over the substrate
  session.py    resume-the-latest-run persistence (recovery = replay) + stock loading
  chat.py       ChatAgent: two modes (human-driven / auto), curated board_* tools
  proposals.py  the staging channel: submit / apply / reject (exactly one decision)
  config.py     runtime config (mode, stock mount) as ("config", key, value) tuples
  workers.py    TaskWorker + plan: claim → execute → propose, decompose into subtasks
  agents.py     named worker definitions (name, role, skills) in the space
  llm_agents.py GenericAgent: arbitrary LLMAgents coordinating on any tuples (Tier 3.1)
  architect.py  ArchitectAgent: conversational, sandboxed file agent (Tier 3.4)
  reviewer.py   review: a reviewer writes approve/reject/abstain verdicts (Tier 3.2)
  discussion.py Discussion: two named agents take turns through the tuple space (Tier 3.2)
  http_space.py HttpSpace: join a shared HTTP board via --endpoint (Tier 3.3)
  tui.py        the textual full-screen navigator
tests/          171 tests (models, board, session, proposals, chat, config, workers,
                TUI, http_space, llm_agents, architect, reviewer, discussion)
```

## Status

Navigation (filter, tag switch, detail-on-cursor, insert/delete/search),
session persistence (resume on launch, WAL fsync on quit), `/chat` in two
runtime-switchable modes (human-driven proposals + approval, auto),
read-only stock mounts, board-scoped config (`/mode`, `/stock`), and task
execution (`/plan` planner, `/work` workers → proposals) are complete and
tested. Named role-specialized agents, skill routing, and dependency-aware
workers are complete (Tier 2).

Generic agents and the reviewer pattern (Tier 3) are complete: `/spawn`
launches an arbitrary `LLMAgent` coordinating on any tuples (live transcript,
budget, and skills in the `/agents` panel), and `/review` runs a reviewer
that writes an approve/reject/abstain verdict per pending proposal — a
*recommendation only*; `/approve` stays the human's decision, so the board
stays human-authored. The discussion sandbox (`/discuss <a> <b> [topic]`)
has two named agents take turns through the tuple space. The architect
(Tier 3.4) is a `/chat`-like, sandboxed file-capable agent: start it with
`/architect start <name> <role> [skills…]`, steer it turn by turn with
`/architect <text>`, and it navigates the raw tuple space and writes files and
directory trees via `create_file` / `scaffold_files`; `/files` lists what was
produced.

The cross-process board (Tier 3.3) is wired: `--endpoint <url>` joins a
shared HTTP board instead of opening an in-process one (see
“Cross-process board” below). The one remaining Tier 3 item is the
planner→worker→reviewer *pipeline* (the reviewer and discussion sandbox
patterns are in; the chained pipeline is not).

## Cross-process board

`--endpoint <url>` connects a TUI to a `tuple_space.server` instance, so
multiple TUIs (or the substrate's CLI) share one board over HTTP:

```bash
python -m tuple_space.server --host 127.0.0.1 --port 8765   # the board
.venv/bin/aboard --endpoint http://127.0.0.1:8765 --name alice   # participant 1
.venv/bin/aboard --endpoint http://127.0.0.1:8765 --name bob     # participant 2
```

`tuple_space.server` needs the substrate's `.[server]` extra (fastapi/uvicorn;
the same deps as ABoard's own `.[server]` extra). The ABoard *client* is
stdlib-only (`HttpSpace` uses `tuple_space.sync_client`, no httpx).

A thin `HttpSpace` adapter (stdlib-only) presents the same surface
`Board`/`config`/`proposals`/`workers` use; each op is one wire dispatch.
The server owns the run + WAL, so client-side resume/`/reset`/archive are
disabled in endpoint mode (the server is started with `--recover <run_dir>`
to resume). Id autogen stays collision-free across clients via the atomic
counter-claim, with counters seeded on connect. The server routes only the
Linda primitives; ABoard's one needed extended op (`are_dependencies_met`)
is recomposed client-side from `dependency` + `completed` tuples. Generic
agents (`/spawn`) therefore need an in-process board — the substrate's
`LLMAgent` registers capabilities/budget through extended ops the wire does
not route — so `/spawn` is refused with `--endpoint`. Workers, the reviewer,
and the discussion sandbox work over the wire because they use only the
Linda primitives.

## License

MIT — see [`LICENSE`](./LICENSE). Contributions to this repository are
accepted under the same license.
