from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from ..models import Category, TaskAssignment, Task
from ..extensions import db

categories_bp = Blueprint("categories", __name__)


# Add new category
@categories_bp.route("/add_category", methods=["POST"])
def add_category():
    try:
        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({"status": "error", "message": "Nieprawidłowy format żądania"}), 400

        data = request.get_json()
        category_name = data.get("category_name")

        # Validate fields
        if not category_name:
            return (
                jsonify({"status": "error", "message": "Nazwa kategorii jest wymagana"}),
                400,
            )

        # Determine position value for new category to put it in the end of the list
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


# Rename category
@categories_bp.route("/rename_category/<int:category_id>", methods=["POST"])
def rename_category(category_id):
    try:
        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({"status": "error", "message": "Nieprawidłowy format żądania"}), 400

        data = request.get_json()

        # Validate fields
        if not data or "name" not in data:
            return jsonify({"status": "error", "message": "Brak wymaganego pola 'name'"}), 400

        category = Category.query.get(category_id)

        if not category:
            return jsonify({"status": "error", "message": "Kategoria nie znaleziona"}), 404

        # Apply new name if confition is meet
        new_name = data["name"].strip()
        if not new_name:
            return (
                jsonify({"status": "error", "message": "Nazwa kategorii nie może być pusta"}),
                400,
            )

        category.name = new_name
        category.updated_at = datetime.now()
        db.session.commit()

        return jsonify({"status": "success", "category_id": category_id, "new_name": new_name}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Delete category with all assigned tasks
@categories_bp.route("/delete_category/<int:category_id>", methods=["POST"])
def delete_category(category_id):
    try:
        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({"status": "error", "message": "Nieprawidłowy format żądania"}), 400

        category = Category.query.get(category_id)

        # Validate fields
        if not category:
            return jsonify({"status": "error", "message": "Kategoria nie znaleziona"}), 404

        # Remove tasks assigned to category
        assignments = TaskAssignment.query.filter_by(category_id=category.id).all()
        for assign in assignments:
            task = Task.query.get(assign.task_id)
            if task:
                db.session.delete(task)

        db.session.delete(category)
        db.session.commit()

        return (
            jsonify(
                {"status": "success", "message": "Kategoria usunięta", "category_id": category_id}
            ),
            200,
        )

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"Błąd serwera: {str(e)}"}), 500


# Move category
@categories_bp.route("/move_category", methods=["POST"])
def move_category():
    try:
        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({"status": "error", "message": "Nieprawidłowy format żądania"}), 400

        data = request.get_json()

        # Validate fields
        if not data or "category_id" not in data or "direction" not in data:
            return (
                jsonify(
                    {"status": "error", "message": "Brak wymaganych pól: category_id i direction"}
                ),
                400,
            )

        category_id = data["category_id"]
        direction = data["direction"].lower()

        # Find source category in db
        category = Category.query.get(category_id)
        if not category:
            return jsonify({"status": "error", "message": "Nie znaleziono kategorii"}), 404

        # Find target category in db
        query = Category.query
        if direction == "up":
            swap_category = (
                query.filter(Category.position < category.position)
                .order_by(Category.position.desc())
                .first()
            )
        elif direction == "down":
            swap_category = (
                query.filter(Category.position > category.position)
                .order_by(Category.position)
                .first()
            )
        else:
            return jsonify({"status": "error", "message": "Nieprawidłowy kierunek"}), 400

        # Swap category positions
        if swap_category:
            category.position, swap_category.position = swap_category.position, category.position
            db.session.commit()
            return (
                jsonify(
                    {
                        "status": "success",
                        "category_id": category_id,
                        "new_position": category.position,
                        "swap_category_id": swap_category.id,
                    }
                ),
                200,
            )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"Błąd serwera: {str(e)}"}), 500


# Get all categories
@categories_bp.route("/get_all_categories/", methods=["GET"])
def get_all_categories():
    try:
        # Get all categories
        categories = (
            Category.query.with_entities(Category.id, Category.name).order_by(Category.name).all()
        )

        category_list = []
        for category in categories:
            category_list.append({"id": category.id, "name": category.name})

        return (
            jsonify(
                {"status": "success", "count": len(category_list), "categories": category_list}
            ),
            200,
        )

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"Błąd bazy danych: {str(e)}"}), 500

    except Exception as e:
        return jsonify({"status": "error", "message": f"Krytyczny błąd serwera: {str(e)}"}), 500
