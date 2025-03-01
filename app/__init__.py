from flask import Flask
from flask_socketio import SocketIO
from .utils.config import Config
from .extensions import db, socketio
from flasgger import Swagger

import threading
import json
import select
from flask_cors import CORS


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Init extensions
    db.init_app(app)
    socketio.init_app(app)

    swagger = Swagger(app, parse=True)

    # Registeer blueprints
    from .routes.index import index_bp
    from .routes.categories import categories_bp
    from .routes.tasks import tasks_bp
    from .routes.task_assignments import task_assignments_bp

    app.register_blueprint(index_bp)
    app.register_blueprint(categories_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(task_assignments_bp)

    return app
