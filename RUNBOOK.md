# RUNBOOK — ABoard

How to install, run, and try ABoard hands-on. What the project is and why
it is shaped this way: see [`README.md`](./README.md). Everything here was
verified against the real TUI and a real LLM endpoint.

## 1. Prerequisites

- Python ≥ 3.10.
- The [`tuple_space`](../deepi) substrate (installed from its repo; not on
  PyPI). ABoard layers on top of it and never modifies it.
- An OpenAI-compatible endpoint for chat (this project uses Ollama). The
  models/board core needs no LLM at all — only `/chat` does.

## 2. Install

On Debian/Ubuntu the system Python is externally managed (PEP 668): use a
virtual environment.

```bash
cd aboard
python3 -m venv .venv
.venv/bin/pip install -e /path/to/tuple_space   # the substrate
.venv/bin/pip install -e ".[tui]"               # aboard + textual
.venv/bin/aboard                                # launches the TUI
```

Two ways to use the venv:

```bash
.venv/bin/aboard                 # explicit interpreter path
# or
source .venv/bin/activate && aboard
```

**When `python3 -m venv` fails with "ensurepip is not available"** (no
`python3-venv` package — common on minimal Debian/Ubuntu, containers) —
create the venv without pip and bootstrap pip into it with `get-pip.py`
(needs network):

```bash
python3 -m venv --without-pip .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python - --quiet
.venv/bin/pip install -e /path/to/tuple_space -e ".[tui]"
```

If the system Python does have pip (`python3 -m pip --version` works) but
no venv package, `python3 -m pip --python .venv/bin/python install …` also
works — but `python3 -m pip` does **not** exist on systems where the
`--without-pip` fallback applies, so the `get-pip.py` route above is the
one that always works.

## 3. The five-minute tour

Launch it. First run starts with an empty board (later runs *resume*, see
§7):

```bash
.venv/bin/aboard --name alice
```

You see: tag selector (task), filter box, the item table, a detail pane on
the right, a command line at the bottom. Press **`?`** at any time — or type
**`/help`** — for the full command reference. Type along — every step below
is one keystroke or one command:

```
/add task try the board out              ← insert (task needs one field)
n                                        ← pre-fills "/add task "; type "second one" + enter
tab                                      → switch to knowledge
/add knowledge commit style | shared | imperative mood, subject <= 50 chars
/                                        ← focus the filter; type "commit" (list narrows live)
enter                                    ← back to the list
d                                        ← delete the highlighted item (the detail pane follows the cursor)
ctrl+q                                   ← quit (fsyncs the WAL — the next launch resumes this board)
```

Keys that work anywhere: `tab`/`shift+tab` move between the panels
(select → filter → table → detail → command), `[`/`]` cycle the active
tag, `1`/`2`/`3` jump to task/knowledge/skill directly, `arrows`/`j`/`k`
move the cursor (with the detail pane focused they scroll it, together
with `pgup`/`pgdn` — long items and chat transcripts are readable),
`enter` on a row shows that tuple in full, `/` focuses the filter (`enter`
returns to the list), `:` focuses the command line, `n` = new item,
`d` = delete, `a` = approve, `r` = reject (the oldest pending proposal),
`?` = help, `ctrl+q` = quit. The board is fully keyboard-driven — the
mouse is optional, and the first launch lands on the table itself.

Filter vs search: the filter box is client-side substring navigation.
`/search <tag> <text>` is the substrate's associative query — exact match
on the first content field, wildcards elsewhere, rendered in the detail
pane.

## 3a. The operator loop — create, plan, work, approve

A task on the board is not the end of the story. The full loop:

```
/add task write a one-paragraph summary of tuple spaces
/plan                     ← planner decomposes the highlighted task into 2-5 subtasks
                            (parent claimed exactly-once; ids like t0.1, t0.2 …)
/work 1                   ← a worker agent starts: claims a task, executes it,
                            and proposes the result — toast: "w0: task t0.1 → proposal p0"
/status                   ← tasks pending, proposals pending, worker progress
/approve                  ← the result lands on the board as knowledge (know0)
/stop                     ← halt workers (in-flight tasks finish first)
```

Every claim and proposal is attributed in the WAL, results land only
after your `/approve`, and a failed worker re-posts its claimed task — no
work is lost. If a chat reply or the detail pane gets covered by a worker
event, **`/chat`** (no argument) re-opens the transcript.

## 3b. Generic agents + a reviewer (multi-agent patterns)

Beyond the curated task workers, ABoard can run **generic agents** that
coordinate on *any* tuples (the substrate's raw tools), and a **reviewer**
that recommends verdicts on pending proposals:

```
/spawn alice analyst writing,editing   ← launch a generic LLM agent
/agents                                 ← live transcript, budget, skills
/review                                 ← reviewer writes a verdict per proposal
                                          (approve / reject / abstain + reason)
/proposals                              ← the inbox now shows each review line
/approve                                ← you still decide; the reviewer never applies
/stop alice                             ← halt the generic agent
```

A generic agent steps until you stop it (or it runs out of budget), writing
each turn to the board as an `agent_turn` tuple — so the transcript is part
of the space and survives restarts. The reviewer is a *recommendation* only:
the board stays human-authored (`/approve` / `/reject` remain the human's
decision, and an unparseable reviewer reply is a safe "abstain", never an
approval).

The **discussion sandbox** is two named agents taking turns through the same
board (no separate space):

```
/discuss alice bob the release plan    ← two agents alternate turns
/discuss                                ← re-open the interleaved transcript
/stop alice                             ← end it (either name stops the loop)
```

Each turn is an `agent_turn` tuple; the driver feeds each agent the other's
recent turns as context. There is no agent-to-agent channel — the tuple space
is the only medium.

## 4. Chat with the agent (human-driven, the default)

Chat needs an endpoint. Check what is installed, then pick a model:

```bash
ollama list                              # pick a tools-capable model, e.g. deepseek-v4-pro:cloud
.venv/bin/aboard --name alice --model deepseek-v4-pro:cloud
```

The agent starts in **human** mode: it reads the board and *proposes* —
nothing lands without your approval.

```
/chat remember that the substrate API returns JSON-safe tuples only
  → agent: "Proposed as knowledge p0 — approve it with /approve p0"
/proposals                                ← the inbox: p0: add knowledge (name=…, …)
/approve                                  ← applies the oldest; /approve p0 a specific one; /reject p0 discards
```

What just happened, mechanically:

- The agent staged `("proposal", p0, "add", "knowledge", {…})` — attributed
  to the agent in the WAL. The board was untouched until you approved.
- `/approve` took the proposal atomically (exactly one decision, even with
  two humans at the board), then inserted through the Board — attributed to
  *you*.
- A proposal gets exactly one decision: `/reject p0` discards without
  applying.
- Each turn toasts the cost ("2,829 tokens · 3 call(s)"). Crossing the
  token budget refuses the *next* send with `BudgetExceeded`.
- The conversation is stored on the board as `("chat", speaker, text)`
  tuples — restart and the chat resumes with the board.

## 5. Auto mode — when you want the agent to write

```
/mode                                    ← shows: chat mode: human
/mode auto                               ← live: the next turn writes directly
/chat add a task to try auto mode        ← no proposal, direct board_add
/mode human                              ← back to the gated mode
```

`/mode` is a runtime command, and the board remembers its own mode: the
choice is stored in the space as a `("config", "mode", "auto")` tuple, so a
restarted board comes back in the mode you left it in.

## 6. Stock — common knowledge, shared and read-only

A stock is just another ABoard board whose items you can cite and copy
into project boards. Curate one once, mount it anywhere:

```bash
.venv/bin/aboard --run-root ~/.aboard/stock     # curate: add knowledge/skill items, quit
.venv/bin/aboard                                 # your project board
```

Inside the project board:

```
/stock ~/.aboard/stock          ← mount (also the refresh); toast: "stock mounted: N item(s)"
/stock                          ← the view: path + per-tag counts
/chat what does the stock say about commit style?
  → agent cites the stock item; it cannot write it (read-only tools)
/import knowledge know0         ← you copy it in — re-id'd for this board, attributed to you
/stock off                      ← unmount
```

The mount is stored in the space too: resumed boards re-mount it
automatically. A broken stock warns and loads empty — it never blocks your
board.

## 7. Persistence — what happens on quit and restart

Every session is one tuple-space *run*; on launch ABoard resumes the
latest prior run with non-empty state (recovery = replay; you'll see a
"resumed N item(s) from run …" toast).

- **Quit is durable**: `ctrl+q` closes (fsyncs) the WAL.
- **Empty runs are skipped** — a launch-and-quit or a crash-before-flush
  never shadows the real board.
- **Corruption is loud**: a mid-file seq gap or unreadable WAL warns and
  starts fresh — never prefix-replayed, never deleted; prior runs stay on
  disk (see §9).
- `--fresh` starts an empty board for that session only; the next launch
  resumes as before.
- **`/reset`** is the factory reset (type it twice): clears items, chat,
  proposals *and* settings, stops workers, and archives every prior run to
  `<runs root>/archive/`. The reset is durable — resume skips empty runs, so
  without archiving the old board would simply come back on the next
  launch. It is also reversible: `mv archive/<run_id> .` restores that
  board, settings included. Recovery recipe:

  ```bash
  ls .tuplespace/runs/archive/        # pick the run you want back
  mv .tuplespace/runs/archive/20260101T000000Z-aaaaaaaa .tuplespace/runs/
  .venv/bin/aboard                      # that board resumes
  ```
- Data lives per project (cwd-relative): `./.tuplespace/runs/<run_id>/` by
  default, `--run-root X` puts runs directly under `X`. Runs are
  self-contained; delete old ones freely.

## 8. The audit trail — who did what

Everything is in the run's `ops.jsonl` (one JSON record per op, total
order). Every mutation and read carries an `actor`:

```bash
python3 - <<'EOF'
import json, sys
path = sorted(__import__('pathlib').Path('.tuplespace/runs').iterdir())[-1]
for line in open(path / "ops.jsonl"):
    r = json.loads(line)
    if r["kind"] == "mutation":
        print(r["seq"], r["op"], r["actor"], str(r["args"])[:70])
EOF
```

You will see e.g. `OUT assistant ['proposal', …]` → `INP human […]` →
`OUT human ['knowledge', …]`: the agent proposed, you decided, you applied.
The same log replays to the exact board state — that is the substrate's
contract, and ABoard inherits it.

## 9. Tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

147 tests, all deterministic — no network, no terminal (the LLM is faked
through a scripted client; TUI flows run under Textual's headless pilot; the
HTTP-board tests run a real uvicorn server on loopback).

## 10. Configuration

| Knob | Where | Default |
|---|---|---|
| Chat model | `--model` | `deepseek-v4.1-flash:cloud` (must exist on the endpoint) |
| Endpoint | `--base-url`, `--api-key` | Ollama `http://127.0.0.1:11434/v1` |
| Shared board | `--endpoint <url>` (join a `tuple_space.server`) | none (in-process board) |
| Your name (attribution, chat speaker) | `--name` | `human` |
| Runs root | `--run-root` or `TUPLE_SPACE_ROOT` | `./.tuplespace/` (per project dir) |
| Chat mode | `/mode [human\|auto]` (runtime, stored in the board) | `human` |
| Stock mount | `/stock [path\|off]` (runtime, stored in the board) | none |
| Token budget | `LLMConfig(token_budget=…)` | 100,000 |

## 11. Troubleshooting

- **`chat failed: LLM endpoint unreachable`** — Ollama isn't running, or
  the base URL is wrong. Check `curl http://127.0.0.1:11434/v1/models`.
- **Chat errors with the default model** — `deepseek-v4.1-flash:cloud` is
  the pinned default but may not be installed; pass `--model` with
  something from `ollama list` (tools-capable models only).
- **`chat agent … exceeded token budget`** — the budget is refused at the
  *next* send; restart aboard or raise `LLMConfig.token_budget`.
- **The board is empty after a restart** — you either ran with `--fresh`, or
  the last session made no changes and was skipped, or the last run was
  lost to a hard crash (documented crash window: entries buffered before a
  hard exit are lost). Older runs are never deleted — inspect
  `.tuplespace/runs/` (and `archive/` after a `/reset`) and resume manually
  with the substrate's `TupleSpace.recover(run_dir)`.
- **`could not resume the board (…) — starting fresh`** — the latest run's
  WAL has a mid-file gap or corrupt line. ABoard refuses prefix-replay
  (it could resurrect deleted items); the broken run is untouched on disk
  for manual recovery.
- **`tab` switched focus instead of tag** — inside an input, `tab` still
  moves focus by design in some contexts; use `shift+tab` for focus, and
  remember `enter` returns from the filter to the list.
- **`/import` says no stock mounted** — mount first with
  `/stock <runs root>`. The path is the runs root of the stock board —
  the same directory you'd pass as `--run-root` when curating it.
- **`/stock` mounts 0 items** — wrong path (it must be a runs root, i.e.
  contain run dirs), or the stock board is empty of task/knowledge/skill
  items. Non-curated tags (e.g. chat tuples) are filtered out on load.
- **`/reset` says "disabled on a remote board"** — you are on a shared
  HTTP board (`--endpoint`): the server owns the WAL, so a client-side
  reset would be a non-reversible clear with no archive. Restart the
  server with `--recover` (or `python -m tuple_space.server --recover
  <run_dir>`) to reset server-side.
- **`--endpoint` ignores `--run-root` / `--fresh`** — both are client-side
  persistence knobs and do not apply when joining a remote board; the
  server owns persistence. The server warns and proceeds.
- **`LogInitError`** — the runs root is unwritable; set `TUPLE_SPACE_ROOT`
  or pass `--run-root` pointing somewhere writable.
- **`error: externally-managed-environment`** — PEP 668; use the venv (§2).
- **`python3 -m venv` fails: "ensurepip is not available"** — no
  `python3-venv` package. Use the `--without-pip` + `get-pip.py` bootstrap
  in §2 (verified: works with no system pip, no sudo). `sudo apt install
  python3-venv python3-pip` is the alternative when you have sudo.
- **`python3 -m pip: No module named pip`** — the system Python has no pip
  at all; same fix (§2 bootstrap). Do not use `python3 -m pip --python …`
  here — there is nothing to delegate to.
- **`.venv/bin/aboard: cannot execute: required file not found`** — the
  venv was created elsewhere and copied here: every script in `.venv/bin/`
  carries a shebang with the *original* absolute path. Recreate the venv
  (§2), or fix the paths in place, e.g.
  `sed -i 's|/old/path|/current/path|g' .venv/bin/*` (also fix
  `VIRTUAL_ENV=` in `activate`).
- **Tests import `tests_support` fails** — run the suite from the repo root
  with `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`
  (discovery puts `tests/` on the path; `src` must be explicit).