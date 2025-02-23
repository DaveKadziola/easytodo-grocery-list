from flask import Blueprint, jsonify
from ..models import TaskAssignment, Task

task_assignments_bp = Blueprint("task_assignments", __name__)


@task_assignments_bp.route("/get_tasks_by_category/<int:category_id>", methods=["GET"])
def get_tasks_by_category(category_id):
    # Get all task with assignments
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

    return jsonify(task_list)
