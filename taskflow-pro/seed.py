"""Load seed-data.json into the database. Run with: python seed.py"""
import json
import os
from datetime import date

from app.database import Base, engine, SessionLocal
from app import models

SEED_PATH = os.path.join(os.path.dirname(__file__), "seed-data.json")


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    if db.query(models.Task).first():
        print("Database already has tasks -- skipping seed. Delete taskflow.db to reseed.")
        return

    with open(SEED_PATH) as f:
        data = json.load(f)

    for t in data["tasks"]:
        db.add(models.Task(
            id=t["id"],
            title=t["title"],
            description=t["description"],
            status_column=t["status_column"],
            start_date=date.fromisoformat(t["start_date"]),
            end_date=date.fromisoformat(t["end_date"]),
            duration_days=t["duration_days"],
            board_position=t["board_position"],
        ))
    db.flush()

    for t in data["tasks"]:
        for dep_id in t["depends_on"]:
            db.add(models.TaskDependency(task_id=t["id"], depends_on_task_id=dep_id))

    db.commit()
    print(f"Seeded {len(data['tasks'])} tasks.")


if __name__ == "__main__":
    main()
