from datetime import datetime
from ..extensions import db
from ..utils.config import CURRENT_SCHEMA


class Category(db.Model):
    __tablename__ = "categories"
    __table_args__ = {"schema": CURRENT_SCHEMA}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now())
    updated_at = db.Column(db.DateTime, default=datetime.now())
