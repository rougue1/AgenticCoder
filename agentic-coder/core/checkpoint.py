import json
from datetime import datetime
from pathlib import Path

_CHECKPOINT_FILENAME = "checkpoint.json"


def save_checkpoint(
    root_dir: Path,
    task_desc: str,
    files_modified: list[str],
    task_index: int,
    total_tasks: int,
    last_exit_code: int = 0,
) -> None:
    """
    Writes a checkpoint file after each successful task cycle.
    Stored at .agent/checkpoint.json.

    Fields:
        last_completed_task:  Full task description string
        task_index:           1-based index of the completed task
        total_tasks:          Total task count at time of checkpoint
        files_modified:       List of relative paths written this cycle
        last_exit_code:       Exit code of the most recent test run.
                              Written on every save — 0 when no test has
                              run yet. Lets boot() distinguish a session
                              that ended green from one that ended
                              mid-healer with tests still failing.
        session_started:      ISO timestamp of when boot() was called
        last_updated:         ISO timestamp of this checkpoint write
    """
    agent_dir = root_dir / ".agent"
    agent_dir.mkdir(exist_ok=True)

    checkpoint_path = agent_dir / _CHECKPOINT_FILENAME

    # Preserve session_started from existing checkpoint if present
    session_started = datetime.utcnow().isoformat()
    existing = load_checkpoint(root_dir)
    if existing:
        session_started = existing.get("session_started", session_started)

    payload = {
        "last_completed_task": task_desc,
        "task_index": task_index,
        "total_tasks": total_tasks,
        "files_modified": files_modified,
        "last_exit_code": last_exit_code,
        "session_started": session_started,
        "last_updated": datetime.utcnow().isoformat(),
    }

    checkpoint_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_exit_code(root_dir: Path, exit_code: int) -> None:
    """
    Stamps last_exit_code onto the existing checkpoint without touching the
    task-progress fields. Called when the healer loop exhausts its retries,
    so the on-disk state records that the session ended red. Creates a
    minimal checkpoint if none exists yet (failure on the very first task).
    """
    agent_dir = root_dir / ".agent"
    agent_dir.mkdir(exist_ok=True)

    checkpoint = load_checkpoint(root_dir) or {}
    checkpoint["last_exit_code"] = exit_code
    checkpoint["last_updated"] = datetime.utcnow().isoformat()

    (agent_dir / _CHECKPOINT_FILENAME).write_text(json.dumps(checkpoint,
                                                             indent=2),
                                                  encoding="utf-8")


def load_checkpoint(root_dir: Path) -> dict | None:
    """
    Loads the checkpoint file if it exists.
    Returns the checkpoint dict on success, None if not found or corrupted.
    """
    checkpoint_path = root_dir / ".agent" / _CHECKPOINT_FILENAME

    if not checkpoint_path.exists():
        return None

    try:
        return json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(
            "[CHECKPOINT] Corrupted checkpoint file — ignoring and starting fresh."
        )
        return None


def checkpoint_exists(root_dir: Path) -> bool:
    """Returns True if a valid checkpoint file exists."""
    return load_checkpoint(root_dir) is not None


def clear_checkpoint(root_dir: Path) -> None:
    """
    Deletes the checkpoint file on clean pipeline completion.
    Called after all tasks are marked complete.
    """
    checkpoint_path = root_dir / ".agent" / _CHECKPOINT_FILENAME
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        print("[CHECKPOINT] Session checkpoint cleared.")


def print_checkpoint_status(root_dir: Path) -> None:
    """
    Prints a human-readable resume prompt if a checkpoint exists.
    Called at boot() before asking the user whether to resume.
    """
    checkpoint = load_checkpoint(root_dir)
    if not checkpoint:
        return

    print("\n" + "─" * 50)
    print("[CHECKPOINT] Previous session detected:")
    print(
        f"  Last completed: {checkpoint.get('last_completed_task', 'unknown')[:60]}"
    )
    print(
        f"  Progress:       {checkpoint.get('task_index', '?')}/{checkpoint.get('total_tasks', '?')} tasks"
    )
    last_exit = checkpoint.get("last_exit_code", 0)
    exit_note = "green" if last_exit == 0 else "tests failing"
    print(f"  Last exit code: {last_exit} ({exit_note})")
    print(f"  Last updated:   {checkpoint.get('last_updated', 'unknown')}")
    print("─" * 50)
