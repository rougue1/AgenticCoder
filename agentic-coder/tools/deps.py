"""
Per-task dependency detection and installation.

Primary path: package manager routing driven entirely by .agent/stack.md.
Each manager block in stack.md declares an install_command template with a
{package} placeholder, the source file extensions its dependencies are
declared in, and regex scan patterns identifying those declarations. The
routing layer substitutes the placeholder and executes the result — no
package manager, language, or toolchain name appears in this code.

Legacy fallback: when stack.md is absent (or declares no package managers),
the original pip/npm behavior is used unchanged so existing projects keep
working without modification.

The LLM call in both paths is one of the two steering-bypass exemptions
(see CLAUDE.md) — it does not go through build_system_prompt().
"""

import re
from fnmatch import fnmatch
from pathlib import Path

from engine.llm import query_llm, clean_and_parse_json, load_config
from spec.stack import load_stack_profile
from tools.conda import (
    install_packages,
    get_installed_packages,
    run_shell_command,
)
from tools.permissions import confirm_command

_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "dist", "build", ".next"
}

# Fallback-path extensions only — the stack path derives extensions from the
# source_file_extensions declared per package manager in stack.md.
_LEGACY_EXTENSIONS = {".py", ".ts", ".js", ".tsx", ".jsx"}


def update_dependencies(
    app_dir: Path,
    root_dir: Path,
    conda_env: str,
) -> None:
    """
    Scans recently modified source files for external dependency
    declarations, asks the Healer model to identify any packages not yet in
    the dependency ledger, installs them via the package manager(s) declared
    in stack.md, and updates the ledger.

    Called after every Surgeon cycle, before the Healer loop, so the first
    test run already has all packages the Surgeon introduced.
    Failure is non-fatal — logs a warning and continues.
    """
    profile = load_stack_profile(root_dir / ".agent")
    managers = (profile.get("package_managers") if profile else None) or []

    if managers:
        _update_dependencies_stack(app_dir, root_dir, conda_env, managers)
    else:
        print("[DEPS] No package managers declared in .agent/stack.md — "
              "falling back to legacy pip/npm detection.\n"
              "       Generate stack.md (rerun steering) to enable "
              "stack-aware dependency routing.")
        _update_dependencies_legacy(app_dir, root_dir, conda_env)


# ==========================================
# STACK.MD ROUTING PATH
# ==========================================


def _update_dependencies_stack(
    app_dir: Path,
    root_dir: Path,
    conda_env: str,
    managers: list[dict],
) -> None:
    """
    stack.md-driven path: scan source files using each manager's declared
    patterns, let the LLM confirm which declarations are genuinely new
    external dependencies and assign each to one of the declared managers,
    then install via that manager's command template.
    """
    config = load_config(root_dir)
    ledger_path = root_dir / "dependency_ledger.md"

    print("[DEPS] Scanning for new dependency declarations "
          "(stack.md routing)...")

    test_glob = _test_file_glob(root_dir)
    extensions = sorted({
        ext
        for manager in managers
        for ext in manager.get("source_file_extensions", [])
    })

    source_content = _collect_source_content(app_dir,
                                             extensions=extensions or None,
                                             test_glob=test_glob)
    if not source_content:
        print("[DEPS] No source files found to scan.")
        return

    candidates = _scan_dependency_candidates(app_dir, managers, test_glob)

    existing_ledger = (ledger_path.read_text(encoding="utf-8") if
                       ledger_path.exists() else "No packages installed yet.")

    manager_names = [m["name"] for m in managers]
    system_prompt = (
        "You are a dependency manager agent for an autonomous coding "
        "pipeline. Analyze the provided source files and identify external "
        "packages that are declared or imported but NOT yet listed in the "
        "dependency ledger.\n\n"
        "The project's available package managers are EXACTLY: "
        f"{', '.join(manager_names)}.\n"
        "Assign every new dependency to one of those manager names — never "
        "invent another manager.\n"
        "Use the real installable package name (the name the package manager "
        "expects), which may differ from the name used in source code.\n\n"
        "Return a SINGLE valid JSON object. No markdown fences. No extra "
        "text.\n"
        'Format: {"new_dependencies": '
        '[{"package": "name", "manager": "manager-name"}]}\n'
        'If no new packages are needed: {"new_dependencies": []}\n\n'
        "Do NOT flag as new dependencies:\n"
        "- Modules that ship with the language's standard library or "
        "built-in runtime.\n"
        "- Internal project modules (anything defined by the source files "
        "themselves).\n"
        "- Any package already listed in the dependency ledger.\n")

    candidate_summary = "\n".join(
        f"  {name}: {', '.join(found) if found else '(none found)'}"
        for name, found in candidates.items())

    user_prompt = (
        f"Dependency Ledger:\n{existing_ledger}\n\n"
        f"Declarations found by the pattern scanner (per package manager — "
        f"hints, not authoritative):\n"
        f"{candidate_summary or '  (no scan patterns declared)'}\n\n"
        f"Source Files:\n{source_content[:8000]}")

    try:
        response = query_llm("healer", system_prompt, user_prompt, config)
        data = clean_and_parse_json(response)
    except Exception as e:
        print(f"[DEPS] Dependency scan skipped due to error: {e}")
        return

    entries = data.get("new_dependencies", [])
    if not entries:
        print("[DEPS] No new dependencies detected.")
        return

    managers_by_name = {m["name"].lower(): m for m in managers}
    primary_name = next(
        (m["name"] for m in managers if m.get("primary")), managers[0]["name"])

    by_manager: dict[str, list[str]] = {}
    for entry in entries:
        if isinstance(entry, str):
            # Tolerate flat package-name strings — route to the primary manager.
            package, manager_name = entry, primary_name
        elif isinstance(entry, dict):
            package = entry.get("package", "")
            manager_name = entry.get("manager", primary_name)
        else:
            continue
        if not package:
            continue
        manager = managers_by_name.get(str(manager_name).lower())
        if manager is None:
            print(f"[DEPS] Skipping '{package}' — manager "
                  f"'{manager_name}' is not declared in stack.md.")
            continue
        bucket = by_manager.setdefault(manager["name"], [])
        if package not in bucket:
            bucket.append(package)

    for manager in managers:
        packages = by_manager.get(manager["name"])
        if not packages:
            continue
        installed = _install_via_manager(manager, packages, conda_env,
                                         root_dir, config)
        if installed:
            append_to_ledger(ledger_path, manager["name"], installed)
            print(f"[DEPS] Ledger updated ({manager['name']}): {installed}")


def _install_via_manager(
    manager: dict,
    packages: list[str],
    conda_env: str,
    root_dir: Path,
    config: dict,
) -> list[str]:
    """
    Installs packages using the manager's install_command template from
    stack.md ({package} placeholder substituted per package). Commands the
    manager marks requires_sudo or interactive go through the permission
    gate once per batch; a decline skips the whole batch and the pipeline
    continues. Returns the packages that installed successfully.
    """
    template = manager.get("install_command")
    if not template or "{package}" not in template:
        print(f"[WARN] Package manager '{manager['name']}' has no usable "
              "install_command template in stack.md — skipping "
              f"{packages}.")
        return []

    working_dir = manager.get("working_directory")
    cwd = (root_dir / working_dir) if working_dir else root_dir

    requires_sudo = manager.get("requires_sudo", False)
    interactive = manager.get("interactive", False)

    commands = [(pkg, template.replace("{package}", pkg)) for pkg in packages]

    if requires_sudo or interactive:
        display = "\n".join(cmd for _, cmd in commands)
        if not confirm_command(
                display,
                f"Install new dependencies via {manager['name']}: "
                f"{', '.join(packages)}",
                requires_sudo,
                interactive,
                config,
        ):
            return []

    passthrough = requires_sudo or interactive
    installed = []
    for package, command in commands:
        print(f"[DEPS] Installing via {manager['name']}: {package}")
        code, output = run_shell_command(
            command,
            conda_env,
            cwd=cwd,
            capture=not passthrough,
            timeout=None if passthrough else 600,
        )
        if code == 0:
            installed.append(package)
        else:
            tail = f"\n{output[-500:]}" if output else ""
            print(f"[WARN] Install failed for '{package}' "
                  f"(exit {code}):{tail}")
    return installed


def _scan_dependency_candidates(
    app_dir: Path,
    managers: list[dict],
    test_glob: str | None,
) -> dict[str, list[str]]:
    """
    Regex scan of source files using each manager's dependency_scan_patterns
    from stack.md. Each pattern's first capture group is the declared
    dependency name. Results are hints for the LLM filter, keyed by manager
    name — managers without patterns simply contribute no candidates.
    """
    routes: dict[str, list[tuple[str, list[re.Pattern]]]] = {}
    for manager in managers:
        compiled = []
        for pattern in manager.get("dependency_scan_patterns", []):
            try:
                compiled.append(re.compile(pattern, re.MULTILINE))
            except re.error as e:
                print(f"[DEPS] Ignoring invalid scan pattern for "
                      f"'{manager['name']}': {pattern!r} ({e})")
        if not compiled:
            continue
        for ext in manager.get("source_file_extensions", []):
            routes.setdefault(ext, []).append((manager["name"], compiled))

    results: dict[str, list[str]] = {m["name"]: [] for m in managers}
    if not routes:
        return results

    seen: dict[str, set[str]] = {m["name"]: set() for m in managers}

    for fpath in sorted(app_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if any(part in _SKIP_DIRS for part in fpath.parts):
            continue
        if fpath.suffix not in routes:
            continue
        if _is_test_file(fpath.name, test_glob):
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for manager_name, patterns in routes[fpath.suffix]:
            for pattern in patterns:
                for match in pattern.finditer(content):
                    name = (match.group(1)
                            if match.groups() else match.group(0)).strip()
                    if name and name not in seen[manager_name]:
                        seen[manager_name].add(name)
                        results[manager_name].append(name)

    return results


def _test_file_glob(root_dir: Path) -> str | None:
    """Test filename glob from run.json, if steering has produced one."""
    from testing.runner import load_run_config
    run_config = load_run_config(root_dir)
    if run_config:
        return run_config.get("test_file_glob", "test_*.py")
    return None


# ==========================================
# LEGACY FALLBACK PATH (stack.md absent)
# ==========================================


def _update_dependencies_legacy(
    app_dir: Path,
    root_dir: Path,
    conda_env: str,
) -> None:
    """
    Original pip/npm behavior, retained verbatim as the fallback when
    stack.md declares no package managers. Stack names are permitted here
    only because this is a fallback path.
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


# ==========================================
# LEDGER
# ==========================================


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


# ==========================================
# SOURCE COLLECTION
# ==========================================


def _collect_source_content(
    app_dir: Path,
    max_chars_per_file: int = 2000,
    extensions: list[str] | set[str] | None = None,
    test_glob: str | None = None,
) -> str:
    """
    Collects content from source files under app_dir for the LLM scan,
    capped at max_chars_per_file each to stay within context budget.

    extensions: which file suffixes count as source. The stack path passes
    the union of source_file_extensions from stack.md; None means the legacy
    hardcoded set (fallback path only).
    test_glob: test filename glob from run.json. None means the legacy
    test-name heuristics (fallback path only).
    Skips __pycache__-style build dirs and .bak snapshot files either way.
    """
    content_parts = []

    suffixes = set(extensions) if extensions else _LEGACY_EXTENSIONS
    skip_suffixes = {".bak", ".pyc", ".pyo"}

    for fpath in sorted(app_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if any(part in _SKIP_DIRS for part in fpath.parts):
            continue
        if fpath.suffix in skip_suffixes:
            continue
        if fpath.suffix not in suffixes:
            continue
        if _is_test_file(fpath.name, test_glob):
            continue

        try:
            text = fpath.read_text(encoding="utf-8")[:max_chars_per_file]
            rel = str(fpath.relative_to(app_dir.parent))
            content_parts.append(f"--- {rel} ---\n{text}")
        except Exception:
            continue

    return "\n\n".join(content_parts)


def _is_test_file(filename: str, test_glob: str | None) -> bool:
    """
    Test-file detection: the run.json test_file_glob when available,
    legacy name heuristics otherwise (fallback path only).
    """
    if test_glob:
        return fnmatch(filename, test_glob)
    return filename.startswith("test_") or filename.endswith(".test.ts")
