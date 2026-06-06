import subprocess
from pathlib import Path


def git_autocommit(
    root_dir: Path,
    task_desc: str,
    task_index: int,
    total_tasks: int,
) -> bool:
    """
    Stages all changes and commits with a structured message after each
    successful task cycle. Gated by config flag git_autocommit=true.

    Commit message format:
        agent: task 4/49 — Create app/backend/models.py with User and Task ORM...

    Returns True on success, False if git is unavailable or commit fails.
    Does NOT exit on failure — git is optional infrastructure.
    """
    if not git_is_initialized(root_dir):
        print("[GIT] No .git directory found. Skipping autocommit.")
        return False

    short_desc = task_desc[:72] if len(task_desc) > 72 else task_desc
    commit_msg = f"agent: task {task_index}/{total_tasks} — {short_desc}"

    # Stage all changes
    stage_result = subprocess.run(
        ["git", "add", "-A"],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )
    if stage_result.returncode != 0:
        print(f"[GIT] git add failed: {stage_result.stderr.strip()}")
        return False

    # Check if there's anything to commit
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )
    if not status_result.stdout.strip():
        print("[GIT] No changes to commit.")
        return True

    # Commit
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        print(f"[GIT] git commit failed: {commit_result.stderr.strip()}")
        return False

    print(f"[GIT] Committed: {commit_msg}")
    return True


def git_is_initialized(root_dir: Path) -> bool:
    """Returns True if root_dir contains a .git directory."""
    return (root_dir / ".git").is_dir()


def git_init_if_needed(root_dir: Path) -> bool:
    """
    Initializes a git repository in root_dir if one doesn't exist.
    Also creates a .gitignore with sensible defaults for Python/Node projects.
    Called on first task cycle when git_autocommit=true.
    Returns True if init succeeded or was already initialized.
    """
    if git_is_initialized(root_dir):
        return True

    result = subprocess.run(
        ["git", "init"],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[GIT] git init failed: {result.stderr.strip()}")
        return False

    _write_gitignore(root_dir)

    # Initial commit so subsequent commits have a parent
    subprocess.run(["git", "add", ".gitignore"],
                   cwd=root_dir,
                   capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "agent: initialize repository"],
        cwd=root_dir,
        capture_output=True,
    )

    print(f"[GIT] Initialized repository at {root_dir}")
    return True


def git_get_task_count(root_dir: Path) -> int:
    """
    Returns the number of agent commits made so far.
    Used to determine task_index for commit messages.
    Counts commits with 'agent: task' prefix in the message.
    """
    if not git_is_initialized(root_dir):
        return 0

    result = subprocess.run(
        ["git", "log", "--oneline", "--grep=agent: task"],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0

    return len([l for l in result.stdout.splitlines() if l.strip()])


def _write_gitignore(root_dir: Path) -> None:
    """Writes a .gitignore appropriate for Python + Node projects."""
    gitignore_path = root_dir / ".gitignore"
    if gitignore_path.exists():
        return

    content = """\
# Python
__pycache__/
*.py[cod]
*.pyo
*.egg-info/
.venv/
dist/
build/
.pytest_cache/
*.bak

# Node
node_modules/
.next/
dist/
*.local

# Agent runtime
.agent/checkpoint.json
healing_telemetry.jsonl

# IDE
.vscode/
.idea/
*.swp
*.DS_Store
"""
    gitignore_path.write_text(content, encoding="utf-8")
