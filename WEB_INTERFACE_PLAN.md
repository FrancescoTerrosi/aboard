# ABoard — TUI / functionality split plan

Goal: make every ABoard capability available to more than one interface —
first the existing Textual TUI, later a web UI — without forking the logic.

Status: **draft for review** — nothing here is implemented yet.

---

## 1. Context

Today `src/aboard/tui.py` is a **fat controller + view**. It contains:

- Textual UI wiring (`compose`, `on_*`, `action_*`, widgets, `notify`,
  `run_worker`).
- Command semantics (`_run_command`, `_add`, `_chat`, `_architect_cmd`,
  `_work_cmd`, `_review_cmd`, `_reset_cmd`, …).
- View rendering (`_render_detail`, `_render_chat`, `_show_status`,
  `_tuples_cmd`, `_files_cmd`, …).
- Live state (active tag, filter query, proposals cursor, active chat and
  architect agents, running workers/agents/discussions).

The underlying domain modules are already UI-free:

- `board.py`, `models.py`, `proposals.py`, `config.py`, `session.py`
- `chat.py`, `workers.py`, `llm_agents.py`, `architect.py`, `reviewer.py`,
  `discussion.py`, `http_space.py`

A web UI can call those today, but it would have to re-implement the command
semantics, state, and orchestration currently embedded in `BoardApp`. This
plan extracts that shared layer.

---

## 2. Design principles

1. **One core, many shells.** A single `BoardService` owns all command
   semantics, live state, and async orchestration. TUI and web become thin
   adapters.
2. **Core has no Textual and no HTTP.** No `App`, no `notify`, no widgets,
   no FastAPI/httpx in the core.
3. **Adapters receive domain events.** The core never renders. It emits
   structured state/data; each adapter maps that to widgets, JSON, or SSE.
4. **Structured data at the core, rendering in the shells.** Core read
   methods return snapshots/dicts. The TUI renders them as markdown; the web
   shell renders them as JSON/HTML.
5. **The web shell is local-first.** The browser UI is served by the same
   process and host that owns the tuple space. No auth, no CORS, no
   multi-tenant concerns in the first slice.
6. **Keep current TUI behavior and tests green.** The split must be
   behavioral-neutral for the existing TUI first, then web adds a new shell.

---

## 3. Target architecture

```
                    ┌───────────────────────────────┐
                    │           BoardService        │
                    │  state + command semantics    │
                    │  + async orchestration        │
                    └───────────────┬───────────────┘
                                    │ emits
                     ┌──────────────▼───────────────┐
                     │          Observer /          │
                     │        CommandResult         │
                     └───────────────┬───────────────┘
              ┌──────────────────────┴──────────────────────┐
              │                                             │
   ┌──────────▼──────────┐                       ┌──────────▼──────────┐
   │   Textual adapter    │                       │    HTTP backend     │
   │   (tui.py, thin)     │                       │   (web.py + API)    │
   └─────────────────────┘                       └─────────┬──────────┘
                                                           │ JSON + SSE
                                                ┌──────────▼──────────┐
                                                │   JS browser shell   │
                                                │ (vanilla JS, no build)│
                                                └─────────────────────┘
```

Proposed files:

```
src/aboard/
  core.py        # BoardService + result/event types (single backend core)
  tui.py         # Textual shell only (owns its markdown renderers)
  web.py         # FastAPI backend: JSON API + SSE + static files
  web/
    index.html   # browser shell markup
    app.js       # browser shell logic (vanilla JS, no build step)
    style.css    # browser shell styling
```

No `rendering.py` and no frontend build step. The core returns structured
snapshots; each shell decides how to display them. The JS shell is a plain
static app served by the same FastAPI process.

---

## 4. Current command inventory

These are the commands the shared core must cover:

| Command | Current TUI handler | Effect |
|---|---|---|
| bare text | `_chat` | chat turn |
| `/add <tag> …` | `_add` | insert curated tuple |
| `/del` | `_delete` | remove highlighted tuple |
| `/search <tag> <text>` | `_search` | exact/wildcard search view |
| `/clear [tag]` | `_clear` | remove all tuples of a tag |
| `/proposals` | `_show_proposals` | proposal inbox view |
| `/approve [pN]` | `_decide` | apply proposal |
| `/reject [pN]` | `_decide` | reject proposal |
| `/import <tag> <id>` | `_import_item` | copy from stock |
| `/mode [human\|auto]` | `_mode_cmd` | show/switch chat mode |
| `/stock [path\|off]` | `_stock_cmd` | mount/show/unmount stock |
| `/plan [id]` | `_plan_cmd` | async planner |
| `/work [n]` | `_work_cmd` | launch workers |
| `/agent …` | `_agent_cmd` | named workers |
| `/spawn …` | `_spawn_cmd` | generic agent |
| `/architect …` | `_architect_cmd` | conversational file agent |
| `/agents` | `_show_llm_agents` | generic agents view |
| `/tuples [tag]` | `_tuples_cmd` | raw tuple inspector |
| `/files` | `_files_cmd` | artifacts view |
| `/review [pN]` | `_review_cmd` | async reviewer |
| `/discuss …` | `_discuss_cmd` | async discussion |
| `/tag <tid> <skill>` | `_task_skill_cmd` | task skill routing |
| `/dep <tid> <deps>` | `_dep_cmd` | task dependencies |
| `/stop [name]` | `_stop_workers` | stop workers/agents |
| `/status` | `_show_status` | board at a glance |
| `/reset` | `_reset_cmd` | factory reset (twice) |
| `/help` | `_render_help` | command reference |

---

## 5. Proposed core API

### 5.1 `BoardService`

```python
class BoardService:
    def __init__(
        self,
        space,
        *,
        human: str = "human",
        chat_mode: str = "human",
        stock: list[tuple] | None = None,
        llm: LLMConfig | None = None,
        chat_agent: ChatAgent | None = None,
        architect_agent: ArchitectAgent | None = None,
    ): ...

    # ── synchronous command dispatch ─────────────────────────────────
    def handle(self, line: str) -> CommandResult: ...

    # ── read model (for shells) ─────────────────────────────────────
    def list_items(self, tag: str, query: str = "") -> list[tuple]
    def item_detail(self, tag: str, tid: str) -> tuple | None
    def proposals(self) -> list[tuple]
    def status(self) -> StatusSnapshot
    def chat_transcript(self) -> list[tuple]
    def architect_transcript(self, name: str | None = None) -> list[tuple]
    def agents_view(self) -> AgentsSnapshot
    def tuples_view(self, tag: str | None) -> TuplesSnapshot
    def files_view(self) -> FilesSnapshot

    # ── async operations (scheduled by the shell) ───────────────────
    async def do_chat(self, text: str, observer: Observer) -> None
    async def do_architect(self, text: str, observer: Observer) -> None
    async def do_plan(self, tid: str, observer: Observer) -> None
    async def do_review(self, pid: str | None, observer: Observer) -> None
    async def do_work(self, n: int, observer: Observer) -> None
    async def do_discuss(self, a: str, b: str, topic: str, observer: Observer) -> None
    async def do_spawn(self, name: str, role: str, skills: list[str], observer: Observer) -> None
    async def do_architect_start(...) -> None
```

### 5.2 State ownership

Move from `BoardApp` into `BoardService`:

- `board`, `human`, `chat_mode`, `stock`, `llm`
- `_chat_agent`, `_architect_agent`, `_architect_name`
- `_task_workers`, `_llm_agents`, `_discussions`, `_worker_seq`
- `_reset_armed`

Keep in the shell (view state only):

- `_tag`, `_query`, `_tuples`, `_current_tid`
- `_detail_view`, `_last_detail_markdown`
- `_proposals`, `_proposal_index`

> Open question: should the active tag live in the core? A web UI has a URL
> that can own the active tag, so I propose keeping it in the shell. The core
> reads whatever `tag` the shell passes to `list_items`.

### 5.3 Events and results

Two mechanisms are needed:

- **`CommandResult`** for immediate, synchronous commands.
- **`Observer`** for long-running async commands that emit progress.

```python
@dataclass
class CommandResult:
    messages: list[Notice]            # toast / flash equivalents
    view: ViewState | None            # structured view update, if any
    table: TableState | None          # active-tag rows + subtitle
    job: Job | None                   # background work, if any

@dataclass
class Job:
    coro: Coroutine
    group: str                        # for cancellation/exclusivity
    exclusive: bool = False

@dataclass
class ViewState:
    name: str                         # "chat", "proposals", "status", …
    data: object                      # structured snapshot for the shell
    scroll_end: bool = False

@dataclass
class TableState:
    tag: str
    rows: list[tuple]                 # full curated tuples, shell formats
    subtitle: str
```

```python
class Observer(Protocol):
    def on_message(self, message: str, *, level: str = "info") -> None: ...
    def on_view(self, state: ViewState) -> None: ...
    def on_table(self, state: TableState) -> None: ...
    def on_state(self) -> None: ...     # generic "reload current view"
```

The TUI adapter implements `Observer` by converting each `ViewState` to the
matching `_render_*` markdown and updating widgets. The web adapter converts
the same states to JSON or SSE events.

### 5.4 Command dispatch

`handle()` mirrors today's `_run_command` but returns a `CommandResult`
instead of touching widgets. Bare text routes to a `Job` for `do_chat`.

---

## 6. Read model / rendering split

The core returns **structured snapshots**, not markdown. The current TUI
renderers stay in `tui.py` and consume those snapshots:

```python
# core read methods return these shapes
def list_items(tag, query="") -> list[tuple]
def item_detail(tag, tid) -> tuple | None
def proposals() -> list[tuple]
def status() -> StatusSnapshot
# …

# tui.py keeps the markdown builders, now fed by snapshots
_render_detail(tup) -> str
_render_chat(turns, *, human, pending=None, thinking=False) -> str
_render_architect(...) -> str
_render_proposals(...) -> str
_render_status(...) -> str
# …
```

The web shell skips markdown and serves the snapshots as JSON (or renders
them client-side). The TUI keeps its current output byte-for-byte.

---

## 7. TUI adapter (post-split)

`tui.py` shrinks to:

- `compose`, bindings, event handlers, widget focus.
- `_apply_command_result(result)`:
  - `messages → self.notify`
  - `view → self._detail_update`
  - `table → self._reload`
  - `job → self.run_worker(..., exclusive=job.exclusive, group=job.group)`
- `_TuiObserver(Observer)`:
  - `on_message → self.notify`
  - `on_view → self._detail_update`
  - `on_table → self._reload`
  - `on_state → self._reload`

No command semantics remain in `tui.py`.

---

## 8. Web backend + JS browser shell

The browser UI runs on the **same host** as the tuple space. Two pieces:

- **Backend** — `web.py`, a FastAPI app that owns one `BoardService` and
  exposes the JSON API, SSE, and static files.
- **Frontend** — `web/index.html` + `web/app.js` + `web/style.css`, a plain
  vanilla-JS single page with no build step and no npm dependency.

The backend supports the same two board modes as the TUI:

- **In-process** (default): `web.py` opens/recovers a `TupleSpace` directly.
- **Local endpoint**: `web.py --endpoint http://127.0.0.1:8765` joins an
  existing local `tuple_space.server` through `HttpSpace`.

Endpoints:

```
GET  /                        # static browser app
GET  /items/{tag}?query=      # curated rows
GET  /items/{tag}/{id}        # one tuple
GET  /proposals
GET  /status
GET  /chat
GET  /architect/{name}
GET  /tuples?tag=
GET  /files
POST /command                 # {"line": "/add task …"} → CommandResult JSON
GET  /jobs/{id}               # job status
GET  /jobs/{id}/events        # SSE progress via Observer
```

### JS shell responsibilities

The browser shell is a thin client of the backend:

- fetch read endpoints and render lists/detail/status in the DOM
- submit commands via `POST /command`
- open an SSE stream for a job and update progress/chat/architect turns live
- keep view state in the URL (`#/tag/task?q=…`) where useful

The JS shell never talks to the tuple space directly; it only calls
`web.py`. No auth, no CORS, no build tooling.

Simplifications from same-host, single-user access:

- **No auth, no CORS, no multi-tenancy.**
- **In-memory job state.** A small `JobRunner` inside `web.py` maps job ids
  to `asyncio.Task`s and SSE queues; nothing is persisted beyond the board.
- **One observer per SSE client.** Progress from `BoardService` async
  methods flows straight to the browser.

Out of scope for the first web slice:

- remote access hardening / TLS / auth
- multiple concurrent human users
- durable web job state across server restarts
- websocket fan-out (SSE is enough for one local browser)
- frontend framework/build step (React/Vue/npm are deliberately avoided)

---

## 9. Migration sequence

Keep the TUI green at every step.

1. **Add `core.py` with types only** — `Notice`, `ViewState`, `TableState`,
   `CommandResult`, `Job`, `Observer`. No behavior yet.
2. **Add `BoardService` state and structured read methods** — mirror
   `BoardApp` state; `list_items`, `status`, `proposals`, etc. `BoardApp`
   delegates reads to it; TUI renderers stay put and consume snapshots.
3. **Port synchronous commands** one by one:
   - `_add`, `_delete`, `_clear`, `_search`, `_import_item`
   - `_mode_cmd`, `_stock_cmd`, `_tag`, `_dep`
   - `_decide`, `_stop_workers`, `_reset_cmd`
   - each becomes a `BoardService` method returning `CommandResult`.
4. **Port async commands** one by one:
   - `do_chat`, `do_architect`, `do_plan`, `do_review`
   - `do_work`, `do_spawn`, `do_discuss`
   - each takes an `Observer` and emits progress; TUI schedules via `run_worker`.
5. **Shrink `BoardApp`** to shell + adapter; delete moved methods.
6. **Add `web.py` backend + `web/` JS shell** using the same
   `BoardService`; serve the static frontend and the JSON/SSE endpoints.
7. **Add web tests** with a scripted client (same seam as the TUI tests).

---

## 10. Test strategy

- Existing 171 tests must stay green after each step.
- Add unit tests for `BoardService` directly:
  - `handle("/add task …")` returns a `CommandResult` with a table update.
  - `handle("/approve p0")` mutates the board and returns a message.
  - async methods emit `Observer` events in order.
- Keep TUI tests as integration tests for the Textual shell.
- Add web shell tests later with FastAPI `TestClient` or `httpx`.

---

## 11. Risks and decisions

### Risks

- **Async orchestration is the hardest seam.** Textual's `run_worker` does
  cancellation/exclusivity for us today. The core must not silently depend
  on it. Mitigation: `Job` objects + adapter-owned scheduling.
- **State split can drift.** If the active tag/query/proposals cursor moves
  to the core, TUI and web could disagree. Mitigation: keep view state in the
  shell, keep durable/live domain state in the core.
- **Markdown vs JSON.** Keeping renderers in the TUI shell preserves current
  output, but the web shell may want JSON. Mitigation: core exposes structured
  snapshots; each shell renders its own representation.
- **Regression surface.** This touches the busiest file in the project.
  Mitigation: stepwise migration, green after each step.

### Decisions (draft — local-browser constraint applied)

1. **`core.py` as a single module.** Promote to `aboard/core/` only if it
   grows beyond one coherent file.
2. **Shell-owned rendering.** Core returns structured snapshots; TUI keeps
   its markdown renderers; web renders JSON/HTML. No shared `rendering.py`.
3. **Active tag/filter stays shell-owned.** Core read methods take them as
   parameters.
4. **FastAPI + SSE for web**, under a new `[web]` extra. Same-host access
   removes auth/CORS/multi-tenancy, but FastAPI still wins for serving
   static files, JSON, and SSE in one process.
5. **No core `JobRunner`.** Core returns `Job` objects; TUI uses
   `run_worker`; `web.py` has a small in-memory job/SSE manager.
6. **Structured JSON at the core; markdown only in TUI.** Read methods
   return snapshots; TUI renderers convert them.
7. **`/files` and `/tuples` stay bounded read-only now.** Add pagination
   when the web shell actually needs it.
8. **The browser shell is vanilla JS, no build step.** `web/index.html` +
   `web/app.js` + `web/style.css` served statically by FastAPI. No npm, no
   bundler, no framework until a real need appears.

---

## 12. Out of scope for now

- Changing the tuple-space substrate.
- Changing command semantics (same commands, same effects).
- Replacing the TUI.
- Authentication, multi-user web sessions, persistence of web job state
  across process restarts (the board already persists; job state does not).
