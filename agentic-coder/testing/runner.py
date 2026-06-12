import json
import os
import re
import shlex
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
# ADAPTIVE THREE-PHASE EXECUTION (stack.md-driven)
# ==========================================
#
# All command syntax for targeted and file-level runs comes from the
# stack.md command templates ({file} / {test} placeholders). Nothing in
# this section names a test runner. When stack.md is absent the healer
# loop never calls these functions — it runs the full suite via run_tests().


def resolve_test_cwd(root_dir: Path) -> Path:
    """
    Working directory for stack.md template commands. Mirrors the suite
    runner's rule: run.json test_cwd relative to project root, defaulting
    to the project root itself.
    """
    run_config = load_run_config(root_dir)
    test_cwd = run_config.get("test_cwd") if run_config else None
    return (root_dir / test_cwd) if test_cwd else root_dir


def run_targeted_tests(
    root_dir: Path,
    conda_env: str,
    test_file: str,
    test_functions: list[str],
    stack_profile: dict,
) -> tuple[int, str]:
    """
    Phase 1 — runs only the named test functions in test_file, one
    targeted_test_command invocation per function. test_file is relative
    to the project root. Falls back to a whole-file run when the profile
    has no targeted template or no function names were supplied.
    """
    template = stack_profile.get("targeted_test_command")
    if not template or not test_functions:
        return run_file_tests(root_dir, conda_env, test_file, stack_profile)

    cwd = resolve_test_cwd(root_dir)
    rel_file = os.path.relpath(root_dir / test_file, cwd)

    results = []
    for func in test_functions:
        code, out = _run_template_command(template,
                                          conda_env,
                                          cwd,
                                          file=rel_file,
                                          test=func)
        results.append((f"targeted {func}", code, out))
    return aggregate_results(results)


def run_file_tests(
    root_dir: Path,
    conda_env: str,
    test_file: str,
    stack_profile: dict,
) -> tuple[int, str]:
    """
    Phase 2 — runs the entire test file via the file_test_command template.
    test_file is relative to the project root.
    """
    template = stack_profile.get("file_test_command")
    if not template:
        # Builder should never route here without a template; degrade to the
        # authoritative suite run rather than failing the phase outright.
        return run_tests(root_dir / "app", conda_env, root_dir)

    cwd = resolve_test_cwd(root_dir)
    rel_file = os.path.relpath(root_dir / test_file, cwd)
    return _run_template_command(template, conda_env, cwd, file=rel_file)


def count_failures(exit_code: int, test_output: str) -> int:
    """
    Best-effort failure count from runner output, used for the healer's
    regression (rollback) comparison. Heuristics only — these are generic
    summary-line shapes shared by most runners, not runner-specific syntax.
    Consistency between two runs of the same command matters more here than
    absolute accuracy. A nonzero exit with no parseable count counts as 1.
    """
    if exit_code == 0:
        return 0

    for pattern in (r'(\d+)\s+fail(?:ed|ures?)', r'fail(?:ed|ures?)\D{0,3}(\d+)'):
        matches = re.findall(pattern, test_output, re.IGNORECASE)
        if matches:
            # Last match — summary lines come after per-test detail.
            return max(int(matches[-1]), 1)

    marker_lines = sum(
        1 for line in test_output.splitlines()
        if re.search(r'\bFAIL(?:ED)?\b|\bERRORS?\b', line))
    return max(marker_lines, 1)


# Fallback test-declaration patterns covering common stacks. Stack names are
# permitted here only because this is a fallback path — stack.md's
# test_function_pattern key overrides the whole set when present.
_FALLBACK_TEST_PATTERNS = (
    r'(?:async\s+)?def\s+(test_\w+)\s*\(',
    r'\bfunc\s+(Test\w+)\s*\(',
    r'\bfn\s+(test_\w+)\s*\(',
    r'(?:^|[^\w.])(?:it|test)\(\s*[\'"]([^\'"]+)[\'"]',
)


def get_test_declaration_patterns(stack_profile: dict | None) -> tuple[str, ...]:
    """Regexes (one capture group = test name) identifying test declarations."""
    if stack_profile and stack_profile.get("test_function_pattern"):
        return (stack_profile["test_function_pattern"],)
    return _FALLBACK_TEST_PATTERNS


def extract_test_function_names(
    text: str,
    stack_profile: dict | None = None,
) -> list[str]:
    """All test names declared in text, in order of appearance, deduplicated."""
    names = []
    seen = set()
    for pattern in get_test_declaration_patterns(stack_profile):
        for match in re.finditer(pattern, text, re.MULTILINE):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def extract_new_test_functions(
    block_content: str,
    stack_profile: dict | None = None,
) -> list[str]:
    """
    Test names the Surgeon just wrote: declared in REPLACE segments of a
    file chunk but absent from its SEARCH anchors (a SEARCH anchor may
    legitimately contain an existing test's declaration as context).
    """
    search_parts, replace_parts = [], []
    for chunk in block_content.split("<<<<<<< SEARCH")[1:]:
        if "=======" not in chunk:
            continue
        search_raw, rest = chunk.split("=======", 1)
        replace_parts.append(rest.split(">>>>>>> REPLACE", 1)[0])
        search_parts.append(search_raw)

    existing = set(
        extract_test_function_names("\n".join(search_parts), stack_profile))
    return [
        name for name in extract_test_function_names("\n".join(replace_parts),
                                                     stack_profile)
        if name not in existing
    ]


def failing_test_names(
    test_output: str,
    candidates: list[str],
    strict: bool = False,
) -> list[str]:
    """
    Which of the candidate test names failed, judged from runner output.
    Primary signal: the name appears on a line carrying a failure marker.
    Non-strict mode falls back to any mention of the name in the output —
    only safe for small targeted runs; verbose suite output mentions
    passing tests too, so suite/file callers must pass strict=True.
    """
    failure_line = re.compile(r'FAIL|ERROR|✗|✕|×|not ok|panic', re.IGNORECASE)
    failing = []
    for line in test_output.splitlines():
        if not failure_line.search(line):
            continue
        for name in candidates:
            if name not in failing and _name_mentioned(name, line):
                failing.append(name)

    if not failing and not strict:
        failing = [n for n in candidates if _name_mentioned(n, test_output)]
    return failing


def _name_mentioned(name: str, text: str) -> bool:
    """
    Whole-name occurrence check: the name must not be flanked by identifier
    characters, so 'test_add' never matches inside 'test_add_two'. Names from
    string-based declarations (it('...')) may contain regex metacharacters,
    hence re.escape rather than \\b anchors (which fail on non-word edges).
    """
    for match in re.finditer(re.escape(name), text):
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if (not (before.isalnum() or before == "_")
                and not (after.isalnum() or after == "_")):
            return True
    return False


def extract_function_source(
    file_text: str,
    name: str,
    stack_profile: dict | None = None,
) -> str:
    """
    Extracts one test function's source by name: from its declaration line
    (plus any decorator/attribute lines directly above) to the line before
    the next test declaration, or EOF. Declaration shape comes from the
    same patterns used for name extraction, so this stays stack-agnostic.
    """
    patterns = get_test_declaration_patterns(stack_profile)
    lines = file_text.splitlines()

    start = None
    for i, line in enumerate(lines):
        for pattern in patterns:
            match = re.search(pattern, line)
            if match and match.group(1) == name:
                start = i
                break
        if start is not None:
            break
    if start is None:
        return ""

    end = len(lines)
    for j in range(start + 1, len(lines)):
        found_next = False
        for pattern in patterns:
            match = re.search(pattern, lines[j])
            if match and match.group(1) != name:
                found_next = True
                break
        if found_next:
            end = j
            break

    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1

    return "\n".join(lines[start:end]).rstrip()


def _run_template_command(
    template: str,
    conda_env: str,
    cwd: Path,
    file: str | None = None,
    test: str | None = None,
) -> tuple[int, str]:
    """
    Substitutes {file}/{test} placeholders into a stack.md command template
    and executes it. Plain replace (not str.format) so literal braces in a
    template never raise.
    """
    cmd_str = template
    if file is not None:
        cmd_str = cmd_str.replace("{file}", file)
    if test is not None:
        cmd_str = cmd_str.replace("{test}", test)

    cmd = shlex.split(cmd_str)
    print(f"[RUNNER] Running via stack.md template: {' '.join(cmd)}")
    return run_in_env(
        cmd,
        conda_env,
        cwd=cwd,
        extra_env={"CI": "true"},
        timeout=300,
    )


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
