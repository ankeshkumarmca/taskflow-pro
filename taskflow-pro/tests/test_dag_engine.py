import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.dag_engine import (
    TaskState,
    would_create_cycle,
    validate_new_edge,
    CycleError,
    compute_ready_blocked,
    propagate_schedule_change,
)


def make_task(id, status="backlog", start="2026-01-01", end="2026-01-05", duration=4):
    return TaskState(
        id=id,
        status_column=status,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        duration_days=duration,
    )


# ---------- Cycle detection ----------

def test_no_cycle_in_simple_chain():
    edges = [(2, 1)]  # task 2 depends on task 1
    assert would_create_cycle(3, 2, edges) is False


def test_direct_cycle_rejected():
    edges = [(2, 1)]  # 1 -> 2
    # Adding "1 depends on 2" would close 1 -> 2 -> 1
    assert would_create_cycle(1, 2, edges) is True


def test_indirect_three_node_cycle_rejected():
    # A -> B -> C  (edges stored as (task, depends_on))
    edges = [(2, 1), (3, 2)]  # 2 depends on 1, 3 depends on 2  => 1->2->3
    # Adding "1 depends on 3" would close 1 -> 2 -> 3 -> 1
    assert would_create_cycle(1, 3, edges) is True


def test_self_dependency_rejected():
    assert would_create_cycle(5, 5, []) is True


def test_validate_new_edge_raises_and_does_not_mutate():
    edges = [(2, 1), (3, 2)]
    original = list(edges)
    with pytest.raises(CycleError):
        validate_new_edge(1, 3, edges)
    assert edges == original  # existing graph must remain unchanged


def test_valid_edge_does_not_raise():
    edges = [(2, 1)]
    validate_new_edge(3, 2, edges)  # should not raise


# ---------- Blocked / Ready ----------

def test_task_with_no_deps_is_ready():
    tasks = {1: make_task(1, status="backlog")}
    result = compute_ready_blocked(tasks, [])
    assert result[1] == "ready"


def test_task_blocked_until_all_prereqs_done():
    tasks = {
        1: make_task(1, status="in_progress"),
        2: make_task(2, status="backlog"),
    }
    edges = [(2, 1)]  # 2 depends on 1
    result = compute_ready_blocked(tasks, edges)
    assert result[2] == "blocked"

    tasks[1].status_column = "done"
    result = compute_ready_blocked(tasks, edges)
    assert result[2] == "ready"


def test_task_blocked_if_any_of_multiple_prereqs_unfinished():
    tasks = {
        1: make_task(1, status="done"),
        2: make_task(2, status="in_progress"),
        3: make_task(3, status="backlog"),
    }
    edges = [(3, 1), (3, 2)]  # 3 depends on both 1 and 2
    result = compute_ready_blocked(tasks, edges)
    assert result[3] == "blocked"


# ---------- No-compounding propagation (the diamond case from the brief) ----------

def test_convergent_paths_do_not_compound_delay():
    """
    A -> B -> D
    A -> C -> D
    A shifts +3 days. D must shift +3 days once, not +6.
    """
    tasks = {
        # A: already updated by the caller to its NEW end date (01-08, was 01-05, i.e. +3d)
        1: make_task(1, status="done", start="2026-01-01", end="2026-01-08", duration=7),
        # B, C, D still hold their STALE dates from before A's change -- propagation must move them
        2: make_task(2, status="in_progress", start="2026-01-05", end="2026-01-09", duration=4),  # B
        3: make_task(3, status="in_progress", start="2026-01-05", end="2026-01-09", duration=4),  # C
        4: make_task(4, status="backlog", start="2026-01-09", end="2026-01-13", duration=4),      # D
    }
    edges = [
        (2, 1),  # B depends on A
        (3, 1),  # C depends on A
        (4, 2),  # D depends on B
        (4, 3),  # D depends on C
    ]

    moved = propagate_schedule_change(1, tasks, edges)

    assert tasks[2].start_date == date(2026, 1, 8)
    assert tasks[3].start_date == date(2026, 1, 8)
    # D must start after BOTH B and C finish -> max(B.end, C.end), computed once
    assert tasks[4].start_date == date(2026, 1, 12)
    assert tasks[4].end_date == date(2026, 1, 16)
    assert 4 in moved
    # Critically: D only moved once. If compounding were happening, D's
    # start would have drifted past 1-12 (e.g. to 1-15 or 1-19).


def test_propagation_skips_done_tasks():
    tasks = {
        1: make_task(1, status="done", start="2026-01-01", end="2026-01-08", duration=7),
        2: make_task(2, status="done", start="2026-01-01", end="2026-01-04", duration=3),  # already done, must not move
    }
    edges = [(2, 1)]
    original_start = tasks[2].start_date
    propagate_schedule_change(1, tasks, edges)
    assert tasks[2].start_date == original_start


# ---------- Rollback ----------

def test_rollback_reblocks_downstream_tasks():
    """
    Moving a completed upstream task back to in_progress must cause
    dependents whose prerequisite is no longer done to become Blocked again.
    """
    tasks = {
        1: make_task(1, status="done"),
        2: make_task(2, status="backlog"),
    }
    edges = [(2, 1)]

    assert compute_ready_blocked(tasks, edges)[2] == "ready"

    tasks[1].status_column = "in_progress"  # regression
    assert compute_ready_blocked(tasks, edges)[2] == "blocked"


def test_rollback_does_not_silently_revert_already_done_dependents():
    """
    Design decision (documented in README): if a task's prerequisite
    regresses, only NOT-YET-DONE dependents flip to Blocked. A dependent
    that is already marked Done keeps reporting 'done' -- TaskFlow Pro
    never silently un-completes work a user explicitly marked finished.
    """
    tasks = {
        1: make_task(1, status="done"),
        2: make_task(2, status="done"),      # completed, downstream of 1
        3: make_task(3, status="backlog"),   # depends on 2, not started yet
    }
    edges = [(2, 1), (3, 2)]  # 1 -> 2 -> 3
    assert compute_ready_blocked(tasks, edges)[3] == "ready"  # 2 is done

    tasks[1].status_column = "in_progress"  # regression on 1
    result = compute_ready_blocked(tasks, edges)
    assert result[2] == "done"    # 2 is not silently reverted
    assert result[3] == "ready"   # 3's direct prereq (2) is still 'done'


def test_rollback_reblocks_a_not_yet_done_dependent_two_hops_away():
    """
    Here the intermediate task is NOT done, so the chain gates correctly:
    3 stays Blocked both before and after, because its direct prerequisite
    (2) was never Done in the first place.
    """
    tasks = {
        1: make_task(1, status="done"),
        2: make_task(2, status="in_progress"),
        3: make_task(3, status="backlog"),
    }
    edges = [(2, 1), (3, 2)]
    assert compute_ready_blocked(tasks, edges)[3] == "blocked"

    tasks[1].status_column = "in_progress"
    assert compute_ready_blocked(tasks, edges)[3] == "blocked"
