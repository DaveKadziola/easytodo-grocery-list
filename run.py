import eventlet

eventlet.monkey_patch()

from app import create_app, socketio
from app.utils.config import DB_CONFIG, CURRENT_SCHEMA

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


if __name__ == "__main__":
    eventlet.spawn(listen_to_db)
    socketio.run(app, debug=True, host="0.0.0.0", port=8100)
