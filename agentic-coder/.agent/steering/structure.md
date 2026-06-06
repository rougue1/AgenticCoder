## Directory Layout

```
app/
├── backend/
│   ├── __init__.py
│   ├── app_factory.py        ← Flask create_app() factory
│   ├── config.py             ← Config classes (Development, Testing, Production)
│   ├── extensions.py         ← Flask extension instances (db, migrate, bcrypt)
│   ├── models.py             ← SQLAlchemy ORM models
│   ├── routes/
│   │   ├── __init__.py
│   │   └── api.py            ← API Blueprint
│   ├── services/             ← Business logic (no Flask imports)
│   │   └── __init__.py
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py       ← pytest fixtures (app, client, db_session)
│       └── test_models.py    ← Unit tests per module
└── frontend/                 ← Optional — Vite + React
    ├── src/
    └── package.json
```

## Module Boundary Rules

- `models.py` → MAY import from `extensions.py` only
- `routes/` → MAY import from `models.py`, `services/`, `extensions.py`
- `services/` → MAY import from `models.py`, `extensions.py`
- `services/` → MUST NOT import from `routes/`
- `models.py` → MUST NOT import from `routes/` or `services/`
- `tests/` → MAY import from anywhere in `backend/`

## Required **init**.py Files

Every Python package directory MUST have an `__init__.py`:

- `app/backend/__init__.py`
- `app/backend/routes/__init__.py`
- `app/backend/services/__init__.py`
- `app/backend/tests/__init__.py`

## Naming Conventions

- Model classes: PascalCase singular (`User`, `Post`, `OrderItem`)
- Database tables: snake_case plural (`users`, `posts`, `order_items`)
- Route files: snake_case (`user_routes.py`, `auth_routes.py`)
- Service files: snake_case matching model (`user_service.py`)
- Test files: `test_` prefix matching module (`test_models.py`, `test_routes.py`)
- Config classes: PascalCase + `Config` suffix (`DevelopmentConfig`, `TestingConfig`)

## Test File Co-location Rule

Test files live in `app/backend/tests/` and mirror the module structure:

- `app/backend/models.py` → `app/backend/tests/test_models.py`
- `app/backend/routes/api.py` → `app/backend/tests/test_api.py`
- `app/backend/services/user_service.py` → `app/backend/tests/test_user_service.py`
