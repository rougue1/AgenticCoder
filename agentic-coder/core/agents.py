import os
import re
from pathlib import Path

from engine.llm import query_llm, clean_and_parse_json, load_config, get_healer_escalation_model, query_llm_with_json_retry
from engine.splicer import splice_multi_file_response
from engine.syntax import verify_syntax
from engine.patch import restore_snapshot
from spec.steering import build_system_prompt, get_fixture_registry
from spec.sdd import read_design_doc
from testing.runner import run_tests
from core.telemetry import log_telemetry, start_timer
from tools.conda import run_in_env

# ==========================================
# TIER 1 — ARCHITECT
# ==========================================


def get_architect_plan(
    task_desc: str,
    app_dir: Path,
    root_dir: Path,
) -> dict:
    """
    Tier 1 — Architect (DeepSeek-R1-32B):
    Analyzes the current project directory tree and produces a precise
    implementation plan for the Surgeon.

    Returns dict with keys:
        'context_files':   list[str] — relative paths the Surgeon must read
        'surgeon_prompt':  str       — detailed implementation brief

    Injects:
        - Filtered directory tree (node_modules, __pycache__ excluded)
        - design.md content for architectural context
        - Steering context (AGENTS.md + structure.md)

    Exits on JSON parse failure — a missing plan means the pipeline cannot proceed.
    """
    import sys
    config = load_config(root_dir)
    print(f"[ARCHITECT] Planning implementation...")

    tree_str = _build_directory_tree(app_dir)
    design_context = read_design_doc(root_dir)

    base_prompt = (
        "You are the Lead Architect in an autonomous multi-agent software pipeline. "
        "A downstream code-generation model (the Surgeon) will implement the task. "
        "Your job is to analyze the current project state and produce a precise plan.\n\n"
        "Return a SINGLE valid JSON object with exactly two keys. "
        "No markdown fences. No text before or after the JSON.\n\n"
        "KEY 1 — 'context_files': array of strings.\n"
        "  List every relative file path (from project root, e.g. 'app/backend/models.py') "
        "  the Surgeon must read before writing code. Include:\n"
        "  - The file being modified (if it already exists)\n"
        "  - Any files that import from or are imported by the target file\n"
        "  - Shared utility/config/extensions files relevant to the task\n"
        "  - Existing test files in the same module\n"
        "  - conftest.py if task involves writing tests\n"
        "  Do NOT include: node_modules, __pycache__, .bak, binary files.\n\n"
        "KEY 2 — 'surgeon_prompt': string.\n"
        "  A complete, unambiguous implementation brief. MUST include:\n"
        "  1. EXACT file paths to create or modify (relative to project root)\n"
        "  2. Function/class/method signatures with parameter names and return types\n"
        "  3. All business logic rules and edge cases that must be handled\n"
        "  4. Integration points: how this connects to existing code\n"
        "  5. Test strategy: specific pytest test cases to write including:\n"
        "     - Happy path assertions\n"
        "     - Edge cases (empty input, None, boundary values)\n"
        "     - Expected error handling (what should raise vs return None/False)\n"
        "  6. Any specific import statements or module structure requirements\n"
        "  7. SQLAlchemy 2.x patterns to use (session.get() not query.get())\n"
        "  Do NOT tell the Surgeon to 'figure it out' — every decision must be explicit.\n"
    )

    system_prompt = build_system_prompt(base_prompt, "architect",
                                        root_dir / ".agent", app_dir)

    user_prompt = (f"Active Task: {task_desc}\n\n"
                   f"Current Directory Tree (app/):\n{tree_str}\n\n"
                   f"Project Design Document:\n{design_context}")

    # Hard constraint: retry up to 2 times with a corrective prompt before halting.
    return query_llm_with_json_retry(
        tier="architect",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        config=config,
        expected_keys=["context_files", "surgeon_prompt"],
        context_label="Architect plan",
    )


# ==========================================
# TIER 2 — SURGEON
# ==========================================


def execute_surgeon(
    plan: dict,
    task_desc: str,
    root_dir: Path,
    app_dir: Path,
) -> str:
    """
    Tier 2 — Surgeon (Qwen-2.5-Coder-32B):
    Reads all context files identified by the Architect and writes the actual
    code changes as SEARCH/REPLACE blocks prefixed with ### FILE: markers.
    Also writes parallel test code in the same response.

    Validates output length — if suspiciously short, retries once.
    """
    config = load_config(root_dir)
    print(f"[SURGEON] Writing implementation...")

    context_payload = _load_context_files(plan.get("context_files", []),
                                          root_dir)
    fixture_list = get_fixture_registry(app_dir)

    from tools.deps import get_ledger_content
    ledger = get_ledger_content(root_dir)

    base_prompt = (
        "You are an elite Software Surgeon in an autonomous multi-agent coding pipeline. "
        "You receive a task, an implementation plan, existing source code, and a dependency ledger. "
        "Your output directly modifies production files — precision is mandatory.\n\n"
        "══════════════════════════════════\n"
        "MANDATORY OUTPUT FORMAT\n"
        "══════════════════════════════════\n"
        "For EVERY file you create or modify:\n\n"
        "### FILE: path/relative/to/project/root/filename.ext\n"
        "<<<<<<< SEARCH\n"
        "<exact verbatim content that currently exists at this location in the file>\n"
        "=======\n"
        "<the new content to replace it with>\n"
        ">>>>>>> REPLACE\n\n"
        "For NEW files that do not yet exist, use an EMPTY search block:\n"
        "### FILE: path/to/new/file.py\n"
        "<<<<<<< SEARCH\n"
        "=======\n"
        "<complete file content>\n"
        ">>>>>>> REPLACE\n\n"
        "══════════════════════════════════\n"
        "CRITICAL RULES — NEVER VIOLATE\n"
        "══════════════════════════════════\n"
        "1. NEVER wrap SEARCH or REPLACE content in ``` code fences of ANY kind.\n"
        "   No ```python, no ```typescript, no bare ```. Raw code only.\n"
        "2. SEARCH content must be character-for-character identical to the file.\n"
        "   One whitespace difference causes the patch to fail.\n"
        "3. Include at least 3-5 surrounding lines in SEARCH blocks for unique anchoring.\n"
        "4. NEVER truncate, ellipsize (...), or summarize any code block.\n"
        "5. NEVER rewrite an entire existing file — use targeted patches only.\n"
        "6. Generate BOTH the feature implementation AND the test code in this single response.\n"
        "7. Test files: backend tests → app/backend/tests/test_<name>.py (pytest)\n"
        "8. Only import packages listed in the dependency ledger or Python stdlib.\n"
        "9. Use SQLAlchemy 2.x patterns: db.session.get(Model, id) not Model.query.get(id)\n"
        "10. All database assertions in tests must come AFTER db.session.commit()\n"
    )

    if fixture_list:
        base_prompt += (
            f"\n══════════════════════════════════\n"
            f"AVAILABLE PYTEST FIXTURES\n"
            f"══════════════════════════════════\n"
            f"Only use these fixture names in test function signatures:\n"
            f"{', '.join(fixture_list)}\n"
            f"Do NOT invent new fixture names — they will cause collection errors.\n"
        )

    system_prompt = build_system_prompt(base_prompt, "surgeon",
                                        root_dir / ".agent", app_dir)

    user_prompt = (
        f"Task to implement: {task_desc}\n\n"
        f"Architect Implementation Brief:\n{plan.get('surgeon_prompt', 'No brief provided.')}\n\n"
        f"Installed Dependency Ledger:\n{ledger}\n\n"
        f"Existing Source Code Context:\n"
        f"{context_payload if context_payload else '(No existing files — all files are new)'}"
    )

    response = query_llm("surgeon", system_prompt, user_prompt, config)

    # Retry once if output is suspiciously short
    min_chars = config.get("surgeon_min_output_chars", 100)
    if len(response.strip()) < min_chars:
        print(
            f"[SURGEON] Output too short ({len(response.strip())} chars) — retrying..."
        )
        response = query_llm("surgeon", system_prompt, user_prompt, config)

    return response


# ==========================================
# TIER 3 — HEALER LOOP
# ==========================================


def execute_healer_loop(
    task_desc: str,
    root_dir: Path,
    app_dir: Path,
    conda_env: str,
    telemetry_file: Path,
) -> bool:
    """
    Tier 3 — Healer (Qwen-2.5-Coder-7B → escalates to 14B/32B):
    Runs the test suite and, on failure, asks the Healer to diagnose and
    patch the error. Retries up to max_retries times.

    Escalation logic:
        Pass 0:          Use healer model (7B) — fast, cheap
        Pass 1+:         Escalate to surgeon model (32B) — more capable
        All retries fail: Return False, orchestrator halts

    Returns True if tests pass within the retry budget, False otherwise.
    """
    config = load_config(root_dir)
    max_retries = config.get("max_healer_retries", 3)
    escalate_after = config.get("healer_escalate_after", 1)

    for iteration in range(max_retries):
        exit_code, test_output = run_tests(app_dir, conda_env)

        if exit_code == 0:
            print(f"[HEALER] ✓ All tests passing (attempt {iteration + 1}).")
            log_telemetry(telemetry_file, task_desc, "SUCCESS", iteration, "")
            return True

        print(f"[HEALER] Test failure (exit {exit_code}) — "
              f"repair pass {iteration + 1}/{max_retries}...")
        log_telemetry(telemetry_file, task_desc, "FAIL_ATTEMPT", iteration,
                      test_output)

        # Determine which model to use for this repair pass
        use_escalated = iteration >= escalate_after
        override_model = get_healer_escalation_model(
            config) if use_escalated else None
        tier = "surgeon" if use_escalated else "healer"

        if use_escalated:
            print(
                f"[HEALER] Escalating to {override_model} for pass {iteration + 1}..."
            )

        # Extract relevant source files from traceback for context
        relevant_source = _extract_traceback_context(test_output, root_dir)

        base_prompt = (
            "You are the Healer, a debugging agent in an autonomous coding pipeline. "
            "A test suite has failed. Identify the root cause and produce the minimum "
            "targeted patch to make the tests pass.\n\n"
            "══════════════════════════════════\n"
            "MANDATORY OUTPUT FORMAT\n"
            "══════════════════════════════════\n"
            "### FILE: path/relative/to/project/root/filename.ext\n"
            "<<<<<<< SEARCH\n"
            "<exact verbatim content to find — character perfect>\n"
            "=======\n"
            "<corrected replacement content>\n"
            ">>>>>>> REPLACE\n\n"
            "RULES:\n"
            "1. NEVER wrap SEARCH or REPLACE in ``` code fences of any kind.\n"
            "2. Read the traceback carefully — find the EXACT file and line causing failure.\n"
            "3. Patch only the broken region. Do NOT restructure or rewrite the whole file.\n"
            "4. Verify your SEARCH string against the provided source code exactly.\n"
            "5. Do NOT modify test files unless the test itself has an outright bug.\n"
            "   Primary target is always application code.\n"
            "6. If the fix requires a new import, add it at the top of the file.\n"
            "7. If the error is a missing fixture, add it to conftest.py — do not modify the test.\n"
            "8. SQLAlchemy 2.x: use db.session.get(Model, id) not Model.query.get(id).\n"
            "9. Assert ORM field values only AFTER db.session.commit().\n"
            "10. Output ONLY the FILE/SEARCH/REPLACE blocks — no explanations.\n"
        )

        system_prompt = build_system_prompt(base_prompt, tier,
                                            root_dir / ".agent", app_dir)

        user_prompt = (
            f"Original Task Context: {task_desc}\n\n"
            f"Test Runner Output (last 3000 chars):\n{test_output[-3000:]}\n\n"
            f"Relevant Source Files:\n"
            f"{relevant_source if relevant_source else '(Could not extract source files from traceback)'}"
        )

        response = query_llm(tier,
                             system_prompt,
                             user_prompt,
                             config,
                             override_model=override_model)
        patched = splice_multi_file_response(response, root_dir)

        if patched:
            # Run syntax check on all patched Python files
            _verify_patched_files(response, root_dir, conda_env)
        else:
            print("[HEALER] No valid patches found in healer response.")

    log_telemetry(telemetry_file, task_desc, "HALTED", max_retries,
                  test_output)
    print(f"[CRITICAL] Healer exhausted {max_retries} repair attempts. "
          "Manual intervention required.")
    return False


# ==========================================
# PRIVATE HELPERS
# ==========================================


def _build_directory_tree(app_dir: Path) -> str:
    """Builds a filtered directory tree string for Architect context."""
    skip_dirs = {
        "node_modules", "__pycache__", ".git", ".venv", "dist", "build",
        ".next"
    }
    lines = []

    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        level = root.replace(str(app_dir), "").count(os.sep)
        indent = "    " * level
        lines.append(f"{indent}{os.path.basename(root)}/")
        sub_indent = "    " * (level + 1)
        for fname in files:
            lines.append(f"{sub_indent}{fname}")

    return "\n".join(lines) if lines else "(empty — no files yet)"


def _load_context_files(file_paths: list[str], root_dir: Path) -> str:
    """Loads content of context files into a formatted string for Surgeon prompts."""
    parts = []
    missing = []

    for path_str in file_paths:
        full_path = root_dir / path_str
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
            parts.append(
                f"\n--- BEGIN FILE: {path_str} ---\n{content}\n--- END FILE: {path_str} ---"
            )
        else:
            missing.append(path_str)

    if missing:
        parts.append(
            f"\n[NOTE] These files do not exist yet and must be CREATED: {missing}"
        )

    return "\n".join(parts)


def _extract_traceback_context(test_output: str, root_dir: Path) -> str:
    """
    Parses pytest traceback output to find referenced source files,
    then loads their content for the Healer's context.
    Only loads files that actually exist under root_dir.
    """
    mentioned_files = re.findall(r'(app/[\w/._\-]+\.py)', test_output)
    seen = set()
    parts = []

    for rel_path in mentioned_files:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        full = root_dir / rel_path
        if full.exists():
            content = full.read_text(encoding="utf-8")
            parts.append(f"--- {rel_path} ---\n{content}")

    return "\n\n".join(parts)


def _verify_patched_files(surgeon_response: str, root_dir: Path,
                          conda_env: str) -> None:
    """
    Runs syntax verification on all Python files mentioned in a Surgeon/Healer response.
    Restores from snapshot if syntax check fails post-patch.
    """
    from engine.splicer import extract_file_chunks
    from engine.syntax import verify_syntax

    chunks = extract_file_chunks(surgeon_response)
    for rel_path, _ in chunks:
        if not rel_path.endswith(".py"):
            continue
        full_path = root_dir / rel_path
        if not full_path.exists():
            continue
        if not verify_syntax(full_path, conda_env):
            print(
                f"[SYNTAX] Restoring {rel_path} from snapshot due to syntax error."
            )
            restore_snapshot(full_path)
