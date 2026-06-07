from flask import Flask, jsonify
from .extensions import db, migrate, bcrypt, jwt, limiter


def create_app(config_name="development"):
    app = Flask(__name__)

    if config_name == "testing":
        app.config.from_object('app.backend.config.TestingConfig')
    else:
        app.config.from_object('app.backend.config.DevelopmentConfig')

    db.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    jwt.init_app(app)
    limiter.init_app(app)

    from .models import User

    from .routes.api import api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.errorhandler(404)
    def not_found_error(error):
        return jsonify({'error': 'Not Found'}), 404

    @app.errorhandler(429)
    def rate_limit_exceeded(error):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f"Server Error: {error}")
        return jsonify({'error': 'Internal Server Error'}), 500

    return app
