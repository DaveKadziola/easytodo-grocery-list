from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from flasgger import swag_from
from ..models import Category, TaskAssignment, Task
from ..extensions import db, BASE_DIR

task_assignments_bp = Blueprint("task_assignments", __name__)


# Get all tasks by category
@swag_from(BASE_DIR / "./docs/get_tasks_by_category.yml")
@task_assignments_bp.route("/v1/get_tasks_by_category/<int:category_id>",
                           methods=["GET"])
def get_tasks_by_category(category_id):
    try:
        # Verify API version
        accept_header = request.headers.get('Accept', '')
        print(accept_header)
        if 'application/vnd.myapi.v1+json' not in accept_header:
            return jsonify({
                "status":
                "error",
                "message":
                "Unsupported API version. Required header: Accept: application/vnd.myapi.v1+json"
            }), 406

        # Check if category exists
        category = Category.query.get(category_id)
        if not category:
            return jsonify({
                "status": "error",
                "message": "Category to fetch tasks not found",
                "category_id": category_id
            }), 404

        # Get task assignments
        assignments = TaskAssignment.query.filter_by(
            category_id=category_id).all()
        task_ids = [assign.task_id for assign in assignments]

        # Filter tasks by undone first and by name
        if task_ids:
            tasks = Task.query.filter(Task.id.in_(task_ids)).order_by(
                Task.is_done, Task.name).all()
        else:
            tasks = []

        task_list = []
        for task in tasks:
            task_list.append({
                "id": task.id,
                "name": task.name,
                "is_done": task.is_done,
                "description": task.description,
            })

        return jsonify({
            "status": "success",
            "message":
            f"Task list for category {category_id} fetched successfully",
            "count": len(task_list),
            "tasks": task_list
        }), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Internal server error occurred: {str(e)}"
        }), 500
