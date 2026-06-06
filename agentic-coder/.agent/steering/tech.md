## Tech Stack

- **Python 3.11** — minimum version
- **Flask 3.x** — use app factory pattern (`create_app()`)
- **SQLAlchemy 2.x** — use `db.session.get(Model, id)` NOT deprecated `Model.query.get(id)`
- **Flask-SQLAlchemy** — declarative base via `db = SQLAlchemy()`
- **pytest + pytest-flask** — test framework; use `app` fixture from conftest.py
- **Alembic** (via Flask-Migrate) — for schema migrations

## SQLAlchemy 2.x Patterns

```python
# CORRECT — SQLAlchemy 2.x
user = db.session.get(User, user_id)
users = db.session.execute(db.select(User).where(User.active == True)).scalars().all()
db.session.add(new_user)
db.session.commit()

# WRONG — deprecated in 2.x, do not use
user = User.query.get(user_id)
users = User.query.filter_by(active=True).all()
```

## Flask App Factory Pattern

```python
# app/backend/app_factory.py
def create_app(config_name="development"):
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    db.init_app(app)
    from .routes import api_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    return app
```

## Prohibited Patterns

- No circular imports — models.py must NOT import from routes.py
- No synchronous file I/O inside async contexts
- No hardcoded localhost URLs in application code (use config values)
- No `import *` — always explicit imports
- No `print()` in application code — use `app.logger` or Python `logging`
- No raw SQL strings — always use SQLAlchemy ORM or `text()` with bound params

## Test Configuration (conftest.py)

```python
@pytest.fixture(scope="function")
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

@pytest.fixture(scope="function")
def client(app):
    return app.test_client()

@pytest.fixture(scope="function")
def db_session(app):
    with app.app_context():
        yield db.session
```
