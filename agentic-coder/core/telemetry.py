import json
import time
from pathlib import Path
from datetime import datetime


def start_timer() -> float:
    """Returns current timestamp for duration tracking. Call at task cycle start."""
    return time.time()


def format_duration(seconds: float) -> str:
    """Formats a duration in seconds to a human-readable string like '1m 34s'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining = seconds % 60
    return f"{minutes}m {remaining}s"


def log_telemetry(
    telemetry_file: Path,
    task_id: str,
    status: str,
    iteration: int,
    error_log: str,
    duration_s: float = 0.0,
) -> None:
    """
    Appends a structured JSONL record for the current pipeline cycle.

    Fields:
        task_id:                  The task description string
        status:                   'SUCCESS' | 'FAIL_ATTEMPT' | 'HALTED'
        healing_loop_iterations:  Which healer pass this record represents (0-indexed)
        duration_seconds:         Wall-clock time for the full task cycle
        timestamp:                ISO 8601 UTC timestamp
        primary_error_trace:      Last 5 lines of test output on failure

    Each call appends one line — file grows as JSONL across all task cycles.
    """
    trunc_error = ""
    if error_log:
        lines = error_log.splitlines()
        trunc_error = "\n".join(lines[-5:])

    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "task_id": task_id,
        "status": status,
        "healing_loop_iterations": iteration,
        "duration_seconds": round(duration_s, 2),
        "primary_error_trace": trunc_error,
    }

    with open(telemetry_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def print_summary(telemetry_file: Path) -> None:
    """
    Reads all JSONL telemetry records and prints a formatted summary table
    on pipeline completion. Shows per-task status, healer passes, and duration.

    Example output:
        ══════════════════════════════════════════════════════
        AGENT RUN SUMMARY
        ══════════════════════════════════════════════════════
         #  Status   Healer  Duration  Task
         1  ✅        0       12s       Create app/backend/extensions.py...
         2  ✅        2       94s       Create app/backend/models.py...
         3  ⚠ HALT   3       —         Write test_models.py...
        ──────────────────────────────────────────────────────
        Total: 3 tasks | 2 clean | 1 healed | 0 halted
        Total runtime: 1m 46s
        ══════════════════════════════════════════════════════
    """
    if not telemetry_file.exists():
        print("[TELEMETRY] No telemetry file found.")
        return

    records = []
    with open(telemetry_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not records:
        print("[TELEMETRY] No records to summarize.")
        return

    # Deduplicate — keep only the final status per task_id
    seen_tasks: dict[str, dict] = {}
    for record in records:
        task_id = record.get("task_id", "unknown")
        # Always overwrite so we keep the last record per task (final outcome)
        seen_tasks[task_id] = record

    final_records = list(seen_tasks.values())

    total = len(final_records)
    clean = sum(1 for r in final_records
                if r.get("healing_loop_iterations", 0) == 0
                and r.get("status") == "SUCCESS")
    healed = sum(1 for r in final_records
                 if r.get("healing_loop_iterations", 0) > 0
                 and r.get("status") == "SUCCESS")
    halted = sum(1 for r in final_records if r.get("status") == "HALTED")
    total_duration = sum(r.get("duration_seconds", 0) for r in final_records)

    width = 60
    print("\n" + "═" * width)
    print("  AGENTIC-CODER RUN SUMMARY")
    print("═" * width)
    print(f"  {'#':<4} {'Status':<10} {'Healer':<8} {'Duration':<10} Task")
    print("─" * width)

    for i, record in enumerate(final_records, 1):
        status = record.get("status", "UNKNOWN")
        iterations = record.get("healing_loop_iterations", 0)
        duration = format_duration(record.get("duration_seconds", 0))
        task_short = record.get("task_id", "unknown")[:45]

        if status == "SUCCESS" and iterations == 0:
            status_icon = "✅"
        elif status == "SUCCESS" and iterations > 0:
            status_icon = "🔧"
        else:
            status_icon = "❌ HALT"

        print(
            f"  {i:<4} {status_icon:<10} {iterations:<8} {duration:<10} {task_short}"
        )

    print("─" * width)
    print(
        f"  Total: {total} tasks | {clean} clean | {healed} healed | {halted} halted"
    )
    print(f"  Total runtime: {format_duration(total_duration)}")
    print("═" * width + "\n")
