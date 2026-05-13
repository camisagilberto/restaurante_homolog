from flask import Flask

from .config import Config
from .db import init_db
from .routes.admin import admin_bp
from .routes.client import client_bp
from .routes.kitchen import kitchen_bp
from .security import csrf_protect, init_security, inject_globals
from .utils import format_currency


def create_app(config_object: type[Config] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object or Config)

    init_security(app)
    init_db(app)

    app.register_blueprint(client_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(kitchen_bp)

    app.jinja_env.filters['currency'] = format_currency
    app.before_request(csrf_protect)
    app.context_processor(inject_globals)
    return app
