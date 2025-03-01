from datetime import datetime
from ..extensions import db
from ..utils.config import CURRENT_SCHEMA


class TemporaryData(db.Model):
    __tablename__ = "temporary_data"
    __table_args__ = {"schema": CURRENT_SCHEMA}

    id = db.Column(db.Integer, primary_key=True)
    field01 = db.Column(db.String(255))
    field02 = db.Column(db.String(255))
    field03 = db.Column(db.String(255))
    field04 = db.Column(db.String(255))
    field05 = db.Column(db.String(255))
    field06 = db.Column(db.String(255))
    field07 = db.Column(db.String(255))
    field08 = db.Column(db.String(255))
    field09 = db.Column(db.String(255))
    field10 = db.Column(db.String(255))
