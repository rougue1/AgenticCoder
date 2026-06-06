## Output Format Rules (NEVER violate these)

- NEVER wrap SEARCH or REPLACE content in ``` code fences of any kind
- NEVER use `python, `typescript, `bash, or bare ` inside SEARCH/REPLACE blocks
- ALWAYS prefix every file edit with exactly: ### FILE: path/relative/to/project/root
- SEARCH content must be character-for-character identical to what is in the file
- Include at least 3-5 surrounding lines in SEARCH blocks to ensure anchor uniqueness
- NEVER truncate, ellipsize (...), or summarize code inside REPLACE blocks
- For NEW files: use an empty SEARCH block (nothing between <<<<<<< SEARCH and =======)
- NEVER rewrite an entire existing file — always use targeted SEARCH/REPLACE patches

## General Conventions

- Use snake_case for all Python identifiers (variables, functions, modules)
- Use UPPER_SNAKE_CASE for module-level constants
- Use PascalCase for class names
- Use absolute imports within the project (from app.backend.models import User)
- All database operations must occur within a Flask application context
- Commit database sessions explicitly — never rely on autocommit
- Use f-strings for string formatting (not .format() or %)
- All functions must have type annotations on parameters and return types
- Maximum line length: 100 characters

## Error Handling

- Catch specific exception types — never bare `except:`
- Log exceptions with context before re-raising or returning error responses
- Use Flask abort() for HTTP errors in route handlers
- Return (data, status_code) tuples from route handlers — never raw dicts

## Testing Conventions

- Test file names: test\_<module_name>.py
- Test function names: test*<what_it_does>*<expected_outcome>
- One assert per logical concern (multiple asserts per test are fine if they test one concept)
- Use pytest fixtures for shared setup — never setUp/tearDown
- All database fixtures must use scope="function" for test isolation
- Assert ORM field values only AFTER db.session.commit()
