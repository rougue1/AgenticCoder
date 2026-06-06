import os
import subprocess
import sys
from pathlib import Path


def ensure_conda_env(env_name: str) -> None:
    """
    Verifies the isolated Conda environment exists.
    Creates it with Python 3.11, Node.js, and pytest if absent.
    Exits the entire process on Conda not found or creation failure —
    there is no recovery path without a working execution environment.
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
        return

    print(
        f"[CONDA] Creating environment '{env_name}' with Python 3.11 + Node.js + pytest..."
    )
    try:
        subprocess.run(
            [
                "conda",
                "create",
                "-y",
                "-n",
                env_name,
                "python=3.11",
                "nodejs",
                "pytest",
                "pytest-flask",
            ],
            check=True,
        )
        print(f"[CONDA] Environment '{env_name}' created successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[CRITICAL] Conda environment creation failed: {e}")
        sys.exit(1)


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

    This is the single chokepoint for all conda subprocess calls in the pipeline.
    All test runners, compilers, and package managers go through here.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    full_cmd = ["conda", "run", "--no-capture-output", "-n", conda_env] + cmd

    try:
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
