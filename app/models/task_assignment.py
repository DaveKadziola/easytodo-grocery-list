from datetime import datetime
from ..extensions import db
from ..utils.config import CURRENT_SCHEMA


class TaskAssignment(db.Model):
    __tablename__ = "task_assignment"
    __table_args__ = {"schema": CURRENT_SCHEMA}

    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer,
        db.ForeignKey(f"{CURRENT_SCHEMA}.tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    category_id = db.Column(
        db.Integer,
        db.ForeignKey(f"{CURRENT_SCHEMA}.categories.id", ondelete="CASCADE"),
        nullable=False,
    )

    assigned_at = db.Column(db.DateTime, default=datetime.now())
