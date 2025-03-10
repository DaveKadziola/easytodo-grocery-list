from ..extensions import socketio
from ..utils.config import DB_CONFIG, CURRENT_SCHEMA
from flask_socketio import emit
from flask import request

import psycopg2


@socketio.on("connect")
def handle_connect(auth=None):
    print(f"Client connected: {request.sid}")
    emit("connection_status", {"status": "connected", "sid": request.sid})


@socketio.on("disconnect")
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")
    emit("connection_status", {"status": "disconnected", "sid": request.sid})


def emit_update(event_type, data):
    socketio.emit("update", {"type": event_type, "data": data})
