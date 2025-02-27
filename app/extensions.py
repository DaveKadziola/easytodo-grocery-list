from flask_sqlalchemy import SQLAlchemy
from flask import Flask
from flask_socketio import SocketIO
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

db = SQLAlchemy()

app = Flask(__name__)

socketio = SocketIO(
    app,
    async_mode="eventlet",
    cors_allowed_origins="*",
    logger=True,
    engineio_logger=True,
)
