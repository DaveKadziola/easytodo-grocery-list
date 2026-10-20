import eventlet

eventlet.monkey_patch()

from app import create_app, socketio
from app.utils.config import DB_CONFIG, CURRENT_SCHEMA, APP_HOST_NAME, APP_PORT
from flask import request

import psycopg2
import json
import select

app = create_app()


def listen_to_db():
    with app.app_context():
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
        cursor.execute("LISTEN data_changes;")

        while True:
            eventlet.sleep(0.1)
            if select.select([conn], [], [], 5) == ([], [], []):
                continue
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop()
                data = json.loads(notify.payload)

                event_name = data.get('action')
                if event_name:
                    socketio.emit(event_name, data)
                else:
                    socketio.emit("update", data)


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


if __name__ == "__main__":
    eventlet.spawn(listen_to_db)
    socketio.run(app, debug=True, host=APP_HOST_NAME, port=APP_PORT)
