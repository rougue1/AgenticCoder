import subprocess
from pathlib import Path


def verify_syntax(file_path: Path, conda_env: str) -> bool:
    """
    Runs a lightweight syntax check on a file after it has been written.
    Called by apply_search_replace_block after every successful patch write.

    Python:  python -m py_compile <file>  — catches SyntaxError, IndentationError
    JS/TS:   node --check <file>          — catches parse errors

    Returns True if syntax is valid, False if check fails.
    Does NOT raise — returns False so callers can decide to restore snapshot.
    """
    if not file_path.exists():
        return True  # Nothing to check

    suffix = file_path.suffix.lower()

    if suffix == ".py":
        result = subprocess.run(
            [
                "conda", "run", "-n", conda_env, "python", "-m", "py_compile",
                str(file_path)
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"  [SYNTAX ERROR] {file_path.name}:\n{result.stderr.strip()}")
            return False
        return True

    if suffix in {".js", ".ts", ".mjs", ".cjs"}:
        result = subprocess.run(
            [
                "conda", "run", "-n", conda_env, "node", "--check",
                str(file_path)
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"  [SYNTAX ERROR] {file_path.name}:\n{result.stderr.strip()}")
            return False
        return True

    # Unsupported file type — skip check, assume valid
    return True


def validate_imports(file_path: Path, conda_env: str) -> list[str]:
    """
    Attempts a dry-run import of a Python module to catch missing packages
    before the test runner tries to collect them.

    Runs: python -c "import <module_name>"
    Returns list of import error messages. Empty list = all imports resolved.

    Only runs on .py files. Skips __init__.py files (they often import from
    siblings that aren't yet complete).
    """
    if not file_path.exists() or file_path.suffix != ".py":
        return []

    if file_path.name == "__init__.py":
        return []

    # Extract top-level import names from the file
    import re
    content = file_path.read_text(encoding="utf-8")
    import_lines = re.findall(r'^(?:import|from)\s+([\w.]+)', content,
                              re.MULTILINE)

    errors = []
    seen = set()

    for module_name in import_lines:
        top_level = module_name.split(".")[0]
        if top_level in seen:
            continue
        seen.add(top_level)

        # Skip stdlib and known internal modules
        if top_level in _STDLIB_MODULES:
            continue

        result = subprocess.run(
            [
                "conda", "run", "-n", conda_env, "python", "-c",
                f"import {top_level}"
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            errors.append(
                f"Cannot import '{top_level}': {result.stderr.strip()[:120]}")

    return errors


def check_package_exists(package_name: str, conda_env: str) -> bool:
    """
    Returns True if a package is importable in the conda environment.
    Used by tools/deps.py before attempting install to avoid redundant ops.
    """
    result = subprocess.run(
        [
            "conda", "run", "-n", conda_env, "python", "-c",
            f"import {package_name}"
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# Common stdlib module names to skip during import validation
_STDLIB_MODULES = {
    "os",
    "sys",
    "re",
    "json",
    "time",
    "math",
    "random",
    "datetime",
    "pathlib",
    "subprocess",
    "urllib",
    "http",
    "collections",
    "itertools",
    "functools",
    "typing",
    "abc",
    "io",
    "string",
    "struct",
    "copy",
    "hashlib",
    "hmac",
    "base64",
    "uuid",
    "enum",
    "dataclasses",
    "contextlib",
    "warnings",
    "logging",
    "traceback",
    "inspect",
    "ast",
    "dis",
    "gc",
    "weakref",
    "threading",
    "multiprocessing",
    "asyncio",
    "socket",
    "ssl",
    "email",
    "html",
    "xml",
    "csv",
    "configparser",
    "argparse",
    "shutil",
    "tempfile",
    "glob",
    "fnmatch",
    "stat",
    "platform",
    "signal",
    "queue",
    "heapq",
    "bisect",
    "array",
    "decimal",
    "fractions",
    "statistics",
    "unittest",
    "textwrap",
    "difflib",
    "pprint",
    "reprlib",
    "numbers",
}
