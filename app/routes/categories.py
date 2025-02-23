from flask import Blueprint, jsonify, request
from datetime import datetime
from ..models import Category, TaskAssignment, Task
from ..extensions import db

categories_bp = Blueprint("categories", __name__)


# Add new category
@categories_bp.route("/add_category", methods=["POST"])
def add_category():
    category_name = request.form.get("category_name")
    if not category_name:
        return (
            jsonify({"status": "error", "message": "Nazwa kategorii jest wymagana"}),
            400,
        )

    try:
        max_position = db.session.query(db.func.max(Category.position)).scalar() or 0
        new_category = Category(name=category_name, position=max_position + 1)
        db.session.add(new_category)
        db.session.commit()

        return (
            jsonify(
                {
                    "status": "success",
                    "category_id": new_category.id,
                    "category_name": new_category.name,
                    "category_position": new_category.position,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Update category name
@categories_bp.route("/update_category/<int:category_id>", methods=["POST"])
def update_category(category_id):
    category = Category.query.get(category_id)
    if category:
        category.name = request.form.get("name", category.name)
        category.updated_at = datetime.now()
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Category not found"})


# Delete category with all assigned tasks
@categories_bp.route("/delete_category/<int:category_id>", methods=["POST"])
def delete_category(category_id):
    category = Category.query.get(category_id)
    if category:
        assignments = TaskAssignment.query.filter_by(category_id=category.id).all()

        for assign in assignments:
            task = Task.query.get(assign.task_id)
            if task:
                db.session.delete(task)

        db.session.delete(category)
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Category not found"})


# Rename category
@categories_bp.route("/rename_category/<int:category_id>", methods=["POST"])
def rename_category(category_id):
    cat = Category.query.get(category_id)
    new_name = request.form.get("name")
    if cat and new_name:
        cat.name = new_name
        cat.updated_at = datetime.now()
        db.session.commit()
        return jsonify({"status": "success", "new_category_name": new_name})
    return jsonify({"status": "error", "message": "Error renaming category"})


# Move category
@categories_bp.route("/move_category", methods=["POST"])
def move_category():
    # Obsługa przeciągania: swap pozycji
    dragged_category_id = request.form.get("dragged_category_id")
    target_category_id = request.form.get("target_category_id")
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
            return jsonify({"status": "error", "message": "Category not found"})
    else:
        # Handling arrow buttons ('up' or 'down')
        category_id = request.form.get("category_id")
        direction = request.form.get("direction")
        if category_id and direction:
            cat = Category.query.get(int(category_id))
            if not cat:
                return jsonify({"status": "error", "message": "Category not found"})

            if direction == "up":
                swap_cat = (
                    Category.query.filter(Category.position < cat.position)
                    .order_by(Category.position.desc())
                    .first()
                )

            elif direction == "down":
                swap_cat = (
                    Category.query.filter(Category.position > cat.position)
                    .order_by(Category.position)
                    .first()
                )

            else:
                return jsonify({"status": "error", "message": "Invalid direction"})

            if swap_cat:
                temp = cat.position
                cat.position = swap_cat.position
                swap_cat.position = temp
                db.session.commit()
                return jsonify({"status": "success"})
            else:
                return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Invalid parameters"})
