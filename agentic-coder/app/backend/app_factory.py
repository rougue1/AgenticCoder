from flask import Flask
from .config import config
from .extensions import db
from .routes import api_bp

def create_app(config_name="development"):
    app = Flask(__name__)
    app.config.from_object(f'app.backend.config.{config[config_name]}')
    db.init_app(app)
    app.register_blueprint(api_bp, url_prefix="/api")
    return app