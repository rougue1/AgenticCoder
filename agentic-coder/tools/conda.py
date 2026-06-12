import hashlib
import os
import subprocess
import sys
from pathlib import Path

from tools.permissions import confirm_command


def ensure_conda_env(env_name: str, python_version: str = "3.11") -> bool:
    """
    Verifies the isolated Conda environment exists.
    Creates it with the requested Python version if absent. Stack-specific
    packages (test framework, plugins, CLI tools) are installed separately
    by install_bootstrap_packages() using the bootstrap_packages list in run.json.
    Exits the entire process on Conda not found or creation failure —
    there is no recovery path without a working execution environment.

    Returns True if the environment was just created, False if it already existed.
    The caller uses this to gate bootstrap package installation so resumed
    sessions do not incur a repeated pip subprocess.
    """
    print(f"[CONDA] Checking environment '{env_name}'...")

    if not _conda_available():
        print(
            "[CRITICAL] 'conda' not found in system PATH.\n"
            "           Install Miniconda: https://docs.conda.io/en/latest/miniconda.html"
        )
        sys.exit(1)

    if conda_env_exists(env_name):
        print(f"[CONDA] Environment '{env_name}' is ready.")
        return False

    print(
        f"[CONDA] Creating environment '{env_name}' with Python {python_version}..."
    )
    try:
        subprocess.run(
            [
                "conda",
                "create",
                "-y",
                "-n",
                env_name,
                f"python={python_version}",
            ],
            check=True,
        )
        print(f"[CONDA] Environment '{env_name}' created successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[CRITICAL] Conda environment creation failed: {e}")
        sys.exit(1)

    return True


def run_shell_command(
    command: str,
    conda_env: str,
    cwd: Path | None = None,
    capture: bool = True,
    timeout: int | None = None,
) -> tuple[int, str]:
    """
    Executes a free-form shell command string inside the conda environment.

    stack.md commands (install templates, bootstrap steps, check commands)
    are arbitrary shell strings — they may chain with &&, set environment
    variables, or pipe — so they run through a shell rather than shlex argv
    splitting. The shell is execution infrastructure, not stack knowledge:
    every command string itself comes from stack.md, never from code.
    """
    return run_in_env(
        ["bash", "-c", command],
        conda_env,
        cwd=cwd,
        capture=capture,
        timeout=timeout,
    )


# ==========================================
# STACK.MD BOOTSTRAP (one-time environment setup)
# ==========================================

_BOOTSTRAP_MARKER = ".bootstrap_hash"


def run_bootstrap_commands(
    commands: list[dict],
    conda_env: str,
    root_dir: Path,
    config: dict,
) -> None:
    """
    Executes the ordered bootstrap command list from stack.md. Each entry is
    a dict from spec/stack.parse_stack_profile():

        command:       arbitrary shell command string (required)
        check:         optional command — exit 0 means the step's effect is
                       already present and the command is skipped (idempotency)
        requires_sudo: route through the permission gate before running
        interactive:   gate + run attached to the terminal so the user can
                       answer the command's own prompts
        reason:        one-line explanation shown at the permission gate

    Non-fatal end to end: declined gates and failed commands are logged and
    skipped — the pipeline continues either way. The per-task dependency
    scanner and the Healer can often recover from a missing bootstrap step.
    """
    print(f"[BOOTSTRAP] Running {len(commands)} stack.md bootstrap step(s)...")

    for index, entry in enumerate(commands, 1):
        command = entry.get("command", "")
        if not command:
            continue
        label = f"[BOOTSTRAP {index}/{len(commands)}]"

        check = entry.get("check")
        if check:
            check_code, _ = run_shell_command(check,
                                              conda_env,
                                              cwd=root_dir,
                                              timeout=120)
            if check_code == 0:
                print(f"{label} Already satisfied (check passed) — "
                      f"skipping: {command}")
                continue

        requires_sudo = entry.get("requires_sudo", False)
        interactive = entry.get("interactive", False)
        reason = entry.get(
            "reason", "One-time environment bootstrap step from stack.md")

        if not confirm_command(command, reason, requires_sudo, interactive,
                               config):
            continue  # decline already logged by the gate — never halt

        print(f"{label} Running: {command}")
        # Gated commands run attached to the terminal so the user can answer
        # password/installer prompts; everything else is captured for logging.
        passthrough = requires_sudo or interactive
        code, output = run_shell_command(
            command,
            conda_env,
            cwd=root_dir,
            capture=not passthrough,
            timeout=None if passthrough else 1800,
        )
        if code != 0:
            tail = f"\n{output[-500:]}" if output else ""
            print(f"[WARN] Bootstrap step failed (exit {code}) — "
                  f"continuing: {command}{tail}")


def bootstrap_pending(agent_dir: Path) -> bool:
    """
    True when the stack.md bootstrap sequence still needs to run: no marker
    from a previous run, or stack.md changed since the marker was written
    (steering regeneration). The fresh-environment case is handled by the
    caller via ensure_conda_env()'s return value, which forces a run
    regardless of the marker.
    """
    stack_path = agent_dir / "stack.md"
    if not stack_path.exists():
        return False
    marker = agent_dir / _BOOTSTRAP_MARKER
    if not marker.exists():
        return True
    try:
        return marker.read_text(
            encoding="utf-8").strip() != _stack_hash(stack_path)
    except OSError:
        return True


def mark_bootstrap_complete(agent_dir: Path) -> None:
    """Stamps the bootstrap marker with the current stack.md content hash."""
    stack_path = agent_dir / "stack.md"
    if not stack_path.exists():
        return
    (agent_dir / _BOOTSTRAP_MARKER).write_text(_stack_hash(stack_path),
                                               encoding="utf-8")


def _stack_hash(stack_path: Path) -> str:
    return hashlib.sha256(stack_path.read_bytes()).hexdigest()


def install_bootstrap_packages(
    packages: list[str],
    conda_env: str,
) -> None:
    """
    LEGACY bootstrap path: installs the bootstrap_packages list from
    .agent/run.json into the conda environment using pip. Only used when
    stack.md declares no bootstrap commands — stack.md's Bootstrap Commands
    section supersedes this list entirely (see run_bootstrap_commands).

    Non-fatal: logs a warning on failure rather than halting. The per-task
    dependency scanner will attempt to recover any missing packages anyway.
    """
    if not packages:
        return
    print(f"[CONDA] Installing bootstrap packages: {packages}")
    success = install_packages(packages, "pip", conda_env)
    if not success:
        print(
            "[WARN] Some bootstrap packages failed to install. "
            "The pipeline will continue — the dependency scanner may recover."
        )


def conda_env_exists(env_name: str) -> bool:
    """
    Returns True if the named Conda environment exists.
    Parses output of 'conda env list' — each line contains an env name and path.
    """
    try:
        result = subprocess.run(
            ["conda", "env", "list"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Each line looks like: "agent_app_env    /home/user/miniconda/envs/agent_app_env"
        # We check for the env name as a whole word to avoid partial matches
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and parts[0] == env_name:
                return True
        return False
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def run_in_env(
    cmd: list[str],
    conda_env: str,
    cwd: Path | None = None,
    extra_env: dict | None = None,
    timeout: int | None = None,
    capture: bool = True,
) -> tuple[int, str]:
    """
    Executes a command inside the specified Conda environment using 'conda run'.
    Returns (returncode, combined_stdout_stderr).

    Args:
        cmd:        Command and arguments, e.g. ["pytest", "-v", "--tb=short"]
        conda_env:  Name of the conda environment to run in
        cwd:        Working directory for the subprocess. Defaults to current dir.
        extra_env:  Additional environment variables to inject (merged with os.environ)
        timeout:    Optional subprocess timeout in seconds
        capture:    When False the subprocess inherits this terminal's stdio —
                    required for commands that prompt the user (sudo passwords,
                    interactive installers). Output string is "" in that mode.

    This is the single chokepoint for all conda subprocess calls in the pipeline.
    All test runners, compilers, and package managers go through here.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    full_cmd = ["conda", "run", "--no-capture-output", "-n", conda_env] + cmd

    try:
        if not capture:
            result = subprocess.run(
                full_cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            return result.returncode, ""

        result = subprocess.run(
            full_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr

    except subprocess.TimeoutExpired:
        print(f"[CONDA] Command timed out after {timeout}s: {' '.join(cmd)}")
        return 1, f"TimeoutExpired: command exceeded {timeout}s"

    except FileNotFoundError:
        print("[CRITICAL] 'conda' not found. Is Conda installed and in PATH?")
        sys.exit(1)

    except Exception as e:
        print(f"[CONDA] Unexpected error running '{' '.join(cmd)}': {e}")
        return 1, str(e)


def install_packages(
    packages: list[str],
    manager: str,
    conda_env: str,
    cwd: Path | None = None,
) -> bool:
    """
    Installs one or more packages using pip or npm inside the conda environment.

    Args:
        packages:   List of package names to install
        manager:    'pip' or 'npm'
        conda_env:  Target conda environment
        cwd:        Working directory — must be frontend dir for npm installs

    Returns True on success, False on failure.
    """
    if not packages:
        return True

    if manager == "pip":
        cmd = ["pip", "install", "--quiet"] + packages
    elif manager == "npm":
        cmd = ["npm", "install", "--save"] + packages
    else:
        print(
            f"[WARN] Unknown package manager '{manager}'. Supported: pip, npm")
        return False

    print(f"[CONDA] Installing via {manager}: {packages}")
    returncode, output = run_in_env(cmd, conda_env, cwd=cwd, timeout=300)

    if returncode != 0:
        print(
            f"[WARN] Package install failed (exit {returncode}):\n{output[-500:]}"
        )
        return False

    return True


def get_installed_packages(conda_env: str) -> set[str]:
    """
    Returns set of installed package names (lowercase) in the conda environment.
    Used by deps.py to skip already-installed packages without re-querying pip.
    Parses 'pip list' output — one package per line in 'name version' format.
    """
    returncode, output = run_in_env(
        ["pip", "list", "--format=columns"],
        conda_env,
    )

    if returncode != 0:
        return set()

    packages = set()
    for line in output.splitlines()[2:]:  # skip header rows
        parts = line.strip().split()
        if parts:
            packages.add(parts[0].lower())

    return packages


def _conda_available() -> bool:
    """Returns True if conda is found in PATH."""
    try:
        subprocess.run(
            ["conda", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
