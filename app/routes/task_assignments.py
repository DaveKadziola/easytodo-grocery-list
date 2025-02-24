from flask import Blueprint, jsonify
from sqlalchemy.exc import SQLAlchemyError
from ..models import Category, TaskAssignment, Task
from ..extensions import db

task_assignments_bp = Blueprint("task_assignments", __name__)


# Get all tasks by category
@task_assignments_bp.route("/get_tasks_by_category/<int:category_id>", methods=["GET"])
def get_tasks_by_category(category_id):
    try:
        # Check if category exists
        category = Category.query.get(category_id)
        if not category:
            return jsonify({"status": "error", "message": "Nie znaleziono kategorii"}), 404

        # Get task assignments
        assignments = TaskAssignment.query.filter_by(category_id=category_id).all()
        task_ids = [assign.task_id for assign in assignments]

        # Filter tasks by undone first and by name
        if task_ids:
            tasks = Task.query.filter(Task.id.in_(task_ids)).order_by(Task.is_done, Task.name).all()
        else:
            tasks = []

        task_list = []
        for task in tasks:
            task_list.append(
                {
                    "id": task.id,
                    "name": task.name,
                    "is_done": task.is_done,
                    "description": task.description,
                }
            )

        return jsonify({"status": "success", "count": len(task_list), "tasks": task_list}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"Błąd bazy danych: {str(e)}"}), 500

    except Exception as e:
        return jsonify({"status": "error", "message": f"Krytyczny błąd serwera: {str(e)}"}), 500
