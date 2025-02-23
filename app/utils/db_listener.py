import psycopg2
import json
import select
from ..extensions import socketio


def listen_to_db():
    with app.app_context():
        conn = psycopg2.connect(
            host=db_config["host"],
            port=db_config["port"],
            user=db_config["user"],
            password=db_config["password"],
            database=db_config["dbname"],
            options=f"-c search_path={config['schema']['name']}",
        )
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
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
                socketio.emit("update", data)
