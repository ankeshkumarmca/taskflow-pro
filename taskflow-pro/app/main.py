from datetime import date

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from . import models, schemas
from .database import Base, engine, get_db
from .dag_engine import (
    TaskState,
    validate_new_edge,
    CycleError,
    compute_ready_blocked,
    propagate_schedule_change,
)
from .llm_suggestions import suggest_dependencies

Base.metadata.create_all(bind=engine)

app = FastAPI(title="TaskFlow Pro API")


# ---------- helpers ----------

def _load_graph(db: Session) -> tuple[dict[int, models.Task], list[tuple[int, int]]]:
    tasks = {t.id: t for t in db.query(models.Task).all()}
    edges = [
        (d.task_id, d.depends_on_task_id) for d in db.query(models.TaskDependency).all()
    ]
    return tasks, edges


def _to_task_state(t: models.Task) -> TaskState:
    return TaskState(
        id=t.id,
        status_column=t.status_column,
        start_date=t.start_date,
        end_date=t.end_date,
        duration_days=t.duration_days,
    )


def _serialize(t: models.Task, dependency_state: str, depends_on: list[int]) -> schemas.TaskOut:
    return schemas.TaskOut(
        id=t.id,
        title=t.title,
        description=t.description,
        status_column=t.status_column,
        start_date=t.start_date,
        end_date=t.end_date,
        duration_days=t.duration_days,
        board_position=t.board_position,
        dependency_state=dependency_state,
        depends_on=depends_on,
    )


# ---------- task endpoints ----------

@app.get("/tasks", response_model=list[schemas.TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    tasks, edges = _load_graph(db)
    states = {tid: _to_task_state(t) for tid, t in tasks.items()}
    ready_blocked = compute_ready_blocked(states, edges)
    prereqs: dict[int, list[int]] = {}
    for task_id, depends_on_id in edges:
        prereqs.setdefault(task_id, []).append(depends_on_id)
    return [
        _serialize(t, ready_blocked[tid], prereqs.get(tid, [])) for tid, t in tasks.items()
    ]


@app.post("/tasks", response_model=schemas.TaskOut)
def create_task(payload: schemas.TaskCreate, db: Session = Depends(get_db)):
    task = models.Task(**payload.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return _serialize(task, "ready", [])


@app.patch("/tasks/{task_id}", response_model=list[schemas.TaskOut])
def update_task(task_id: int, payload: schemas.TaskUpdate, db: Session = Depends(get_db)):
    """
    Updates a task and, if its end_date or duration changed, propagates the
    schedule change to descendants using the no-compounding DAG logic.
    Returns every task whose fields changed as a result (the task itself,
    plus any downstream tasks that moved).
    """
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    old_end = task.end_date
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.flush()

    tasks, edges = _load_graph(db)
    states = {tid: _to_task_state(t) for tid, t in tasks.items()}

    changed = [task]
    if task.end_date != old_end:
        moved = propagate_schedule_change(task_id, states, edges)
        for tid, new_state in moved.items():
            db_task = tasks[tid]
            db_task.start_date = new_state.start_date
            db_task.end_date = new_state.end_date
            changed.append(db_task)

    db.commit()

    # recompute ready/blocked once more against final persisted state
    tasks, edges = _load_graph(db)
    states = {tid: _to_task_state(t) for tid, t in tasks.items()}
    ready_blocked = compute_ready_blocked(states, edges)
    prereqs: dict[int, list[int]] = {}
    for tid, dep_id in edges:
        prereqs.setdefault(tid, []).append(dep_id)

    return [
        _serialize(t, ready_blocked[t.id], prereqs.get(t.id, [])) for t in changed
    ]


# ---------- dependency endpoints ----------

@app.post("/dependencies")
def create_dependency(payload: schemas.DependencyCreate, db: Session = Depends(get_db)):
    _, edges = _load_graph(db)
    try:
        validate_new_edge(payload.task_id, payload.depends_on_task_id, edges)
    except CycleError as e:
        raise HTTPException(400, str(e))

    dep = models.TaskDependency(
        task_id=payload.task_id, depends_on_task_id=payload.depends_on_task_id
    )
    db.add(dep)
    db.commit()
    return {"status": "created"}


@app.delete("/dependencies")
def delete_dependency(task_id: int, depends_on_task_id: int, db: Session = Depends(get_db)):
    dep = db.get(models.TaskDependency, (task_id, depends_on_task_id))
    if not dep:
        raise HTTPException(404, "Dependency not found")
    db.delete(dep)
    db.commit()
    return {"status": "deleted"}


# ---------- AI suggestion endpoint ----------

@app.post("/suggest-dependencies", response_model=list[schemas.DependencySuggestion])
def suggest_dependencies_endpoint(
    payload: schemas.SuggestDependenciesRequest, db: Session = Depends(get_db)
):
    tasks, edges = _load_graph(db)
    target = tasks.get(payload.task_id)
    if not target:
        raise HTTPException(404, "Task not found")

    target_dict = {"id": target.id, "title": target.title, "description": target.description}
    candidates = [
        {"id": t.id, "title": t.title, "description": t.description}
        for t in tasks.values()
    ]
    return suggest_dependencies(target_dict, candidates, edges)
