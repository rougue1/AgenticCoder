## Architecture Overview
```
frontend <-> (Flask REST API) <-> (SQLite/PostgreSQL)
          |
          |-- auth_routes.py
          |-- task_routes.py
          |-- tag_routes.py
          |-- services/
              |-- auth_service.py
              |-- task_service.py
              |-- tag_service.py
          |-- models.py
          |-- schemas.py
          |-- tests/
              |-- conftest.py
              |-- test_auth.py
              |-- test_tasks.py
              |-- test_tags.py
```

## Technology Stack
- Language: Python 3.11
- Framework: Flask 3.x (REST API)
- Database: SQLite for development, PostgreSQL for production
- Auth: JWT with Flask-JWT-Extended
- Hashing: bcrypt via Flask-Bcrypt
- ORM: SQLAlchemy 2.x
- Input Validation: marshmallow
- Rate Limiting: Flask-Limiter
- Testing: pytest + pytest-flask + pytest-cov
Justification: Flask's flexibility and ecosystem tools provide optimal balance between performance, security, and developer productivity.

## Directory Structure
```
app/
  backend/
    __init__.py
    models.py
    schemas.py
    auth_routes.py
    task_routes.py
    tag_routes.py
    services/
      auth_service.py
      task_service.py
      tag_service.py
    config.py
    extensions.py
    create_app.py
  tests/
    __init__.py
    conftest.py
    test_auth.py
    test_tasks.py
    test_tags.py
```

## Data Models
### User
- id: integer (PK)
- email: string (unique, not null)
- password_hash: string (not null)
- created_at: datetime (default now())
- is_active: boolean (default true)

### Task
- id: integer (PK)
- user_id: integer (FK to User.id)
- title: string (not null)
- description: text
- status: enum (todo, in_progress, done)
- priority: enum (low, medium, high)
- due_date: date
- created_at: datetime (default now())
- updated_at: datetime (on update now())

### Tag
- id: integer (PK)
- name: string (unique, not null)
- color: string (hex code format)

### TaskTag
- task_id: integer (FK to Task.id)
- tag_id: integer (FK to Tag.id)

## API Contracts
### Auth Endpoints
1. POST /api/auth/register
   - Request: {email, password}
   - Response: {success, data: {user}, message}
2. POST /api/auth/login
   - Request: {email, password}
   - Response: {success, data: {access_token}, message}
3. GET /api/auth/me
   - Auth Required
   - Response: {success, data: {user}, message}
4. POST /api/auth/logout
   - Auth Required
   - Response: {success, data: {}, message}

### Task Endpoints
1. GET /api/tasks
   - Auth Required
   - Query Params: status, priority, tag
   - Response: paginated list of tasks
2. POST /api/tasks
   - Auth Required
   - Request: {title, description, status, priority, due_date, tags}
3. GET /api/tasks/<id>
   - Auth Required
4. PUT /api/tasks/<id>
   - Auth Required
5. DELETE /api/tasks/<id>
   - Auth Required

### Tag Endpoints
1. GET /api/tags
   - Auth Required
2. POST /api/tags
   - Auth Required
3. DELETE /api/tags/<id>
   - Auth Required

## Key Dependencies
- Flask==3.x
- SQLAlchemy==2.x
- marshmallow==3.x
- Flask-JWT-Extended==4.x
- Flask-Bcrypt==1.x
- Flask-Migrate==3.x
- Flask-Limiter==2.x
- pytest==8.x
- pytest-flask==1.x
- pytest-cov==4.x