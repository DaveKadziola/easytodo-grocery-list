from flask import Blueprint, jsonify, request
from datetime import datetime
from ..models import TaskAssignment, Task
from ..extensions import db

tasks_bp = Blueprint("tasks", __name__)


# Assign task to category
@tasks_bp.route("/add_task/", methods=["POST"])
def add_task():

    data = request.get_json()

    if not data or "category_id" not in data or "task_name" not in data:
        return (
            jsonify({"status": "error", "message": "Brak wymaganych pól"}),
            400,
        )

    category_id = data["category_id"]
    task_name = data["task_name"]

    try:
        new_task = Task(name=task_name)
        db.session.add(new_task)
        db.session.commit()

        assignment = TaskAssignment(task_id=new_task.id, category_id=category_id)
        db.session.add(assignment)
        db.session.commit()

        return (
            jsonify(
                {
                    "status": "success",
                    "category_id": category_id,
                    "task_id": new_task.id,
                    "task_name": new_task.name,
                    "is_done": new_task.is_done,
                    "description": new_task.description,
                }
            ),
            200,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Update task – change name and description
@tasks_bp.route("/update_task/<int:task_id>", methods=["POST"])
def update_task(task_id):
    task = Task.query.get(task_id)
    if task:
        task.name = request.form.get("name", task.name)
        task.description = request.form.get("description", task.description)
        task.updated_at = datetime.now()
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Task not found"})


# Delete task
@tasks_bp.route("/delete_task/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    task = Task.query.get(task_id)
    if task:
        db.session.delete(task)
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Task not found"})


# Move task to other category
@tasks_bp.route("/move_task", methods=["POST"])
def move_task():
    task_id = request.form.get("task_id")
    new_category_id = request.form.get("new_category_id")
    if task_id and new_category_id:
        assignment = TaskAssignment.query.filter_by(task_id=task_id).first()
        if assignment:
            assignment.category_id = new_category_id
            db.session.commit()
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "error", "message": "Assignment not found"})

    return jsonify({"status": "error", "message": "Invalid parameters"})


# Update task status
@tasks_bp.route("/toggle_task/<int:task_id>", methods=["POST"])
def toggle_task(task_id):
    task = Task.query.get(task_id)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    data = request.get_json()
    if not data or "is_done" not in data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400

    # Set is_done status from frontend input (True/False)
    task.is_done = data["is_done"]
    task.updated_at = datetime.now()
    db.session.commit()

    return jsonify({"status": "success", "is_done": task.is_done})
