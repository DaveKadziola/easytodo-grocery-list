from app import create_app
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


@socketio.on("request_updates")
def handle_request_updates(last_timestamp):
    with app.app_context():
        try:
            conn = psycopg2.connect(
                host=DB_CONFIG["host"],
                port=DB_CONFIG["port"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                database=DB_CONFIG["dbname"],
                options=f"-c search_path={CURRENT_SCHEMA}",
            )
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action, event_data, created_at
                FROM prod.updates_log 
                WHERE created_at > %s 
                ORDER BY id ASC, created_at ASC
                """, (last_timestamp, ))

            for update_id, action, event_data, timestamp in cursor:
                socketio.emit(action, {
                    'request_updates': True,
                    'update_id': update_id,
                    'timestamp': timestamp.isoformat(),
                    **event_data
                },
                              room=request.sid)

        except psycopg2.Error as e:
            print(f"Database error: {e}")
            emit("sync_error", {"message": str(e)})
        finally:
            if 'cursor' in locals():
                cursor.close()
            if conn and not conn.closed:
                conn.close()
