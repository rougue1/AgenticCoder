import json
import os
from pathlib import Path
from tools.conda import run_in_env

# ==========================================
# PUBLIC INTERFACE
# ==========================================


def run_tests(
    app_dir: Path,
    conda_env: str,
    root_dir: Path | None = None,
) -> tuple[int, str]:
    """
    Main test runner entry point.

    Primary path: when root_dir is provided, loads .agent/run.json and executes
    the stack-declared test_command in test_cwd. This is the authoritative path
    for all projects that have completed steering generation.

    Legacy fallback (when run.json is absent or root_dir is not provided): detects
    project structure and dispatches to tiered pytest/vitest execution.
    Retained for one milestone, then removed in a cleanup pass.

    Returns (exit_code, combined_output).
    exit_code 0 = all passing tiers green.
    exit_code non-zero = at least one required tier failed.
    """
    # ── Primary: run.json-driven execution ──
    if root_dir is not None:
        run_config = load_run_config(root_dir)
        if run_config is not None:
            return _run_from_config(run_config, app_dir, conda_env, root_dir)

    # ── Legacy fallback: tiered structure detection ──
    print("[RUNNER] No run.json found — using legacy structure detection.")
    backend_dir = app_dir / "backend"
    frontend_dir = app_dir / "frontend"

    results = []

    if backend_dir.exists():
        # ── Tier 1: Unit tests ──
        unit_code, unit_out = run_unit_tests(app_dir, conda_env)
        results.append(("unit", unit_code, unit_out))

        # ── Tier 2: Integration tests (only if app_factory exists) ──
        app_factory_exists = ((backend_dir / "app_factory.py").exists()
                              or (backend_dir / "app.py").exists()
                              or (backend_dir / "__init__.py").exists())
        if app_factory_exists:
            int_code, int_out = run_integration_tests(app_dir, conda_env)
            results.append(("integration", int_code, int_out))
        else:
            print(
                "[RUNNER] Skipping integration tests — app factory not found yet."
            )

        # ── Tier 3: Frontend tests (warn only, non-blocking) ──
        if frontend_dir.exists() and (frontend_dir / "package.json").exists():
            fe_code, fe_out = run_frontend_tests(frontend_dir, conda_env)
            results.append(("frontend", fe_code, fe_out))

    else:
        # ── Flat layout fallback ──
        print(
            "[RUNNER] Flat project structure detected — running pytest from app root."
        )
        code, out = _run_pytest(app_dir, conda_env, test_path=None)
        results.append(("pytest", code, out))

    return aggregate_results(results)


def run_unit_tests(app_dir: Path, conda_env: str) -> tuple[int, str]:
    """
    Runs pytest unit tests from app/backend/tests/.
    These tests must NOT require a running Flask server.
    Skips gracefully if the tests directory doesn't exist yet.
    """
    tests_dir = app_dir / "backend" / "tests"

    if not tests_dir.exists():
        print(
            "[RUNNER] No backend/tests/ directory found — skipping unit tests."
        )
        return 0, "No unit tests directory found."

    if not any(tests_dir.rglob("test_*.py")):
        print(
            "[RUNNER] No test_*.py files found in backend/tests/ — skipping.")
        return 0, "No test files found."

    print("[RUNNER] Running unit tests...")
    return _run_pytest(app_dir, conda_env, test_path="backend/tests/")


def run_integration_tests(app_dir: Path, conda_env: str) -> tuple[int, str]:
    """
    Runs pytest integration tests. These may spin up a Flask test client
    via pytest-flask fixtures. Requires app_factory.py or equivalent.

    Separated from unit tests so a missing app factory doesn't block
    unit test execution on early tasks.
    """
    integration_dir = app_dir / "backend" / "tests" / "integration"

    if not integration_dir.exists():
        print(
            "[RUNNER] No integration/ subfolder — unit suite is authoritative."
        )
        return 0, "No dedicated integration directory — unit tier covers full scope."

    if not any(integration_dir.rglob("test_*.py")):
        print("[RUNNER] No integration test files found — skipping.")
        return 0, "No integration test files found."

    print("[RUNNER] Running integration tests...")
    return _run_pytest(app_dir,
                       conda_env,
                       test_path="backend/tests/integration/")


def run_frontend_tests(frontend_dir: Path, conda_env: str) -> tuple[int, str]:
    """
    Runs vitest frontend test suite.
    Non-blocking — frontend failures emit a warning but do not fail the task cycle.
    Uses MSW (Mock Service Worker) pattern — no real Flask server required.
    Skips if vitest is not in package.json.
    """
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        return 0, "No package.json found."

    try:
        import json
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})
        deps = {
            **pkg.get("dependencies", {}),
            **pkg.get("devDependencies", {})
        }
        if "vitest" not in deps and "test" not in scripts:
            print(
                "[RUNNER] vitest not in package.json — skipping frontend tests."
            )
            return 0, "vitest not configured."
    except Exception:
        pass

    print("[RUNNER] Running frontend vitest suite...")
    code, output = run_in_env(
        ["npx", "vitest", "run", "--reporter=verbose"],
        conda_env,
        cwd=frontend_dir,
        extra_env={"CI": "true"},
        timeout=120,
    )

    if code != 0:
        print(
            f"[RUNNER] Frontend tests failed (non-blocking):\n{output[-500:]}")

    return code, output


def aggregate_results(results: list[tuple[str, int, str]]) -> tuple[int, str]:
    """
    Combines results from multiple test tiers into a single (exit_code, output).

    Tier blocking rules:
        - 'unit' failures:        BLOCKING — fails the task cycle
        - 'integration' failures: BLOCKING — fails the task cycle
        - 'pytest' failures:      BLOCKING — fails the task cycle
        - 'frontend' failures:    NON-BLOCKING — logged as warning only

    Returns (0, combined_output) only if all blocking tiers pass.
    """
    combined_output_parts = []
    final_code = 0

    non_blocking_tiers = {"frontend"}

    for tier_name, code, output in results:
        header = f"\n{'─'*40}\n[{tier_name.upper()} TESTS] exit={code}\n{'─'*40}\n"
        combined_output_parts.append(header + output)

        if code != 0:
            if tier_name in non_blocking_tiers:
                print(
                    f"[RUNNER] {tier_name} tests failed (non-blocking warning)."
                )
            else:
                final_code = code

    combined_output = "\n".join(combined_output_parts)
    return final_code, combined_output


def load_run_config(root_dir: Path) -> dict | None:
    """
    Loads .agent/run.json and returns the parsed dict if valid.
    Returns None if the file is absent, unparseable, or lacks a non-empty
    test_command array. Public so the orchestrator and conda bootstrap
    can read test_file_glob and bootstrap_packages without re-parsing.
    """
    run_json = root_dir / ".agent" / "run.json"
    if not run_json.exists():
        return None
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
        if isinstance(data.get("test_command"), list) and data["test_command"]:
            return data
        print("[RUNNER] run.json found but 'test_command' is missing or empty.")
        return None
    except Exception as e:
        print(f"[RUNNER] Failed to parse run.json: {e}")
        return None


# ==========================================
# PRIVATE HELPERS
# ==========================================


def _run_pytest(
    app_dir: Path,
    conda_env: str,
    test_path: str | None,
) -> tuple[int, str]:
    """
    Runs pytest inside the conda environment from app_dir.
    Uses -v for verbose output and --tb=short for concise tracebacks
    that are easier for the Healer model to parse.
    """
    cmd = [
        "pytest",
        "--tb=short",
        "-v",
        "--no-header",
    ]
    if test_path:
        cmd.append(test_path)

    return run_in_env(
        cmd,
        conda_env,
        cwd=app_dir,
        extra_env={"CI": "true"},
        timeout=300,
    )


def _run_from_config(
    run_config: dict,
    app_dir: Path,
    conda_env: str,
    root_dir: Path,
) -> tuple[int, str]:
    """
    Executes the test suite using the command and working directory declared
    in run.json. test_cwd is resolved relative to root_dir; defaults to
    root_dir when the key is absent.
    """
    cmd = run_config["test_command"]
    test_cwd_str = run_config.get("test_cwd")
    cwd = (root_dir / test_cwd_str) if test_cwd_str else root_dir

    print(f"[RUNNER] Running via run.json: {' '.join(cmd)}")
    return run_in_env(
        cmd,
        conda_env,
        cwd=cwd,
        extra_env={"CI": "true"},
        timeout=300,
    )
