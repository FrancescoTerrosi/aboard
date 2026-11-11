# ABoard findings & open work

Working notes from the TUI / tuplespace review pass. Each item is marked
**fixed** (implemented + tested) or **open** (needs a decision or a change).
Test suite: **158 passed**.

---

## Fixed

### F1. Detail pane flickered on every update
The right-hand pane re-rendered identical content on worker events /
reloads.

- **Fix:** `_detail_update()` memoizes the last rendered markdown and skips
  no-op renders (`markdown == last and view == current`).
- **Test:** covered indirectly by the TUI command tests (no regression).

### F2. Finished agents/workers showed "running" forever
`/agent start`, `/work`, and `/spawn` never removed runners after they exited,
so `/agent [list]`, `/agents`, and `/status` reported them as still running.

- **Fix:** release-on-exit wrappers `_run_task_worker()` and
  `_run_generic_agent()` remove the name when `run()` returns.
- **Test:** updated TUI tests use live-until-stopped fakes and assert the
  name is released.

### F3. Discussion transcript was hard to follow
Turns rendered as flat `name: text` lines with no ordering cues and no
auto-scroll.

- **Fix:** `_show_discussion()` renders numbered turns with headings and
  separators (`### alice · turn 1` … `---` …) and scrolls to the latest turn.
- **Test:** `test_discuss_command_starts_and_shows_transcript`.

### F4. Discussion transcript invisible after restart
`/discuss` (no arg) depended on an in-memory `Discussion` object, which is
gone after a restart — so the `agent_turn` transcript was orphaned.

- **Fix:** `/discuss` (no arg) now reads `agent_turn` directly from the space.
- **Test:** `test_discuss_no_arg_reads_transcript_from_the_space`.

### F5. Discussion seats never freed
After a discussion ended, the two participant names stayed in `_llm_agents`,
so `/discuss a b` was refused for the rest of the session.

- **Fix:** `_run_discussion()` releases both names and the discussion on exit.
- **Test:** `test_discuss_command_starts_and_shows_transcript` asserts the
  seats are freed after `/stop`.

### F6. `/chat` agent could not see agent/discussion transcript
The chat agent's tools only covered curated tags + proposals + stock.

- **Fix:** added a read-only, bounded, attributed `agent_turns` tool and
  mentioned it in the system prompt.
- **Test:** `test_agent_turns_tool_reads_the_transcript`.

### F7. "skill" was ambiguous (two unrelated meanings)
The curated `skill` tag (`/add skill …`, board content) and the worker's
`[skills…]` routing list (`/agent add …`) shared a name with no
disambiguation.

- **Fix:** explicit "two skill meanings" callouts in README, RUNBOOK,
  CHEATSHEET, `agents.py`, and `workers.py`.

### F8. No in-TUI raw tuple inspection
Only curated tags were reachable in the table; orchestration tuples
(`dependency`, `task_skill`, `completed`, `review`, `agent`, `config`,
`agent_turn`, …) had no human view.

- **Fix (partial — see O1):** `/tuples [tag]` — read-only, bounded, attributed
  detail-pane view of one tag or the whole space grouped by tag.
- **Test:** `test_tuples_command_reads_any_tag_and_all`.

---

## Open

### O1. Dedicated inspector panel (item C)
`/tuples` is a detail-pane lens. The remaining ask is a **dedicated
full-screen inspector**:

- own keybinding (proposed: `\`) + `/tuples` alias
- DataTable over **all** tuples with a client-side filter box
- generic columns `tag | id | content` (or per-tag columns when filtered)
- **read-only** — no add/delete/claim from this screen
- bounded loading, memoized live refresh on worker events

**Decisions needed:**
1. Entry: `\` keybinding only, or `\` + `/tuples` both opening the panel?
2. Columns: generic `tag | id | content`, or per-tag when a single tag is
   selected?

### O2. Discussion participant lifecycle
Still deferred: no `join` / `swap` / `vacate` for a live discussion. The
transcript survives a stop, but a third participant can't enter and a
participant can't be replaced mid-thread.

**Decision needed:** keep deferred, or implement `swap`/`vacate`/`join`
(seat/vacancy state + `/stop` semantic overload).

### O3. Mid-discussion human steering
A human can start and stop a discussion but cannot inject a prompt while it
runs (topic is fixed at start). To "continue" with new steering you must
stop and restart with a new topic.

**Decision needed:** add a nudge command (e.g. `/discuss prompt <text>`) that
writes a human-authored context tuple the next speaker sees, or leave as-is?

### O4. Finished-worker evidence after release
Once a worker is released from `_task_workers` (F2), `/status` no longer
shows its `tasks_done`/token totals. The evidence still exists on the board
(proposals, `completed` tuples, WAL), but a "last run" summary per named
agent may be useful.

**Decision needed:** add a per-agent last-run summary, or rely on proposals +
WAL?
