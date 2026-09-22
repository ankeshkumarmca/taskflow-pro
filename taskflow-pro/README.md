# TaskFlow Pro — Backend

A dependency-aware (DAG) scheduling engine behind a Kanban task board, built for the
Contata Hackathon 2026 "TaskFlow Pro" problem statement.

This repository currently contains the **backend and DAG engine** — the part of the
system where correctness (cycle rejection, no-compounding propagation, rollback)
actually lives, built and tested first per the brief's own guidance. A React Kanban
frontend consuming this API is the natural next layer.

## What's here

```
app/
  database.py        SQLAlchemy engine/session setup (SQLite)
  models.py           Task and TaskDependency ORM models
  schemas.py           Pydantic request/response models
  dag_engine.py        Core DAG logic: cycle detection, ready/blocked, propagation
  llm_suggestions.py    AI-augmented dependency suggestion (grounded, structured output)
  main.py               FastAPI app wiring it all together
tests/
  test_dag_engine.py    Unit tests for cycle detection, no-compounding propagation, rollback
seed.py                  Loads seed-data.json into the database
seed-data.json            10 seeded tasks with realistic dependencies (see below)
requirements.txt
```

## Setup

```bash
pip install -r requirements.txt
python seed.py              # loads seed-data.json into taskflow.db (SQLite)
uvicorn app.main:app --reload
```

API docs (Swagger UI) will be at `http://localhost:8000/docs`.

To enable AI dependency suggestions:

```bash
export ANTHROPIC_API_KEY=your-key-here
```

Without a key set, `/suggest-dependencies` returns an empty list rather than
failing or faking output — see Assumptions & Limitations below.

## Running tests

```bash
pytest tests/ -v
```

14 tests cover the three hardest requirements in the brief directly:
- **No Cycles**: direct, indirect (3-node), and self-dependency cycles are all rejected
  without mutating the existing graph.
- **No Compounding**: a diamond-shaped convergence (A→B→D, A→C→D) is asserted to move D
  by the upstream delta exactly once, not once per path.
- **Rollback**: moving a Done task back to In Progress re-blocks dependents whose
  prerequisite is no longer satisfied.

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /tasks` | List all tasks with computed `dependency_state` (ready/blocked/done) |
| `POST /tasks` | Create a task |
| `PATCH /tasks/{id}` | Update a task; if `end_date`/duration changes, propagates to descendants and returns every task that moved |
| `POST /dependencies` | Add a dependency edge; rejected with 400 if it would create a cycle |
| `DELETE /dependencies` | Remove a dependency edge |
| `POST /suggest-dependencies` | AI-suggested prerequisites for a task (see below) |

## AI/LLM usage

`/suggest-dependencies` sends the target task and the rest of the board's task
titles/descriptions to Claude, asking for candidate prerequisite IDs with a
confidence level and a one-sentence rationale, returned as **structured JSON only**.

Grounding and safety measures:
- The model is only shown tasks that already exist on the board, and is instructed to
  reference only their IDs — it cannot invent tasks.
- Every suggestion is re-validated against `dag_engine.would_create_cycle` before being
  returned, so a suggestion that would break the DAG is never shown to the user.
- Suggestions are never written to `task_dependencies` directly. The frontend is
  expected to show them as a distinct "Suggested" state that the user must explicitly
  accept; acceptance goes through the same `POST /dependencies` cycle-checked endpoint
  as a manually-added edge.
- If no `ANTHROPIC_API_KEY` is configured, or the model's output isn't valid JSON, the
  endpoint returns an empty list rather than guessing or erroring — it fails closed.

## Key Assumptions and Limitations

**Assumptions:**
- Single-board scope: dependencies are assumed to exist only within one board/project;
  cross-board dependencies are out of scope for this sprint.
- Dates are day-granularity (no timezone or hour-level scheduling).
- Auth is out of scope for this backend slice; endpoints are currently unauthenticated.
  A production version would add per-workspace auth before exposing these endpoints.
- A completed (`done`) task's own dates are never rescheduled by propagation, even if
  its prerequisites change — completed work is treated as historical fact, not something
  that silently moves.

**Limitations:**
- **Rollback does not un-complete downstream Done tasks.** If task A regresses from
  Done to In Progress, a downstream task B that is *not yet Done* correctly flips to
  Blocked. But if B is *already marked Done*, it is left as Done rather than being
  silently reverted — un-completing someone's explicitly-marked-finished work without
  their action felt like the wrong default. This is a deliberate product decision, not
  an oversight, but it does mean the board can (rarely) show a Done task whose
  prerequisite has since regressed; a real product would likely want to surface a
  warning badge for this case rather than silently allowing it or silently reverting it.
- No real-time collaboration/websocket layer — concurrent edits from multiple users are
  not conflict-resolved beyond whatever the database's default transaction behavior gives.
- The frontend (Kanban board, drag-and-drop, dependency graph visualization, Critical
  Path view) is not yet implemented in this slice.
- AI suggestions are only for *prerequisites of one task at a time* — there's no
  "suggest dependencies for the whole board" batch mode.

## AI-Tool Declaration

This solution's backend code, tests, README, and synopsis were developed with
assistance from Claude (Anthropic). Claude was used to draft the DAG algorithms
(cycle detection, no-compounding propagation), generate and iterate on unit tests,
and scaffold the FastAPI application. All logic was reviewed and verified by running
the test suite and manual end-to-end API checks before inclusion. The
AI-dependency-suggestion *feature itself* (Claude called at runtime via
`/suggest-dependencies`) is a separate, product-facing use of AI described above.
