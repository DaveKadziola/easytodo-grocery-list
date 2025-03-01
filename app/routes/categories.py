from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timedelta
from flasgger import swag_from
from ..models import Category, TaskAssignment, Task, TemporaryData
from ..extensions import db, BASE_DIR

categories_bp = Blueprint("categories", __name__)


# Add new category
@swag_from(BASE_DIR / "./docs/add_category.yml")
@categories_bp.route("/v1/add_category", methods=["POST"])
def add_category():
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

        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Invalid request format"
            }), 400

        data = request.get_json()
        category_name = data.get("category_name")

        # Validate fields
        if not category_name:
            return (
                jsonify({
                    "status": "error",
                    "message": "Category name is required"
                }),
                400,
            )

        # Determine position value for new category to put it in the end of the list
        max_position = db.session.query(db.func.max(
            Category.position)).scalar() or 0
        new_category = Category(name=category_name, position=max_position + 1)
        db.session.add(new_category)
        db.session.commit()

        return (
            jsonify({
                "status": "success",
                "message": "Category created successfully",
                "category_id": new_category.id,
                "category_name": new_category.name,
                "category_position": new_category.position,
            }),
            200,
        )

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


# Rename category
@swag_from(BASE_DIR / "./docs/rename_category.yml")
@categories_bp.route("/v1/rename_category/<int:category_id>", methods=["PUT"])
def rename_category(category_id):
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

        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Invalid request format"
            }), 400

        data = request.get_json()

        # Validate fields
        if not data or "name" not in data:
            return jsonify({
                "status": "error",
                "message": "Missing required field 'name'"
            }), 400

        category = Category.query.get(category_id)

        if not category:
            return jsonify({
                "status": "error",
                "message": "Category not found",
                "category_id": category_id
            }), 404

        # Apply new name if confition is meet
        new_name = data["name"].strip()
        if not new_name:
            return (
                jsonify({
                    "status": "error",
                    "message": "Category name cannot be empty"
                }),
                400,
            )

        category.name = new_name
        category.updated_at = datetime.now()
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": "Updated category Name",
            "category_id": category_id,
            "new_name": new_name
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


# Delete category with all assigned tasks
@swag_from(BASE_DIR / "./docs/delete_category.yml")
@categories_bp.route("/v1/delete_category/<int:category_id>",
                     methods=["DELETE"])
def delete_category(category_id):
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

        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Invalid request format"
            }), 400

        category = Category.query.get(category_id)

        # Validate fields
        if not category:
            return jsonify({
                "status": "error",
                "message": "Category not found",
                "category_id": category_id
            }), 404

        # Remove tasks assigned to category
        assignments = TaskAssignment.query.filter_by(
            category_id=category.id).all()
        for assign in assignments:
            task = Task.query.get(assign.task_id)
            if task:
                db.session.delete(task)

        temp_data = TemporaryData(field01="categories",
                                  field02="DELETE_CATEGORY",
                                  field03=category_id)

        db.session.add(temp_data)
        db.session.commit()

        db.session.delete(category)
        db.session.commit()

        return (
            jsonify({
                "status": "success",
                "message": "Category deleted",
                "category_id": category_id
            }),
            200,
        )

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": f"Internal server error occurred: {str(e)}"
        }), 500


# Move category
@swag_from(BASE_DIR / "./docs/move_category.yml")
@categories_bp.route("/v1/move_category", methods=["PUT"])
def move_category():
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

        # Require header Content-Type: application/json
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Invalid request format"
            }), 400

        data = request.get_json()

        # Validate fields
        if not data or "category_id" not in data or "direction" not in data:
            return (
                jsonify({
                    "status":
                    "error",
                    "message":
                    "Required fields are missing: category_id and direction"
                }),
                400,
            )

        category_id = data["category_id"]
        direction = data["direction"].lower()

        # Find source category in db
        category = Category.query.get(category_id)
        if not category:
            return jsonify({
                "status": "error",
                "message": "Category not found",
                "category_id": category_id
            }), 404

        # Find target category in db
        query = Category.query
        if direction == "up":
            swap_category = (query.filter(
                Category.position < category.position).order_by(
                    Category.position.desc()).first())
        elif direction == "down":
            swap_category = (query.filter(
                Category.position > category.position).order_by(
                    Category.position).first())
        else:
            return jsonify({
                "status": "error",
                "message": "Invalid direction parameter"
            }), 400

        # Swap category positions
        if swap_category:
            category.position, swap_category.position = swap_category.position, category.position

            base_time = datetime.now()
            swap_category.updated_at = base_time
            category.updated_at = base_time + timedelta(microseconds=1000)
            db.session.commit()

            temp_data = TemporaryData(field01="categories",
                                      field02="MOVE_CATEGORY",
                                      field03=category_id,
                                      field04=direction)

            db.session.add(temp_data)
            db.session.commit()

            return (jsonify({
                "status": "success",
                "category_id": category_id,
                "new_position": category.position,
                "swap_category_id": swap_category.id,
            }), 200)

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": f"Internal server error occurred: {str(e)}"
        }), 500


# Get all categories
@swag_from(BASE_DIR / "./docs/get_all_categories.yml")
@categories_bp.route("/v1/get_all_categories/", methods=["GET"])
def get_all_categories():
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

        # Get all categories
        categories = (Category.query.with_entities(
            Category.id, Category.name).order_by(Category.name).all())

        category_list = []
        for category in categories:
            category_list.append({"id": category.id, "name": category.name})

        return (
            jsonify({
                "status": "success",
                "message": "Successfully retrieved categories list",
                "count": len(category_list),
                "categories": category_list
            }),
            200,
        )

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
