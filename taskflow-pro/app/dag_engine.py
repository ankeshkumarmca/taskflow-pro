"""
Core dependency-graph logic for TaskFlow Pro.

Deliberately kept storage-agnostic: everything here operates on plain
in-memory structures (dicts / tuples), not SQLAlchemy sessions. This is
what the brief calls out explicitly -- "test the dependency and scheduling
logic separately before wiring it into the frontend" -- and it's why these
functions are covered directly by tests/test_dag_engine.py without needing
a database at all.
"""

from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable


DONE = "done"


@dataclass
class TaskState:
    id: int
    status_column: str
    start_date: date
    end_date: date
    duration_days: int


Edge = tuple[int, int]  # (task_id, depends_on_task_id) -- task_id depends on depends_on_task_id


class CycleError(ValueError):
    """Raised when a candidate edge would create a circular dependency."""


def _forward_adjacency(edges: Iterable[Edge]) -> dict[int, list[int]]:
    """Build prerequisite -> dependents adjacency (i.e. graph edges point
    from the thing that must finish first to the thing waiting on it)."""
    adj: dict[int, list[int]] = defaultdict(list)
    for task_id, depends_on_id in edges:
        adj[depends_on_id].append(task_id)
    return adj


def would_create_cycle(new_task_id: int, new_depends_on_id: int, edges: Iterable[Edge]) -> bool:
    """
    Check whether adding the edge (new_task_id depends_on new_depends_on_id)
    would create a cycle, WITHOUT mutating any state.

    Adding prerequisite edge (new_depends_on_id -> new_task_id) creates a
    cycle exactly when new_task_id can already reach new_depends_on_id via
    existing edges -- i.e. new_depends_on_id is already (transitively)
    downstream of new_task_id, so making it upstream too would close a loop.
    """
    if new_task_id == new_depends_on_id:
        return True  # a task cannot depend on itself

    adjacency = _forward_adjacency(edges)
    visited = set()
    stack = [new_task_id]
    while stack:
        node = stack.pop()
        if node == new_depends_on_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adjacency[node])
    return False


def validate_new_edge(new_task_id: int, new_depends_on_id: int, edges: list[Edge]) -> None:
    """Raise CycleError if this edge is invalid. Call before persisting."""
    if would_create_cycle(new_task_id, new_depends_on_id, edges):
        raise CycleError(
            f"Adding dependency (task {new_task_id} depends on {new_depends_on_id}) "
            f"would create a circular dependency. Edge rejected; existing graph unchanged."
        )


def compute_ready_blocked(tasks: dict[int, TaskState], edges: Iterable[Edge]) -> dict[int, str]:
    """
    Return {task_id: 'ready' | 'blocked' | 'done'} for every task.

    A task is Ready if every prerequisite (depends_on edge target) has
    status_column == 'done'. A task that is itself Done is reported as
    'done' regardless of its own dependencies (it already happened).
    This is re-derived from scratch every time it's called -- there's no
    stored "is_blocked" flag to fall out of sync, which is what makes
    rollback (an upstream task moving back to in_progress) work for free:
    recompute this after any status change and downstream tasks naturally
    flip back to Blocked if their prerequisite is no longer Done.
    """
    prereqs: dict[int, list[int]] = defaultdict(list)
    for task_id, depends_on_id in edges:
        prereqs[task_id].append(depends_on_id)

    result: dict[int, str] = {}
    for task_id, task in tasks.items():
        if task.status_column == DONE:
            result[task_id] = "done"
            continue
        prereq_ids = prereqs.get(task_id, [])
        is_ready = all(tasks[p].status_column == DONE for p in prereq_ids if p in tasks)
        result[task_id] = "ready" if is_ready else "blocked"
    return result


def _topo_order_of_descendants(changed_task_id: int, edges: Iterable[Edge]) -> list[int]:
    """
    Kahn's algorithm restricted to the changed task plus everything
    reachable downstream of it (its descendants). Returning only the
    affected subgraph, rather than sorting the whole board, keeps
    propagation cost proportional to what actually changed.
    """
    forward_adj = _forward_adjacency(edges)
    dependents_of: dict[int, list[int]] = defaultdict(list)
    for task_id, depends_on_id in edges:
        dependents_of[task_id].append(depends_on_id)

    descendants = set()
    queue = deque([changed_task_id])
    while queue:
        node = queue.popleft()
        for child in forward_adj[node]:
            if child not in descendants:
                descendants.add(child)
                queue.append(child)

    relevant = descendants | {changed_task_id}
    in_degree = {n: 0 for n in relevant}
    for n in relevant:
        for dep in dependents_of[n]:
            if dep in relevant:
                in_degree[n] += 1

    queue = deque([n for n in relevant if in_degree[n] == 0])
    order: list[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in forward_adj[node]:
            if child in relevant:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
    return order


def propagate_schedule_change(
    changed_task_id: int,
    tasks: dict[int, TaskState],
    edges: list[Edge],
) -> dict[int, TaskState]:
    """
    Recompute dates for every descendant of changed_task_id, in topological
    order, and return the set of tasks that were actually moved
    (changed_task_id itself is assumed already updated by the caller and is
    not included in the result).

    THE NO-COMPOUNDING GUARANTEE: each descendant's new start date is
    max(end_date of each of its prerequisites), computed once from the
    prerequisites' *absolute* end dates -- not by summing a delta along
    every incoming path. When two paths reconverge (A->B->D, A->C->D),
    D is visited once, after both B and C have already been recomputed,
    and takes max(B.end_date, C.end_date). This is what keeps a task that
    is fed by two paths from the same +3-day upstream change from moving
    +6 days instead of +3.

    Tasks already marked 'done' are left untouched -- completed work
    doesn't get silently rescheduled (see README assumptions).
    """
    dependents_of: dict[int, list[int]] = defaultdict(list)
    for task_id, depends_on_id in edges:
        dependents_of[task_id].append(depends_on_id)

    order = _topo_order_of_descendants(changed_task_id, edges)
    moved: dict[int, TaskState] = {}

    for task_id in order:
        if task_id == changed_task_id:
            continue
        task = tasks[task_id]
        if task.status_column == DONE:
            continue

        prereq_ids = dependents_of.get(task_id, [])
        if not prereq_ids:
            continue  # no prerequisites -> nothing upstream to react to

        new_start = max(tasks[p].end_date for p in prereq_ids)
        new_end = new_start + timedelta(days=task.duration_days)

        if new_start != task.start_date or new_end != task.end_date:
            task.start_date = new_start
            task.end_date = new_end
            moved[task_id] = task

    return moved
