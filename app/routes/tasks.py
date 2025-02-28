from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from flasgger import swag_from
from ..models import TaskAssignment, Task
from ..extensions import db, BASE_DIR

tasks_bp = Blueprint("tasks", __name__)


# Assign task to category
@swag_from(BASE_DIR / "./docs/add_task.yml")
@tasks_bp.route("/v1/add_task/", methods=["POST"])
def add_task():
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
        if not data or "category_id" not in data or "task_name" not in data:
            return jsonify({
                "status": "error",
                "message": "Required fields are missing"
            }), 400

        category_id = data["category_id"]
        task_name = data["task_name"]
        creation_time = datetime.now()

        new_task = Task(name=task_name,
                        created_at=creation_time,
                        updated_at=creation_time)
        db.session.add(new_task)
        db.session.commit()

        assignment = TaskAssignment(task_id=new_task.id,
                                    category_id=category_id,
                                    assigned_at=creation_time)
        db.session.add(assignment)
        db.session.commit()

        return (
            jsonify({
                "status": "success",
                "message": "Task added successfully",
                "category_id": category_id,
                "task_id": new_task.id,
                "task_name": new_task.name,
                "is_done": new_task.is_done
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


# Update task – change name and description
@swag_from(BASE_DIR / "./docs/update_task.yml")
@tasks_bp.route("/v1/update_task/<int:task_id>", methods=["PUT"])
def update_task(task_id):
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
                "message": "Required fields are missing"
            }), 400

        task = Task.query.get(task_id)
        if not task:
            return jsonify({
                "status": "error",
                "message": "Task not found",
                "task_id": task.id
            }), 404

        new_name = data["name"].strip()
        new_description = data.get("description", "").strip()

        if not new_name:
            return jsonify({
                "status": "error",
                "message": "The name of the task is required"
            }), 400

        task.name = new_name
        task.description = new_description
        task.updated_at = datetime.now()
        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Task details updated successfully",
            "task_id": task.id,
            "task_name": task.name,
            "description": task.description
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


# Delete task
@swag_from(BASE_DIR / "./docs/delete_task.yml")
@tasks_bp.route("/v1/delete_task/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
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

        task = Task.query.get(task_id)
        if not task:
            return jsonify({
                "status": "error",
                "message": "Task not found",
                "task_id": task_id
            }), 404

        db.session.delete(task)
        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Task successfully deleted",
            "task_id": task_id
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


# Move task to other category
@swag_from(BASE_DIR / "./docs/move_task.yml")
@tasks_bp.route("/v1/move_task", methods=["PUT"])
def move_task():
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
        if not data or "task_id" not in data or "new_category_id" not in data:
            return jsonify({
                "status": "error",
                "message": "Required fields are missing"
            }), 400

        task_id = data["task_id"]
        new_category_id = data["new_category_id"]

        assignment = TaskAssignment.query.filter_by(task_id=task_id).first()
        if assignment:
            assignment.category_id = new_category_id
            assignment.assigned_at = datetime.now()
            db.session.commit()
            return jsonify({
                "status":
                "success",
                "message":
                f"Task {task_id} moved successfully to category {new_category_id}"
            }), 200

        return jsonify({
            "status":
            "error",
            "message":
            f"Task assignment for task_id {task_id} not found"
        }), 404

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


# Update task status
@swag_from(BASE_DIR / "./docs/toggle_task.yml")
@tasks_bp.route("/v1/toggle_task/<int:task_id>", methods=["PUT"])
def toggle_task(task_id):
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

        task = Task.query.get(task_id)

        if not task:
            return jsonify({
                "status": "error",
                "message": "Task not found"
            }), 404

        data = request.get_json()

        # Validate fields
        if not data or "is_done" not in data:
            return jsonify({
                "status": "error",
                "message": "Invalid field(s)"
            }), 400

        # Set is_done status from frontend input (True/False)
        task.is_done = data["is_done"]
        task.updated_at = datetime.now()
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": "Status for task changed successfully",
            "task_id": task_id,
            "is_done": task.is_done
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
