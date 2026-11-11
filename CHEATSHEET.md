# ABoard cheatsheet

Operational quick reference for the ABoard TUI. Covers the commands, the
tuple conventions, agent mechanics, and the verification paths. Full
rationale lives in [`README.md`](./README.md) and [`RUNBOOK.md`](./RUNBOOK.md).

## 1. Keys

| Key | Action |
|---|---|
| `/` | focus the filter box (client-side substring) |
| `enter` (in filter) | return to the table |
| `:` | focus the command line |
| `tab` / `shift+tab` | cycle panels (select → filter → table → detail → command) |
| `arrows` / `j` / `k` | move cursor (detail follows) |
| `enter` (on a row) | show that tuple in full |
| `]` / `[` | next / previous tag |
| `1` / `2` / `3` | jump to task / knowledge / skill |
| `n` | pre-fill `/add <tag> ` |
| `d` | delete highlighted item |
| `a` / `r` | approve / reject (marked proposal, else oldest) |
| `?` | help |
| `ctrl+q` | quit (fsyncs the WAL) |

## 2. Tuple conventions

Curated board content:

| Tag | Shape | Autogen id |
|---|---|---|
| `task` | `("task", t*N*, instruction)` | `t0, t1, …` |
| `knowledge` | `("knowledge", know*N*, name, context, text)` | `know0, …` |
| `skill` | `("skill", skill*N*, name, context, text)` | `skill0, …` |

Orchestration / transcript (not shown in the table):

| Tag | Shape | Written by |
|---|---|---|
| `proposal` | `("proposal", p*N*, add\|claim, tag, values, source)` | chat agent (human mode), workers |
| `derived` | `("derived", new_id, source_tag, source_id)` | `apply()` on approval |
| `review` | `("review", pid, approve\|reject\|abstain, rationale)` | `/review` |
| `agent` | `("agent", name, role, [skills])` | `/agent add` |
| `task_skill` | `("task_skill", task_id, skill)` | `/tag` |
| `dependency` | `("dependency", task_id, [deps])` | `/dep` |
| `completed` | `("completed", task_id, None)` | workers / planner |
| `agent_turn` | `("agent_turn", name, text)` | `/discuss`, `/spawn` |
| `architect` | `("architect", name, speaker, text)` | `/architect` turns |
| `project` | `("project", name, sandbox_root)` | `/architect` |
| `file_created` | `("file_created", path, size, "success")` | `/architect` |
| `file_error` | `("file_error", path, error)` | `/architect` |
| `chat` | `("chat", speaker, text)` | chat turns |
| `config` | `("config", key, value)` | `/mode`, `/stock` |
| `id_counter` | `("id_counter", tag, next_index)` | id autogen |

## 3. Commands

### Board content

```bash
/add task summarize the README
/add knowledge name | context | text
/add skill name | context | text
/del                     # or `d`
/search <tag> <text>     # exact + wildcard, locks first content field
/clear [tag]
/tuples [tag]            # read-only raw view: one tag, or all grouped
```

### Chat

```bash
/chat hello               # or any bare text
/chat                     # re-open the transcript
/mode                     # show mode
/mode human|auto          # propose-then-approve | direct writes
```

- `human` (default): agent proposes; **you** `/approve` or `/reject`.
- `auto` (opt-in): agent writes directly (`board_add` / `board_claim`).

### Proposals / approval

```bash
/proposals                # the inbox; j/k select, a/r decide
/approve [pN]
/reject [pN]
```

`apply()` records provenance as a `derived` relation when the proposal
carried a `source` (`{"tag","id"}`).

### Task execution

```bash
/plan [id]                # decompose a task into 2-5 subtasks (<id>.1, …)
/work [n]                 # n anonymous generalist workers
/agent [list]
/agent add <name> <role> [skills…]
/agent start <name>
/tag <task_id> <skill>    # require a skill for a task
/dep <task_id> <dep1> [dep2…]
/stop [name]              # stop one worker/agent, or all
/status
```

### Multi-agent patterns

```bash
/spawn <name> <role> [skills…]   # generic LLMAgent (raw Linda tools)
/agents                          # generic agents panel (transcript/budget)
/review [pN]                     # reviewer writes a verdict per proposal
/discuss <a> <b> [topic]         # two agents take turns
/discuss                         # re-open the transcript
/architect                      # re-open the active transcript
/architect start <n> <role> [s] # start/resume a named architect
/architect <text>               # talk to the active architect (navigate + files)
/files                          # projects + files agents have produced
```

### Stock / import

```bash
/stock <runs_root>       # mount shared knowledge read-only
/stock                    # show the mount
/stock off                # unmount
/import <tag> <id>        # copy a stock item into this board
```

### Reset

```bash
/reset                    # type twice; clears items+chat+settings, archives runs
```

## 4. Agents, skills, and knowledge retrieval

`/work n` and named agents are tool-calling completions whose tools are
**read-only** (`board_list`, `board_get`, `board_search`). At claim time each
worker sees:

1. the task instruction
2. a **bounded, newest-first board snapshot** (`board_view`): up to 8 items
   per curated tag, with the true total (`+N more`)
3. read-only tools to retrieve beyond the snapshot
4. skill requirements and dependencies (used for routing, not shown)

Skill routing:

- `/work n` → generalist (`skills=[]`), claims anything not blocked by deps.
- named agent → has `role` + `skills`; claims a task only if it is untagged or
  every `task_skill` tag is in the worker's skill list.

> **Two “skill” meanings.** The curated `skill` tag (`/add skill …`) is
> **board content** in the snapshot; the `[skills…]` list on `/agent add`
> (and `/spawn`) is a **routing attribute** matched against `/tag`. Same
> word, unrelated.

```bash
/agent add writer writer writing
/agent add coder coder coding
/tag t0 writing
/tag t1 coding
/agent start writer
/agent start coder
```

**Use named agents when tasks need routing; use `/work n` for homogeneous
throughput.** For active board search / arbitrary tuples, use `/spawn`
(in-process only — extended ops aren't on the wire).

## 5. Discussion lifecycle

- `/discuss <a> <b> [topic]` — exactly **two** participants; no join/swap/vacate.
- Default budget: **3 rounds → 6 turns** (each speaks 3 times).
- `/stop <name>` ends it; seats are freed when the discussion exits.
- `/discuss` (no arg) re-opens the transcript **from the space** (survives restart).
- Resume: run `/discuss a b` again — context picks up from each side's last 3 turns.
- No mid-discussion human prompt injection (topic is fixed at start).

## 6. What the `/chat` agent can see

- chat transcript (last 12 turns)
- `task` / `knowledge` / `skill` via `board_view` + `board_list/get/search`
- pending proposals via `board_proposals`
- stock (if mounted) via `stock_list/search`
- agent/discussion transcript via `agent_turns` (read-only, bounded)

It does **not** see `review`, `dependency`, or `agent` metadata directly.

## 7. Verification

### Dependencies (`/dep`)

Behavioral check — a worker with unmet deps reports:

```
no claimable tasks (N pending — blocked by skills/deps)
```

Raw check, remote board (`--endpoint`):

```bash
.venv/bin/python -m tuple_space.cli read-all \
  --template '["dependency", "t1", null]' \
  --endpoint http://127.0.0.1:8765

.venv/bin/python -m tuple_space.cli snapshot --endpoint http://127.0.0.1:8765
```

Raw check, in-process board (after `ctrl+q`):

```bash
run=$(ls -t .tuplespace/runs | head -1)
grep '"dependency"' ".tuplespace/runs/$run/ops.jsonl"
```

Python:

```python
space.read_all("dependency", tid, None)   # -> [("dependency", tid, [deps])]
space.rdp("dependency", tid, None)
```

### Test suite

```bash
.venv/bin/python -m pytest -q       # 171 tests
```

## 8. Budgets and limits

| Thing | Default |
|---|---|
| discussion turns | `DEFAULT_ROUNDS = 3` → 6 total turns |
| generic agent steps | `MAX_STEPS = 20` |
| worker tool follow-ups per task | `max_tool_rounds = 3` |
| chat tool follow-ups per send | `max_tool_rounds = 3` |
| chat history kept | `HISTORY_TURNS = 12` |
| board snapshot per tag | `per_tag = 8` (newest-first, bounded read + `count`) |
| tool result cap | `TOOL_RESULT_MAX = 20` |
| token budget (workers/discussion/agents) | `LLMConfig.token_budget = 100_000` (step-boundary rule; chat has no cap) |

## 9. Cross-process (`--endpoint`)

```bash
python -m tuple_space.server --host 127.0.0.1 --port 8765
.venv/bin/aboard --endpoint http://127.0.0.1:8765 --name alice
```

- Server owns the run + WAL; client-side `/reset` and resume are disabled.
- `/spawn` is refused (extended ops aren't routed); workers, reviewer, and
  discussion work over the wire.
- `--run-root` and `--fresh` are ignored with `--endpoint`.
