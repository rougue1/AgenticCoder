from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
# from flask_migrate import Migrate
# from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
migrate = Migrate()
bcrypt = Bcrypt()
jwt = JWTManager()
limiter = Limiter(key_func=get_remote_address)

def create_app(config_name="development"):
    app = Flask(__name__)
    
    # Configure the application with the specified environment
    if config_name == "testing":
        app.config.from_object('app.backend.config.TestingConfig')
    else:
        app.config.from_object('app.backend.config.DevelopmentConfig')

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    jwt.init_app(app)
    limiter.init_app(app)

    # Import models to avoid circular imports
    from .models import User  # Example model

    # Register blueprints
    from .routes.api import api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    # Error handlers
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