from datetime import datetime
from ..extensions import db
from ..utils.config import CURRENT_SCHEMA


class Task(db.Model):
    __tablename__ = "tasks"
    __table_args__ = {"schema": CURRENT_SCHEMA}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    is_done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now())
    updated_at = db.Column(db.DateTime, default=datetime.now())
