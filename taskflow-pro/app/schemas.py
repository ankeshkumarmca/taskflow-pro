from datetime import date
from pydantic import BaseModel, ConfigDict


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    status_column: str = "backlog"
    start_date: date
    end_date: date
    duration_days: int
    board_position: int = 0


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status_column: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    duration_days: int | None = None
    board_position: int | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    status_column: str
    start_date: date
    end_date: date
    duration_days: int
    board_position: int
    dependency_state: str  # 'ready' | 'blocked' | 'done', computed
    depends_on: list[int]  # prerequisite task ids


class DependencyCreate(BaseModel):
    task_id: int
    depends_on_task_id: int


class SuggestDependenciesRequest(BaseModel):
    task_id: int  # suggest prerequisites for this task, from the rest of the board


class DependencySuggestion(BaseModel):
    depends_on_task_id: int
    confidence: str  # 'high' | 'medium' | 'low'
    rationale: str
