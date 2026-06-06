from pathlib import Path
from engine.llm import query_llm, clean_and_parse_json, load_config
from tools.conda import install_packages, get_installed_packages


def update_dependencies(
    app_dir: Path,
    root_dir: Path,
    conda_env: str,
) -> None:
    """
    Scans recently modified source files for third-party imports, asks the
    Healer model to identify any packages not yet in the dependency ledger,
    installs them, and updates the ledger.

    Called after every successful Surgeon → Healer cycle, before task state commit.
    Failure is non-fatal — logs a warning and continues.
    """
    ledger_path = root_dir / "dependency_ledger.md"
    config = load_config(root_dir)

    print(f"[DEPS] Scanning for new dependency requirements...")

    # Collect source file content (capped per file to stay within context)
    source_content = _collect_source_content(app_dir, max_chars_per_file=2000)
    if not source_content:
        print("[DEPS] No source files found to scan.")
        return

    existing_ledger = (ledger_path.read_text(encoding="utf-8") if
                       ledger_path.exists() else "No packages installed yet.")

    already_installed = get_installed_packages(conda_env)

    system_prompt = (
        "You are a dependency manager agent. Analyze the provided source files and "
        "identify any third-party packages that are imported but NOT yet listed in "
        "the dependency ledger.\n\n"
        "Return a SINGLE valid JSON object. No markdown fences. No extra text.\n"
        'Format: {"new_dependencies": ["package1", "package2"], "manager": "pip"}\n\n'
        'Use "manager": "npm" for JavaScript/TypeScript packages.\n'
        'Use "manager": "pip" for Python packages.\n'
        "If no new packages are needed, return: {\"new_dependencies\": []}\n\n"
        "IMPORTANT EXCLUSIONS — do NOT flag these as new dependencies:\n"
        "- Python stdlib modules: os, sys, re, json, time, pathlib, subprocess, "
        "  urllib, typing, abc, io, collections, itertools, functools, datetime, "
        "  hashlib, uuid, enum, dataclasses, logging, traceback, inspect, ast, "
        "  shutil, tempfile, glob, threading, asyncio, socket, unittest, etc.\n"
        "- Any package already listed in the dependency ledger.\n"
        "- Any package in the already-installed list provided below.\n")

    user_prompt = (
        f"Already Installed Packages:\n{', '.join(sorted(already_installed)) or 'none'}\n\n"
        f"Dependency Ledger:\n{existing_ledger}\n\n"
        f"Source Files:\n{source_content[:8000]}")

    try:
        response = query_llm("healer", system_prompt, user_prompt, config)
        data = clean_and_parse_json(response)

        new_deps = data.get("new_dependencies", [])
        if not new_deps:
            print("[DEPS] No new dependencies detected.")
            return

        # Filter out anything already installed (double-check)
        truly_new = [d for d in new_deps if d.lower() not in already_installed]
        if not truly_new:
            print("[DEPS] All detected packages already installed.")
            return

        manager = data.get("manager", "pip")
        cwd = app_dir / "frontend" if manager == "npm" else app_dir

        success = install_packages(truly_new, manager, conda_env, cwd=cwd)

        if success:
            append_to_ledger(ledger_path, manager, truly_new)
            print(f"[DEPS] Ledger updated: {truly_new}")

    except Exception as e:
        print(f"[DEPS] Dependency scan skipped due to error: {e}")


def append_to_ledger(ledger_path: Path, manager: str,
                     packages: list[str]) -> None:
    """
    Appends newly installed packages to the dependency ledger.
    Creates the ledger file if it doesn't exist.
    Format: markdown list with manager and package names.
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = f"\n- [{timestamp}] Installed via {manager}: {', '.join(packages)}\n"

    if not ledger_path.exists():
        ledger_path.write_text(
            "# Dependency Ledger\n\nTracks all packages installed by the agent.\n",
            encoding="utf-8",
        )

    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(entry)


def get_ledger_content(root_dir: Path) -> str:
    """Returns dependency ledger content, or a default string if not yet created."""
    ledger_path = root_dir / "dependency_ledger.md"
    if ledger_path.exists():
        return ledger_path.read_text(encoding="utf-8")
    return "No external dependencies installed yet."


def _collect_source_content(app_dir: Path,
                            max_chars_per_file: int = 2000) -> str:
    """
    Collects content from all Python and TypeScript source files under app_dir.
    Caps each file at max_chars_per_file to stay within LLM context budget.
    Skips test files, __pycache__, node_modules, and .bak files.
    """
    content_parts = []

    skip_dirs = {
        "node_modules", "__pycache__", ".git", ".venv", "dist", "build",
        ".next"
    }
    skip_suffixes = {".bak", ".pyc", ".pyo"}

    for fpath in sorted(app_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if any(part in skip_dirs for part in fpath.parts):
            continue
        if fpath.suffix in skip_suffixes:
            continue
        if fpath.suffix not in {".py", ".ts", ".js", ".tsx", ".jsx"}:
            continue
        if fpath.name.startswith("test_") or fpath.name.endswith(".test.ts"):
            continue

        try:
            text = fpath.read_text(encoding="utf-8")[:max_chars_per_file]
            rel = str(fpath.relative_to(app_dir.parent))
            content_parts.append(f"--- {rel} ---\n{text}")
        except Exception:
            continue

    return "\n\n".join(content_parts)
