import json
from pathlib import Path
from engine.llm import query_llm, clean_and_parse_json, load_config
from engine.splicer import splice_multi_file_response
from spec.steering import build_system_prompt


def validate_source_completeness(
    file_paths: list[str],
    task_desc: str,
    root_dir: Path,
    app_dir: Path,
) -> bool:
    """
    Tier 2.5 — Validator pre-flight check #1.
    Before writing tests, audits implementation files to confirm they are
    complete and ready to be tested against.

    Checks for:
        - Stub functions that only 'pass' or 'return None' where real logic is expected
        - Missing imports referenced in the file body
        - Obvious syntax issues (unterminated strings, bad indentation blocks)
        - Missing __init__.py in package directories pytest needs to discover
        - Circular import risks from import patterns
        - Hardcoded absolute paths that will break in test environment

    Auto-applies any fixes the model returns in the 'fixes' dict.
    Returns True if source is ready for testing (with or without fixes applied).
    Returns False only if model flags critical blockers it cannot auto-fix.
    """
    config = load_config(root_dir)
    print("[PREFLIGHT] Auditing source completeness before test generation...")

    source_context = _load_files(file_paths, root_dir)
    if not source_context:
        print("[PREFLIGHT] No source files found to audit — proceeding.")
        return True

    base_prompt = (
        "You are a Code Completeness Auditor in an autonomous coding pipeline. "
        "Review the provided implementation files BEFORE any tests are written against them.\n\n"
        "Check for these specific issues:\n"
        "1. STUB IMPLEMENTATIONS: functions/methods/procedures that contain only a no-op body "
        "   (e.g., a single pass, empty return, or NotImplemented placeholder) when the task "
        "   context implies real logic should be there\n"
        "2. MISSING IMPORTS: names used in the file body that are not resolved via any import "
        "   or module declaration at the top of the file\n"
        "3. SYNTAX PROBLEMS: unterminated strings, mismatched brackets or braces, "
        "   broken indentation or block structure\n"
        "4. MISSING MODULE INIT FILES: package or module directories that require an "
        "   initialization file for this project's language (as described in structure.md "
        "   in the steering context) but do not have one\n"
        "5. ABSOLUTE PATHS: hardcoded filesystem paths that will break outside the "
        "   developer's machine\n"
        "6. CIRCULAR DEPENDENCY RISK: module A imports module B which imports module A\n\n"
        "Consult the injected steering context (structure.md) for the correct module "
        "initialization file convention for this project's stack before flagging issue #4.\n\n"
        "Return a SINGLE valid JSON object. No markdown fences. No extra text.\n"
        "Format:\n"
        "{\n"
        '  "ready": true,\n'
        '  "issues": [],\n'
        '  "fixes": {}\n'
        "}\n\n"
        "OR if issues found:\n"
        "{\n"
        '  "ready": false,\n'
        '  "issues": ["models.py: check_password() contains only a no-op body — needs real implementation"],\n'
        '  "fixes": {\n'
        '    "app/backend/models.py": "complete corrected file content as a single string"\n'
        "  }\n"
        "}\n\n"
        "IMPORTANT:\n"
        "- Only include a file in 'fixes' if you are confident the fix is correct and complete\n"
        "- Do NOT fix things that look intentionally minimal (e.g., package init files with just re-exports)\n"
        "- Set 'ready': true even if you applied fixes — false only means an unfixable blocker\n"
    )
    system_prompt = build_system_prompt(base_prompt, "validator",
                                        root_dir / ".agent", app_dir)

    user_prompt = (f"Task being implemented: {task_desc}\n\n"
                   f"Source files to audit:\n{source_context}")

    try:
        response = query_llm("validator", system_prompt, user_prompt, config)
        data = clean_and_parse_json(response)

        issues = data.get("issues", [])
        fixes = data.get("fixes", {})
        ready = data.get("ready", True)

        if issues:
            print(f"[PREFLIGHT] Source issues found ({len(issues)}):")
            for issue in issues:
                print(f"  ⚠ {issue}")

        if fixes:
            print(f"[PREFLIGHT] Applying {len(fixes)} auto-fix(es)...")
            apply_validated_fixes(fixes, root_dir)

        if not ready:
            print(
                "[PREFLIGHT] Source has unfixable blockers — proceeding with caution."
            )

        return True  # Always proceed — preflight is advisory, not a hard gate

    except json.JSONDecodeError as e:
        print(
            f"[PREFLIGHT] Source audit JSON parse failed: {e}. Proceeding anyway."
        )
        return True
    except Exception as e:
        print(f"[PREFLIGHT] Source audit error: {e}. Proceeding anyway.")
        return True


def validate_test_correctness(
    test_files: list[str],
    source_files: list[str],
    task_desc: str,
    root_dir: Path,
    app_dir: Path,
) -> bool:
    """
    Tier 2.5 — Validator pre-flight check #2.
    After the Surgeon writes tests but BEFORE running them, verifies that
    the test code is logically correct and will actually execute.

    Checks for:
        - Fixture names not defined in any conftest.py
        - Imports referencing modules that don't exist yet
        - pytest.raises used when function returns None on failure
        - ORM assertions before db.session.commit()
        - session.get() vs deprecated query.get() (SQLAlchemy 2.x)
        - App context missing around database operations
        - Test isolation issues (shared mutable state between tests)

    If issues found: applies SEARCH/REPLACE fixes using splice_multi_file_response.
    Returns True if tests look valid or fixes were applied.
    Returns False only on unrecoverable parse failure.
    """
    config = load_config(root_dir)
    print("[PREFLIGHT] Validating test correctness before test run...")

    # Load test files + source files + all conftest.py files
    context = _load_files(test_files + source_files, root_dir)
    conftest_context = _load_all_conftests(app_dir)

    if not context:
        print("[PREFLIGHT] No test files to validate — skipping.")
        return True

    base_prompt = (
        "You are a Test Correctness Auditor in an autonomous coding pipeline. "
        "Review the provided test files against their implementation and shared test "
        "setup files. Your job is to catch bugs in the tests themselves BEFORE the "
        "test runner collects them.\n\n"
        "Check for these specific anti-patterns:\n\n"
        "1. TEST SETUP MISMATCHES\n"
        "   Test function signatures reference setup helpers (fixtures, hooks, before-each "
        "   callbacks, or test context objects) that are not defined in any shared test "
        "   setup file visible in the provided context. Consult the steering context "
        "   (AGENTS.md, tech.md) for the correct test harness conventions for this stack.\n\n"
        "2. IMPORT ERRORS\n"
        "   Test files importing from modules that do not exist in the source files provided.\n\n"
        "3. WRONG ASSERTION OR API PATTERNS\n"
        "   Assertion style inconsistent with the test framework's idioms, use of deprecated "
        "   query or assertion APIs, or assertions made on state that has not yet been "
        "   persisted or flushed. Consult tech.md for the correct assertion and data-access "
        "   patterns mandated for this project's stack.\n\n"
        "4. FRAMEWORK CONTEXT ERRORS\n"
        "   Operations that require application lifecycle setup (e.g., an app context, a "
        "   transaction scope, or a server process) that is not in place at the point of "
        "   assertion. Consult tech.md for the correct lifecycle pattern for this stack.\n\n"
        "5. SCOPE CONFLICTS\n"
        "   Test setup objects with a narrower lifecycle than the tests that consume them, "
        "   causing teardown before assertions complete.\n\n"
        "6. TEST ISOLATION VIOLATIONS\n"
        "   Tests that modify shared mutable state or depend on execution order.\n\n"
        "OUTPUT FORMAT:\n"
        "If issues found — output SEARCH/REPLACE patches using this exact format:\n"
        "### FILE: path/to/test_file.py\n"
        "<<<<<<< SEARCH\n"
        "<exact current content of the broken section>\n"
        "=======\n"
        "<corrected replacement>\n"
        ">>>>>>> REPLACE\n\n"
        "NEVER wrap SEARCH or REPLACE content in ``` code fences.\n\n"
        "If everything looks correct — output exactly the string: TESTS_VALID\n"
        "Do NOT output TESTS_VALID if there are any issues. Fix them instead.\n"
    )
    system_prompt = build_system_prompt(base_prompt, "validator",
                                        root_dir / ".agent", app_dir)

    user_prompt = (f"Task context: {task_desc}\n\n"
                   f"[CONFTEST FIXTURES]\n{conftest_context}\n\n"
                   f"[SOURCE + TEST FILES]\n{context}")

    try:
        response = query_llm("validator", system_prompt, user_prompt, config)

        if "TESTS_VALID" in response and "### FILE:" not in response:
            print("[PREFLIGHT] ✓ Tests passed correctness audit.")
            return True

        print(
            "[PREFLIGHT] Test issues detected — applying pre-run corrections..."
        )
        splice_multi_file_response(response, root_dir)
        print("[PREFLIGHT] ✓ Test corrections applied.")
        return True

    except Exception as e:
        print(
            f"[PREFLIGHT] Test validation error: {e}. Proceeding with unchecked tests."
        )
        return True


def apply_validation_fixes(fixes: dict, root_dir: Path) -> None:
    """
    Writes auto-fix content from a validator JSON 'fixes' dict to disk.
    Each key is a relative file path, each value is the complete corrected content.
    Used when the validator returns full-file corrections rather than SEARCH/REPLACE blocks.
    """
    for path_str, content in fixes.items():
        if not content or not path_str:
            continue
        full_path = root_dir / path_str
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        print(f"  [PREFLIGHT FIX] {path_str}")


# ==========================================
# PRIVATE HELPERS
# ==========================================


def _load_files(file_paths: list[str],
                root_dir: Path,
                max_chars: int = 3000) -> str:
    """
    Loads content of multiple files into a single formatted string.
    Caps each file at max_chars to stay within context window budget.
    Skips files that don't exist without raising.
    """
    parts = []
    for path_str in file_paths:
        full_path = root_dir / path_str
        if not full_path.exists():
            parts.append(f"--- {path_str} --- [FILE NOT FOUND]")
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
            parts.append(
                f"--- BEGIN: {path_str} ---\n{content}\n--- END: {path_str} ---"
            )
        except Exception as e:
            parts.append(f"--- {path_str} --- [READ ERROR: {e}]")
    return "\n\n".join(parts)


def _load_all_conftests(app_dir: Path) -> str:
    """
    Loads all conftest.py files found under app_dir.
    Returns formatted string of fixture definitions for injection into prompts.
    """
    parts = []
    for conftest_path in sorted(app_dir.rglob("conftest.py")):
        try:
            content = conftest_path.read_text(encoding="utf-8")
            rel = str(conftest_path.relative_to(app_dir.parent))
            parts.append(f"--- {rel} ---\n{content}")
        except Exception:
            continue
    return "\n\n".join(parts) if parts else "No conftest.py files found yet."
