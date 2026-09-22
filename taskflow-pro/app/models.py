from sqlalchemy import Column, Integer, String, Date, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    # One of: backlog, in_progress, review, done
    status_column = Column(String, nullable=False, default="backlog")
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    duration_days = Column(Integer, nullable=False)
    board_position = Column(Integer, nullable=False, default=0)

    # Prerequisites this task depends on (edges where this task is the dependent)
    dependencies = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.task_id",
        back_populates="task",
        cascade="all, delete-orphan",
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id = Column(Integer, ForeignKey("tasks.id"), primary_key=True)
    depends_on_task_id = Column(Integer, ForeignKey("tasks.id"), primary_key=True)

    task = relationship("Task", foreign_keys=[task_id], back_populates="dependencies")
