import re
from pathlib import Path


def check_fixture_drift(app_dir: Path) -> list[str]:
    """
    Compares pytest fixtures USED in test files against fixtures DEFINED
    in conftest.py. Returns a list of drift warnings.

    A drift warning looks like:
        "test_models.py: 'db_session' used but not defined in any conftest.py"

    This is run before every test cycle so the Healer can be pre-warned
    about fixture mismatches before they cause test collection errors.
    Returns empty list if no issues found.
    """
    defined = parse_all_conftest_fixtures(app_dir)
    warnings = []

    # Built-in pytest fixtures that don't need to be in conftest
    builtin_fixtures = {
        "request",
        "tmp_path",
        "tmp_path_factory",
        "capsys",
        "capfd",
        "capsysbinary",
        "capfdbinary",
        "caplog",
        "monkeypatch",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "recwarn",
        "tmpdir",
        "tmpdir_factory",
        "cache",
        "mocker",
        "freezer",
    }

    for test_file in sorted(app_dir.rglob("test_*.py")):
        used = parse_test_fixture_params(test_file)
        for fixture_name in used:
            if fixture_name in builtin_fixtures:
                continue
            if fixture_name not in defined:
                rel = str(test_file.relative_to(app_dir.parent))
                warnings.append(
                    f"{rel}: '{fixture_name}' used but not defined in any conftest.py"
                )

    return warnings


def parse_all_conftest_fixtures(app_dir: Path) -> set[str]:
    """
    Parses all conftest.py files under app_dir and returns a set of
    all defined fixture names. Handles nested conftest.py files
    (e.g., app/conftest.py and app/backend/tests/conftest.py).
    """
    all_fixtures = set()
    for conftest_path in app_dir.rglob("conftest.py"):
        all_fixtures.update(parse_conftest_fixtures(conftest_path))
    return all_fixtures


def parse_conftest_fixtures(conftest_path: Path) -> set[str]:
    """
    Extracts all @pytest.fixture function names from a single conftest.py.
    Handles:
        @pytest.fixture
        @pytest.fixture()
        @pytest.fixture(scope='function')
        @pytest.fixture(scope="module", autouse=True)
    Returns set of fixture names defined in this file.
    """
    if not conftest_path.exists():
        return set()

    try:
        content = conftest_path.read_text(encoding="utf-8")
    except Exception:
        return set()

    # Match @pytest.fixture decorator (with any params) followed by def name(
    pattern = re.compile(
        r'@pytest\.fixture[^\n]*\n'  # decorator line
        r'(?:@[^\n]+\n)*'  # optional additional decorators
        r'def\s+(\w+)\s*\(',  # function definition
        re.MULTILINE,
    )

    return {match.group(1) for match in pattern.finditer(content)}


def parse_test_fixture_params(test_file: Path) -> set[str]:
    """
    Extracts all parameter names from test function signatures in a file.
    These represent fixture requests that must be satisfied by conftest.py.

    Handles:
        def test_something(app, db, client):
        def test_another(app, user_a, auth_token):
        async def test_async(app, db):
        class TestSomething:
            def test_method(self, app, db):  ← 'self' excluded automatically

    Returns set of parameter names (excluding 'self').
    """
    if not test_file.exists():
        return set()

    try:
        content = test_file.read_text(encoding="utf-8")
    except Exception:
        return set()

    # Match test function definitions and capture their parameter list
    pattern = re.compile(
        r'(?:async\s+)?def\s+test_\w+\s*\(([^)]*)\)',
        re.MULTILINE,
    )

    params = set()
    for match in pattern.finditer(content):
        param_str = match.group(1)
        for param in param_str.split(","):
            name = param.strip().split(
                ":")[0].strip()  # strip type annotations
            name = name.split("=")[0].strip()  # strip default values
            if name and name != "self" and name != "cls":
                params.add(name)

    return params


def ensure_init_files(app_dir: Path) -> None:
    """
    Ensures every Python package directory under app_dir has an __init__.py.
    pytest's default test discovery mode requires __init__.py for proper
    module resolution when tests import from application code.

    Skips:
        - node_modules/
        - __pycache__/
        - .git/
        - .venv/
        - Directories containing no .py files

    Called at the start of every task cycle before any test run.
    """
    skip_dirs = {
        "node_modules", "__pycache__", ".git", ".venv", "dist", "build",
        ".next"
    }

    for dirpath in sorted(app_dir.rglob("*")):
        if not dirpath.is_dir():
            continue

        # Skip excluded directories
        if any(part in skip_dirs for part in dirpath.parts):
            continue

        # Only add __init__.py if directory contains Python files
        has_py_files = any(dirpath.glob("*.py"))
        if not has_py_files:
            continue

        init_file = dirpath / "__init__.py"
        if not init_file.exists():
            init_file.touch()
            print(f"  [INIT] Created {init_file.relative_to(app_dir.parent)}")


def get_fixture_summary(app_dir: Path) -> str:
    """
    Returns a human-readable summary of all available fixtures and
    which conftest.py file defines them. Used for diagnostic output.
    """
    lines = []
    for conftest_path in sorted(app_dir.rglob("conftest.py")):
        fixtures = parse_conftest_fixtures(conftest_path)
        if fixtures:
            rel = str(conftest_path.relative_to(app_dir.parent))
            lines.append(f"{rel}: {', '.join(sorted(fixtures))}")
    return "\n".join(lines) if lines else "No fixtures defined yet."
