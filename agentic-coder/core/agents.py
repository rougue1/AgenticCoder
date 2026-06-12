import os
import re
import time
from pathlib import Path

from engine.llm import query_llm, clean_and_parse_json, load_config, get_healer_escalation_model, query_llm_with_json_retry
from engine.splicer import splice_multi_file_response, extract_file_chunks
from engine.syntax import verify_syntax
from engine.patch import restore_snapshot
from spec.prompts import resolve_base_prompt
from spec.stack import load_stack_profile
from spec.steering import build_system_prompt, get_fixture_registry
from spec.sdd import read_design_doc
from testing.runner import (
    run_tests,
    run_targeted_tests,
    run_file_tests,
    aggregate_results,
    count_failures,
    load_run_config,
    extract_test_function_names,
    failing_test_names,
    extract_function_source,
)
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
        - All shared test infrastructure files (conftest.py) found under
          app_dir — unconditional, not part of context_files selection,
          capped at 4000 chars

    Exits on JSON parse failure — a missing plan means the pipeline cannot proceed.
    """
    import sys
    config = load_config(root_dir)
    print(f"[ARCHITECT] Planning implementation...")

    tree_str = _build_directory_tree(app_dir)
    design_context = read_design_doc(root_dir)

    # Compiled stack-specific prompt if present, generic base prompt otherwise.
    base_prompt = resolve_base_prompt("architect", root_dir / ".agent")

    system_prompt = build_system_prompt(base_prompt, "architect",
                                        root_dir / ".agent", app_dir)

    user_prompt = (f"Active Task: {task_desc}\n\n"
                   f"Current Directory Tree (app/):\n{tree_str}\n\n"
                   f"Project Design Document:\n{design_context}")

    # Unconditional injection: the Architect must always know the existing
    # fixture/helper landscape so surgeon_prompts don't ask the Surgeon to
    # recreate or contradict shared test scaffolding.
    test_infra = _load_shared_test_infra(app_dir)
    if test_infra:
        user_prompt += (
            "\n\n══════════════════════════════════\n"
            "SHARED TEST INFRASTRUCTURE (always injected — existing fixtures "
            "and helpers the Surgeon must work within, never recreate)\n"
            "══════════════════════════════════\n"
            f"{test_infra}")
        print(
            f"[ARCHITECT] Injected shared test infrastructure ({len(test_infra)} chars)."
        )

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

    # Compiled stack-specific prompt if present, generic base prompt otherwise.
    # The fixture appendix below is dynamic per-call state, so it is appended
    # on top of whichever base was resolved rather than baked into either.
    base_prompt = resolve_base_prompt("surgeon", root_dir / ".agent")

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
    new_tests: list[dict] | None = None,
    task_index: int = 0,
    cycle_start: float | None = None,
) -> tuple[bool, int]:
    """
    Tier 3 — Healer (Qwen-2.5-Coder-7B → escalates to 14B/32B):
    Runs the adaptive three-phase test sequence and, on failure at any
    phase, asks the Healer to diagnose and patch the error.

    Phases (built by _build_phase_plan from stack.md command templates):
        Phase 1 — targeted run of only the test functions the Surgeon wrote
        Phase 2 — full run of the files containing those tests
        Phase 3 — full suite (frequency configurable; always when 1–2 empty)
    When stack.md is absent the plan collapses to a single full-suite run —
    the pre-adaptive behavior.

    new_tests: per-test-file dicts from the orchestrator —
        {"path": str, "is_new_file": bool, "functions": [str]}

    Semantics: max_healer_retries = total repair attempts across all phases.
    Each repair is verified by re-running the phase that failed. Before each
    patch the touched files are snapshotted in memory; if the verification
    run shows MORE failures than before the patch, all touched files are
    rolled back and the next attempt starts from the pre-patch state.

    Escalation swaps the model after healer_escalate_after failed repairs —
    the healer prompt/role is kept either way.

    Returns (success, final_exit_code). exit_code is 0 on success and the
    last failing phase's exit code when the retry budget is exhausted.
    """
    config = load_config(root_dir)
    max_retries = config.get("max_healer_retries", 3)
    escalate_after = config.get("healer_escalate_after", 1)
    context_budget = config.get("healer_context_budget", 6000)

    def _elapsed() -> float:
        return time.time() - cycle_start if cycle_start else 0.0

    stack_profile = load_stack_profile(root_dir / ".agent")
    phases = _build_phase_plan(root_dir, app_dir, conda_env, new_tests or [],
                               task_index, config, stack_profile)
    if len(phases) > 1:
        print("[HEALER] Adaptive plan: " +
              " → ".join(p["label"] for p in phases))

    repairs_used = 0

    for phase in phases:
        print(f"[HEALER] {phase['label']} — running...")
        exit_code, test_output = phase["run"]()
        failure_count = count_failures(exit_code, test_output)

        while exit_code != 0:
            if repairs_used >= max_retries:
                log_telemetry(telemetry_file, task_desc, "HALTED",
                              repairs_used, test_output,
                              duration_s=_elapsed())
                print(
                    f"[CRITICAL] Healer exhausted {max_retries} repair attempts. "
                    "Manual intervention required.")
                return False, exit_code

            print(f"[HEALER] {phase['label']} failed (exit {exit_code}) — "
                  f"repair pass {repairs_used + 1}/{max_retries}...")
            log_telemetry(telemetry_file, task_desc, "FAIL_ATTEMPT",
                          repairs_used, test_output, duration_s=_elapsed())

            # Determine which model to use for this repair pass
            use_escalated = repairs_used >= escalate_after
            override_model = get_healer_escalation_model(
                config) if use_escalated else None
            tier = "surgeon" if use_escalated else "healer"

            if use_escalated:
                print(f"[HEALER] Escalating to {override_model} for pass "
                      f"{repairs_used + 1}...")

            # Phase-scoped test context: only the test functions relevant to
            # this failure, never whole test files or unrelated suites.
            phase_test_context = _build_phase_test_context(
                phase, test_output, root_dir, app_dir, stack_profile)

            # Budgeted source context from traceback file mentions
            relevant_source = _extract_traceback_context(
                test_output, root_dir, context_budget)

            # Compiled stack-specific prompt if present, generic base otherwise.
            # The healer unit is used even on escalated passes — escalation
            # swaps the model, not the repair role.
            base_prompt = resolve_base_prompt("healer", root_dir / ".agent")

            system_prompt = build_system_prompt(base_prompt, tier,
                                                root_dir / ".agent", app_dir)

            test_context_section = (
                f"Failing Test Code (only the relevant functions):\n"
                f"{phase_test_context}\n\n") if phase_test_context else ""

            user_prompt = (
                f"Original Task Context: {task_desc}\n\n"
                f"TEST PHASE THAT FAILED: {phase['label']}\n"
                f"The regression scope is limited to this phase — diagnose within it.\n\n"
                f"{test_context_section}"
                f"Test Runner Output (last 3000 chars):\n{test_output[-3000:]}\n\n"
                f"Relevant Source Files:\n"
                f"{relevant_source if relevant_source else '(Could not extract source files from traceback)'}"
            )

            response = query_llm(tier,
                                 system_prompt,
                                 user_prompt,
                                 config,
                                 override_model=override_model)

            # In-memory snapshot of every file this patch will touch, taken
            # before the splice so a regression can be rolled back exactly.
            snapshots = _snapshot_response_files(response, root_dir)
            patched = splice_multi_file_response(response, root_dir)

            if patched:
                _verify_patched_files(response, root_dir, conda_env)
            else:
                print("[HEALER] No valid patches found in healer response.")

            repairs_used += 1

            # ── Verify the repair by re-running the failed phase ──
            new_exit, new_output = phase["run"]()
            new_failures = count_failures(new_exit, new_output)

            if patched and new_failures > failure_count:
                # Unconditional rollback on regression: restore every touched
                # file, then repair again from the last known good state.
                print(f"[HEALER] Patch made things worse ({failure_count} → "
                      f"{new_failures} failures) — rolling back "
                      f"{len(snapshots)} file(s).")
                _restore_in_memory_snapshots(snapshots, root_dir)
                continue  # keep pre-patch exit_code/test_output/failure_count

            exit_code, test_output = new_exit, new_output
            failure_count = new_failures

        if repairs_used == 0:
            print(f"[HEALER] ✓ {phase['label']} green on first run.")
        else:
            print(f"[HEALER] ✓ {phase['label']} green.")

    log_telemetry(telemetry_file, task_desc, "SUCCESS", repairs_used, "",
                  duration_s=_elapsed())
    return True, 0


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


def _extract_traceback_context(
    test_output: str,
    root_dir: Path,
    budget: int = 6000,
) -> str:
    """
    Parses test runner output to find referenced source files under app/,
    then loads their content for the Healer's context — subject to a hard
    total character budget.

    Priority: files mentioned more often in the traceback are loaded first.
    Files are loaded whole (never truncated mid-content); loading stops at
    the first file that would exceed the remaining budget, and everything
    after it is reported as omitted so the Healer knows the exact
    boundaries of its context.

    Extension-agnostic: matches any file path under app/ regardless of
    language. Skips binary files and unreadable paths without raising.
    """
    mentioned_files = re.findall(r'(app/[\w/._\-]+\.\w+)', test_output)
    if not mentioned_files:
        return ""

    counts: dict[str, int] = {}
    for rel_path in mentioned_files:
        counts[rel_path] = counts.get(rel_path, 0) + 1
    ordered = sorted(counts, key=lambda rel: -counts[rel])

    loaded, omitted, parts = [], [], []
    used = 0
    budget_exhausted = False

    for rel_path in ordered:
        full = root_dir / rel_path
        if not full.exists():
            continue
        try:
            content = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        if budget_exhausted or used + len(content) > budget:
            budget_exhausted = True
            omitted.append(rel_path)
            continue

        used += len(content)
        loaded.append(rel_path)
        parts.append(f"--- {rel_path} (mentioned {counts[rel_path]}×) ---\n"
                     f"{content}")

    if not loaded and not omitted:
        return ""

    header = (f"[SOURCE CONTEXT — budget {budget} chars] "
              f"Files loaded in full: {', '.join(loaded) or 'none'}.")
    if omitted:
        header += (f" Files OMITTED (budget exhausted — not visible to you): "
                   f"{', '.join(omitted)}.")

    return header + "\n\n" + "\n\n".join(parts) if parts else header


def _load_shared_test_infra(app_dir: Path, cap: int = 4000) -> str:
    """
    Loads every shared test infrastructure file (conftest.py) found under
    app_dir for unconditional injection into the Architect's prompt.
    Capped at `cap` total characters so a pathologically large file cannot
    dominate the Architect's context.
    """
    skip_dirs = {
        "node_modules", "__pycache__", ".git", ".venv", "dist", "build",
        ".next"
    }
    parts = []
    for path in sorted(app_dir.rglob("conftest.py")):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(path.relative_to(app_dir.parent))
        parts.append(f"--- {rel} ---\n{content}")

    combined = "\n\n".join(parts)
    if len(combined) > cap:
        combined = combined[:cap] + f"\n... [truncated at {cap} chars]"
    return combined


def _snapshot_response_files(response: str,
                             root_dir: Path) -> dict[str, str | None]:
    """
    In-memory pre-patch snapshot of every file a Healer response will touch.
    A None value marks a file that does not exist yet — it will be created
    by the patch and must be deleted again on rollback.
    """
    snapshots: dict[str, str | None] = {}
    for rel_path, _ in extract_file_chunks(response):
        if rel_path in snapshots:
            continue
        full = root_dir / rel_path
        if full.exists():
            try:
                snapshots[rel_path] = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        else:
            snapshots[rel_path] = None
    return snapshots


def _restore_in_memory_snapshots(snapshots: dict[str, str | None],
                                 root_dir: Path) -> None:
    """Restores every snapshotted file to its exact pre-patch state."""
    for rel_path, content in snapshots.items():
        full = root_dir / rel_path
        if content is None:
            full.unlink(missing_ok=True)
        else:
            full.write_text(content, encoding="utf-8")
        print(f"  [ROLLBACK] {rel_path}")


def _build_phase_plan(
    root_dir: Path,
    app_dir: Path,
    conda_env: str,
    new_tests: list[dict],
    task_index: int,
    config: dict,
    stack_profile: dict | None,
) -> list[dict]:
    """
    Builds the ordered adaptive phase plan for one task cycle. Each phase is
    {"label", "kind", "targets", "run"} where run() -> (exit_code, output).

    Routing rules:
        - stack.md absent → single full-suite phase (pre-adaptive behavior)
        - no new tests this task → full suite only (something must run)
        - Phase 1: targeted_test_command per new function for tests added to
          existing files; whole-file run (file_test_command) for brand new
          files since every test in them is new
        - Phase 2: file run, only for files where Phase 1 was function-
          targeted (a Phase-1 whole-file run already covered the file)
        - Phase 3: full suite, every suite_run_every_n_tasks tasks
          (0 = only when AGENT_FORCE_SUITE=1), and always when Phases 1–2
          produced nothing to run
    """
    suite_phase = {
        "label": "Phase 3 — full suite run",
        "kind": "suite",
        "targets": [],
        "run": lambda: run_tests(app_dir, conda_env, root_dir),
    }

    if stack_profile is None:
        suite_phase["label"] = "Full suite run (stack.md absent)"
        return [suite_phase]
    if not new_tests:
        suite_phase["label"] = "Full suite run (no new tests this task)"
        return [suite_phase]

    targeted_tpl = stack_profile.get("targeted_test_command")
    file_tpl = stack_profile.get("file_test_command")

    # (path, functions, mode) — mode is "functions" or "file"
    phase1_targets = []
    for test in new_tests:
        if test["is_new_file"]:
            if file_tpl:
                phase1_targets.append((test["path"], test["functions"], "file"))
            elif targeted_tpl and test["functions"]:
                phase1_targets.append(
                    (test["path"], test["functions"], "functions"))
        else:
            if targeted_tpl and test["functions"]:
                phase1_targets.append(
                    (test["path"], test["functions"], "functions"))
            elif file_tpl:
                phase1_targets.append((test["path"], test["functions"], "file"))

    phases = []
    if phase1_targets:
        phases.append({
            "label": "Phase 1 — targeted run (new tests only)",
            "kind": "targeted",
            "targets": phase1_targets,
            "run": lambda targets=phase1_targets: _run_phase_targets(
                root_dir, conda_env, targets, stack_profile),
        })

    phase2_files = [(path, functions)
                    for path, functions, mode in phase1_targets
                    if mode == "functions"]
    if phase2_files and file_tpl:
        phases.append({
            "label": "Phase 2 — file run (files containing new tests)",
            "kind": "file",
            "targets": phase2_files,
            "run": lambda files=phase2_files: _run_phase_files(
                root_dir, conda_env, files, stack_profile),
        })

    every_n = config.get("suite_run_every_n_tasks", 1)
    force_suite = os.environ.get("AGENT_FORCE_SUITE") == "1"
    if force_suite or (every_n > 0 and task_index % every_n == 0) or not phases:
        phases.append(suite_phase)

    return phases


def _run_phase_targets(root_dir: Path, conda_env: str, targets: list,
                       stack_profile: dict) -> tuple[int, str]:
    """Phase 1 executor: one aggregated result across all targeted runs."""
    results = []
    for path, functions, mode in targets:
        if mode == "functions":
            code, out = run_targeted_tests(root_dir, conda_env, path,
                                           functions, stack_profile)
        else:
            code, out = run_file_tests(root_dir, conda_env, path,
                                       stack_profile)
        results.append((f"targeted {path}", code, out))
    return aggregate_results(results)


def _run_phase_files(root_dir: Path, conda_env: str, files: list,
                     stack_profile: dict) -> tuple[int, str]:
    """Phase 2 executor: whole-file runs aggregated across all test files."""
    results = []
    for path, _ in files:
        code, out = run_file_tests(root_dir, conda_env, path, stack_profile)
        results.append((f"file {path}", code, out))
    return aggregate_results(results)


def _build_phase_test_context(
    phase: dict,
    test_output: str,
    root_dir: Path,
    app_dir: Path,
    stack_profile: dict | None,
) -> str:
    """
    Phase-scoped test code for the Healer prompt — only the functions
    relevant to this failure, never whole test files:

        Phase 1 (targeted): code of the failing new test function(s)
        Phase 2 (file):     code of the new function(s) that passed Phase 1
                            plus the existing function(s) now failing
        Phase 3 (suite):    code of the function(s) that newly fail at suite
                            scope (Phases 1–2 were green, so every suite
                            failure is new)

    Returns "" when nothing can be extracted (e.g. legacy single-phase mode
    with stack.md absent) — the Healer then works from runner output alone.
    """
    kind = phase["kind"]
    parts = []

    if kind == "targeted":
        for path, functions, _mode in phase["targets"]:
            file_text = _read_text_or_empty(root_dir / path)
            if not file_text:
                continue
            candidates = functions or extract_test_function_names(
                file_text, stack_profile)
            failing = failing_test_names(test_output, candidates) or candidates
            for name in failing:
                source = extract_function_source(file_text, name,
                                                 stack_profile)
                if source:
                    parts.append(f"--- {path} :: {name} (failing new test) ---"
                                 f"\n{source}")

    elif kind == "file":
        for path, new_functions in phase["targets"]:
            file_text = _read_text_or_empty(root_dir / path)
            if not file_text:
                continue
            all_names = extract_test_function_names(file_text, stack_profile)
            existing = [n for n in all_names if n not in new_functions]
            # strict: verbose file-run output also mentions passing tests
            failing_existing = failing_test_names(test_output,
                                                  existing,
                                                  strict=True)
            for name in new_functions:
                source = extract_function_source(file_text, name,
                                                 stack_profile)
                if source:
                    parts.append(f"--- {path} :: {name} "
                                 f"(new test — passed Phase 1) ---\n{source}")
            for name in failing_existing:
                source = extract_function_source(file_text, name,
                                                 stack_profile)
                if source:
                    parts.append(f"--- {path} :: {name} "
                                 f"(existing test now failing) ---\n{source}")

    elif kind == "suite" and stack_profile is not None:
        run_cfg = load_run_config(root_dir)
        test_glob = run_cfg.get("test_file_glob",
                                "test_*.py") if run_cfg else "test_*.py"
        for test_file in sorted(app_dir.rglob(test_glob)):
            file_text = _read_text_or_empty(test_file)
            if not file_text:
                continue
            names = extract_test_function_names(file_text, stack_profile)
            # strict: verbose suite output mentions every test, pass or fail
            failing = failing_test_names(test_output, names, strict=True)
            rel = str(test_file.relative_to(root_dir))
            for name in failing:
                source = extract_function_source(file_text, name,
                                                 stack_profile)
                if source:
                    parts.append(f"--- {rel} :: {name} "
                                 f"(newly failing at suite scope) ---\n{source}")

    return "\n\n".join(parts)


def _read_text_or_empty(path: Path) -> str:
    """Reads a text file, returning "" on absence or decode/OS errors."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _verify_patched_files(surgeon_response: str, root_dir: Path,
                          conda_env: str) -> None:
    """
    Runs syntax verification on all files mentioned in a Surgeon/Healer response.
    Restores from snapshot if syntax check fails post-patch.
    verify_syntax dispatches by extension and no-ops on unsupported types,
    so no extension filter is needed here.
    """
    from engine.splicer import extract_file_chunks
    from engine.syntax import verify_syntax

    chunks = extract_file_chunks(surgeon_response)
    for rel_path, _ in chunks:
        full_path = root_dir / rel_path
        if not full_path.exists():
            continue
        if not verify_syntax(full_path, conda_env):
            print(
                f"[SYNTAX] Restoring {rel_path} from snapshot due to syntax error."
            )
            restore_snapshot(full_path)
