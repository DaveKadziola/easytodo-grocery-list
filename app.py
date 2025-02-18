import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from datetime import datetime
import configparser
import psycopg2
import threading
import json
import select
from flask_cors import CORS

app = Flask(__name__)
socketio = SocketIO(app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    logger=True,
    engineio_logger=True
)

# Load configuration from file
config = configparser.ConfigParser()
config.read('config.ini')

# Build database connection string
db_config = config['database']
app.config['SQLALCHEMY_DATABASE_URI'] = (
    f"postgresql://{db_config['user']}:{db_config['password']}"
    f"@{db_config['host']}:{db_config['port']}/{db_config['dbname']}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Get current schema
current_schema = config['schema']['name']

db = SQLAlchemy(app)


class Category(db.Model):
    __tablename__ = 'categories'
    __table_args__ = {'schema': current_schema}
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Task(db.Model):
    __tablename__ = 'tasks'
    __table_args__ = {'schema': current_schema}
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    is_done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskAssignment(db.Model):
    __tablename__ = 'task_assignment'
    __table_args__ = {'schema': current_schema}
    id = db.Column(db.Integer, primary_key=True)

    task_id = db.Column(
        db.Integer,
        db.ForeignKey(f'{current_schema}.tasks.id', ondelete='CASCADE'),
        nullable=False
    )

    category_id = db.Column(
        db.Integer,
        db.ForeignKey(f'{current_schema}.categories.id', ondelete='CASCADE'),
        nullable=False
    )

    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)


# Main page
@app.route('/')
def index():
    categories = Category.query.order_by(Category.position).all()
    tasks_by_category = {}

    for cat in categories:
        # Get all task with assignments
        assignments = TaskAssignment.query.filter_by(category_id=cat.id).all()
        task_ids = [assign.task_id for assign in assignments]

        # Filter tasks by undone first and by name
        if task_ids:
            tasks = (
                Task.query.filter(Task.id.in_(task_ids))
                .order_by(Task.is_done, Task.name)
                .all()
            )
        else:
            tasks = []

        tasks_by_category[cat.id] = tasks

    return render_template(
        'index.html',
        categories=categories,
        tasks_by_category=tasks_by_category)


# Add new category
@app.route('/add_category', methods=['POST'])
def add_category():
    category_name = request.form.get('category_name')
    if not category_name:
        return jsonify({'status': 'error', 'message': 'Nazwa kategorii jest wymagana'}), 400

    try:
        max_position = db.session.query(db.func.max(Category.position)).scalar() or 0
        new_category = Category(name=category_name, position=max_position + 1)
        db.session.add(new_category)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'category_id': new_category.id,
            'category_name': new_category.name,
            'category_position': new_category.position
        }), 200

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500





# Update category name
@app.route('/update_category/<int:category_id>', methods=['POST'])
def update_category(category_id):
    category = Category.query.get(category_id)
    if category:
        category.name = request.form.get('name', category.name)
        category.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Category not found"
    })


# Delete category with all assigned tasks
@app.route('/delete_category/<int:category_id>', methods=['POST'])
def delete_category(category_id):
    category = Category.query.get(category_id)
    if category:
        assignments = (
            TaskAssignment.query
            .filter_by(category_id=category.id)
            .all()
        )

        for assign in assignments:
            task = Task.query.get(assign.task_id)
            if task:
                db.session.delete(task)

        db.session.delete(category)
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Category not found"
    })


# Assign task to category
@app.route('/add_task/', methods=['POST'])
def add_task():

    data = request.get_json()

    if not data or 'category_id' not in data or 'task_name' not in data:
        return jsonify({
            "status": "error",
            "message": "Brak wymaganych pól"
        }), 400

    category_id = data['category_id']
    task_name = data['task_name']

    try:
        new_task = Task(name=task_name)
        db.session.add(new_task)
        db.session.commit()

        assignment = TaskAssignment(
            task_id=new_task.id,
            category_id=category_id
        )
        db.session.add(assignment)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'category_id': category_id,
            'task_id': new_task.id,
            'task_name': new_task.name,
            'is_done': new_task.is_done,
            'description': new_task.description
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500




# Update task – change name and description
@app.route('/update_task/<int:task_id>', methods=['POST'])
def update_task(task_id):
    task = Task.query.get(task_id)
    if task:
        task.name = request.form.get('name', task.name)
        task.description = request.form.get('description', task.description)
        task.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Task not found"
    })


# Delete task
@app.route('/delete_task/<int:task_id>', methods=['POST'])
def delete_task(task_id):
    task = Task.query.get(task_id)
    if task:
        db.session.delete(task)
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Task not found"
    })


# Move task to other category
@app.route('/move_task', methods=['POST'])
def move_task():
    task_id = request.form.get('task_id')
    new_category_id = request.form.get('new_category_id')
    if task_id and new_category_id:
        assignment = TaskAssignment.query.filter_by(task_id=task_id).first()
        if assignment:
            assignment.category_id = new_category_id
            db.session.commit()
            return jsonify({"status": "success"})
        else:
            return jsonify({
                "status": "error",
                "message": "Assignment not found"
            })

    return jsonify({
        "status": "error",
        "message": "Invalid parameters"
    })


# Rename category
@app.route('/rename_category/<int:category_id>', methods=['POST'])
def rename_category(category_id):
    cat = Category.query.get(category_id)
    new_name = request.form.get('name')
    if cat and new_name:
        cat.name = new_name
        cat.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Error renaming category"
    })


# Move category
@app.route('/move_category', methods=['POST'])
def move_category():
    # Obsługa przeciągania: swap pozycji
    dragged_category_id = request.form.get('dragged_category_id')
    target_category_id = request.form.get('target_category_id')
    if dragged_category_id and target_category_id:
        cat1 = Category.query.get(int(dragged_category_id))
        cat2 = Category.query.get(int(target_category_id))
        if cat1 and cat2:
            temp = cat1.position
            cat1.position = cat2.position
            cat2.position = temp
            db.session.commit()
            return jsonify({"status": "success"})
        else:
            return jsonify({
                "status": "error",
                "message": "Category not found"
            })
    else:
        # Handling arrow buttons ('up' or 'down')
        category_id = request.form.get('category_id')
        direction = request.form.get('direction')
        if category_id and direction:
            cat = Category.query.get(int(category_id))
            if not cat:
                return jsonify({
                    "status": "error",
                    "message": "Category not found"
                })

            if direction == 'up':
                swap_cat = (
                    Category.query
                    .filter(Category.position < cat.position)
                    .order_by(Category.position.desc())
                    .first()
                )

            elif direction == 'down':
                swap_cat = (
                    Category.query
                    .filter(Category.position > cat.position)
                    .order_by(Category.position)
                    .first()
                )

            else:
                return jsonify({
                    "status": "error",
                    "message": "Invalid direction"
                })

            if swap_cat:
                temp = cat.position
                cat.position = swap_cat.position
                swap_cat.position = temp
                db.session.commit()
                return jsonify({"status": "success"})
            else:
                return jsonify({"status": "success"})
    return jsonify({
        "status": "error",
        "message": "Invalid parameters"
    })


# Update task status
@app.route('/toggle_task/<int:task_id>', methods=['POST'])
def toggle_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        return jsonify({
            "status": "error",
            "message": "Task not found"
        }), 404

    data = request.get_json()
    if not data or "is_done" not in data:
        return jsonify({
            "status": "error",
            "message": "Invalid request"
        }), 400

    # Set is_done status from frontend input (True/False)
    task.is_done = data["is_done"]
    task.updated_at = datetime.utcnow()
    db.session.commit()



    return jsonify({
        "status": "success",
        "is_done": task.is_done
    })

@app.route('/get_tasks_by_category/<int:category_id>', methods=['GET'])
def get_tasks_by_category(category_id):
    # Get all task with assignments
    assignments = TaskAssignment.query.filter_by(category_id=category_id).all()
    task_ids = [assign.task_id for assign in assignments]

    # Filter tasks by undone first and by name
    if task_ids:
        tasks = (
            Task.query.filter(Task.id.in_(task_ids))
            .order_by(Task.is_done, Task.name)
            .all()
        )
    else:
        tasks = []

    task_list = []
    for task in tasks:
        task_list.append({
            'id': task.id,
            'name': task.name,
            'is_done': task.is_done,
            'description': task.description
        })

    return jsonify(task_list)



def listen_to_db():
    with app.app_context():
        conn = psycopg2.connect(
            host=db_config['host'],
            port=db_config['port'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['dbname'],
            options=f"-c search_path={config['schema']['name']}"
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
                socketio.emit('update', data)


@socketio.on('connect')
def handle_connect(auth=None):
    print(f"Client connected: {request.sid}")
    emit('connection_status', {
        'status': 'connected',
        'sid': request.sid
    })


def emit_update(event_type, data):
    socketio.emit('update', {'type': event_type, 'data': data})

if __name__ == '__main__':
    #app.run(debug=True, host="0.0.0.0", port=8100)
    #threading.Thread(target=listen_to_db, daemon=True).start()
    eventlet.spawn(listen_to_db)

    socketio.run(app, debug=True, host="0.0.0.0", port=8100)

