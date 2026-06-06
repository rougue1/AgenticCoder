import os
import sys
import time
from pathlib import Path

from engine.llm import load_config
from engine.splicer import splice_multi_file_response, extract_file_chunks
from engine.patch import cleanup_snapshots
from spec.sdd import sdd_documents_exist, generate_sdd_documents
from spec.tasks import get_next_task, commit_task_complete, count_tasks
from spec.steering import generate_steering_files
from testing.preflight import validate_source_completeness, validate_test_correctness
from testing.fixtures import ensure_init_files, check_fixture_drift, get_fixture_summary
from tools.conda import ensure_conda_env
from tools.deps import update_dependencies
from core.agents import get_architect_plan, execute_surgeon, execute_healer_loop
from core.telemetry import log_telemetry, print_summary, start_timer, format_duration
from core.checkpoint import (
    save_checkpoint,
    load_checkpoint,
    clear_checkpoint,
    checkpoint_exists,
    print_checkpoint_status,
)


def boot() -> None:
    """
    Main entry point. Orchestrates the full SDD → Architect → Surgeon →
    Preflight → Healer → Deps → State pipeline.

    Flow:
        1. Resolve paths from config
        2. Check for previous session checkpoint — offer resume
        3. If SDD docs missing — prompt for project description, generate them
        4. Generate steering files from design.md (first run only)
        5. Ensure conda environment is ready
        6. Loop: read next unchecked task → run full agent cycle
        7. On success: save checkpoint, update deps, mark task complete
        8. On healer failure: halt with exit code 1
        9. On all tasks complete: print summary, clear checkpoint, exit 0

    Reads env vars set by main.py CLI flags:
        AGENT_AUTO_RESUME=1  → skip resume prompt, always resume
        AGENT_TASKS_ONLY=1   → skip SDD generation, go straight to task loop
    """
    print("=" * 60)
    print("  AGENTIC-CODER — LOCAL MULTI-TIER AGENT AUTOPILOT")
    print("=" * 60)

    root_dir = Path(__file__).parent.parent.absolute()
    config = load_config(root_dir)
    app_dir = root_dir / "app"
    agent_dir = root_dir / ".agent"
    telemetry_file = root_dir / "healing_telemetry.jsonl"

    app_dir.mkdir(exist_ok=True)
    agent_dir.mkdir(exist_ok=True)

    conda_env = config.get("conda_env", "agent_app_env")

    # ── Read CLI flags injected by main.py via environment ──
    auto_resume = os.environ.get("AGENT_AUTO_RESUME") == "1"
    tasks_only = os.environ.get("AGENT_TASKS_ONLY") == "1"

    # ── Resume detection ──
    if checkpoint_exists(root_dir):
        print_checkpoint_status(root_dir)
        if auto_resume:
            print("[BOOT] Auto-resuming previous session (--resume flag).")
        else:
            try:
                resume = input(
                    "\nResume previous session? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                resume = "y"
            if resume in {"n", "no"}:
                clear_checkpoint(root_dir)
                print("[BOOT] Starting fresh session.")

    # ── SDD document generation ──
    if tasks_only:
        print("[BOOT] --tasks-only flag set — skipping SDD generation.")
    elif not sdd_documents_exist(root_dir):
        print("[BOOT] SDD documents not found — initializing new project...")

        # Load from testprompt.txt if it exists, otherwise fall back to input()
        prompt_file = root_dir / "testprompt.txt"
        if prompt_file.exists():
            project_desc = prompt_file.read_text(encoding="utf-8").strip()
            print(
                f"[BOOT] Loaded project description from testprompt.txt ({len(project_desc)} chars)"
            )
        else:
            try:
                project_desc = input(
                    "\nDescribe the application you want to build:\n> ").strip(
                    )
            except (EOFError, KeyboardInterrupt):
                print("\n[ABORTED] No project description provided.")
                sys.exit(1)

        if not project_desc:
            print("[FATAL] Project description is required.")
            sys.exit(1)

        generate_sdd_documents(project_desc, root_dir)

    # ── Steering file generation (once per project) ──
    steering_dir = agent_dir / "steering"
    if not steering_dir.exists() or not (steering_dir / "AGENTS.md").exists():
        generate_steering_files(agent_dir, root_dir)

    # ── Conda environment ──
    ensure_conda_env(conda_env)

    # ── Print initial task queue state ──
    completed, total = count_tasks(root_dir / "tasks.md")
    print(
        f"\n[BOOT] Task queue: {completed}/{total} complete. Starting cycle...\n"
    )

    task_index = completed

    # ══════════════════════════════════
    # MAIN TASK LOOP
    # ══════════════════════════════════
    while True:
        active_task = get_next_task(root_dir / "tasks.md")

        if not active_task:
            print(
                "\n[COMPLETE] All tasks in tasks.md marked complete. Build finished."
            )
            print_summary(telemetry_file)
            clear_checkpoint(root_dir)
            sys.exit(0)

        task_index += 1
        _, total = count_tasks(root_dir / "tasks.md")

        print(f"\n{'═' * 60}")
        print(f"  TASK {task_index}/{total}")
        print(f"  {active_task[:72]}")
        print(f"{'═' * 60}")

        cycle_start = start_timer()
        success = run_task_cycle(
            task_desc=active_task,
            task_index=task_index,
            total_tasks=total,
            config=config,
            root_dir=root_dir,
            app_dir=app_dir,
            conda_env=conda_env,
            telemetry_file=telemetry_file,
        )

        if success:
            duration = time.time() - cycle_start
            print(
                f"\n[CYCLE] ✓ Task complete in {format_duration(duration)}. Advancing...\n"
            )
        else:
            print("\n[HALT] Healer loop exhausted without green tests.\n"
                  "       Review healing_telemetry.jsonl and fix manually.")
            print_summary(telemetry_file)
            sys.exit(1)


def run_task_cycle(
    task_desc: str,
    task_index: int,
    total_tasks: int,
    config: dict,
    root_dir: Path,
    app_dir: Path,
    conda_env: str,
    telemetry_file: Path,
) -> bool:
    """
    Executes the full agent pipeline for a single task:

        Architect → Surgeon → Splice → Preflight Source Check →
        Preflight Test Check → __init__ Ensure → Fixture Drift Check →
        Healer Loop → Dep Update → Task State Commit → Snapshot Cleanup →
        Checkpoint Save

    Returns True on full success, False if healer loop exhausted.
    """

    # ── Step 1: Architect plans ──
    plan = get_architect_plan(task_desc, app_dir, root_dir)
    context_files = plan.get("context_files", [])

    # ── Step 2: Surgeon writes code ──
    surgeon_output = execute_surgeon(plan, task_desc, root_dir, app_dir)

    # ── Step 3: Apply patches to disk ──
    patched = splice_multi_file_response(surgeon_output, root_dir)
    if not patched:
        print(
            "[WARN] Surgeon produced no valid file patches. Attempting healer anyway..."
        )

    # ── Step 4: Ensure __init__.py files exist ──
    ensure_init_files(app_dir)

    # ── Step 5: Pre-flight source completeness check ──
    validate_source_completeness(context_files, task_desc, root_dir, app_dir)

    # ── Step 6: Collect newly written test files ──
    import time as _time
    recent_cutoff = _time.time() - 180  # files written in last 3 minutes
    new_test_files = [
        str(p.relative_to(root_dir)) for p in (app_dir).rglob("test_*.py")
        if p.stat().st_mtime > recent_cutoff
    ]

    # ── Step 7: Pre-flight test correctness check ──
    if new_test_files:
        validate_test_correctness(new_test_files, context_files, root_dir,
                                  app_dir)

    # ── Step 8: Fixture drift warning ──
    drift_warnings = check_fixture_drift(app_dir)
    if drift_warnings:
        print(f"[FIXTURES] Drift warnings ({len(drift_warnings)}):")
        for w in drift_warnings:
            print(f"  ⚠ {w}")
        print(
            f"[FIXTURES] Available fixtures:\n  {get_fixture_summary(app_dir)}"
        )

    # ── Step 9: Healer loop ──
    success = execute_healer_loop(
        task_desc=task_desc,
        root_dir=root_dir,
        app_dir=app_dir,
        conda_env=conda_env,
        telemetry_file=telemetry_file,
    )

    if not success:
        return False

    # ── Step 10: Update dependencies ──
    update_dependencies(app_dir, root_dir, conda_env)

    # ── Step 11: Commit task state ──
    committed = commit_task_complete(root_dir / "tasks.md", task_desc,
                                     root_dir)
    if not committed:
        print(
            "[WARN] Could not mark task complete in tasks.md. Continuing anyway."
        )

    # ── Step 12: Clean up .bak snapshots ──
    cleanup_snapshots(root_dir)

    # ── Step 13: Save checkpoint ──
    modified_files = [p for p, _ in extract_file_chunks(surgeon_output)]
    save_checkpoint(root_dir, task_desc, modified_files, task_index,
                    total_tasks)

    return True
