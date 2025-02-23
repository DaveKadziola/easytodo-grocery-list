from flask import Blueprint, render_template
from ..models import Category, TaskAssignment, Task
from ..extensions import db

index_bp = Blueprint("index", __name__)


# Main page
@index_bp.route("/")
def index():
    categories = Category.query.order_by(Category.position).all()
    tasks_by_category = {}

    for cat in categories:
        # Get all task with assignments
        assignments = TaskAssignment.query.filter_by(category_id=cat.id).all()
        task_ids = [assign.task_id for assign in assignments]

        # Filter tasks by undone first and by name
        if task_ids:
            tasks = Task.query.filter(Task.id.in_(task_ids)).order_by(Task.is_done, Task.name).all()
        else:
            tasks = []

        tasks_by_category[cat.id] = tasks

    return render_template("index.html", categories=categories, tasks_by_category=tasks_by_category)
