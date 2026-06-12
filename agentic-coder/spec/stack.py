"""
Reader for the stack profile at .agent/stack.md.

stack.md is the single source of truth for how to interact with the target
project's toolchain (see CLAUDE.md — "The stack.md Contract"). It is generated
once during steering (spec/steering.py generate_stack_profile()) and parsed
here into a plain dict for the toolchain layer: the adaptive test runner reads
the command templates, tools/deps.py reads the package manager blocks, and
tools/conda.py reads the bootstrap command list.

The document is Markdown — human and LLM readable. Scalar keys appear as
"key: value" bullet lines anywhere outside the two structured sections. The
"## Package Managers" section holds one "### <name>" block per manager, and
the "## Bootstrap Commands" section holds an ordered list of command entries.
No toolchain name is interpreted anywhere in this module — every value is an
opaque string or template that the executors substitute and run.

Every consumer must tolerate a None return — when stack.md is absent the
pipeline falls back to its legacy behavior (full-suite runs via run.json,
pip/npm dependency handling, run.json bootstrap_packages).
"""

import re
from pathlib import Path

# Scalar keys recognized outside the structured sections. Values are free-form
# strings; command templates use {file}/{test} placeholders that the test
# runner substitutes.
_KNOWN_KEYS = {
    "primary_package_manager",
    "requires_sudo",
    "interactive",
    "build_command",
    "test_suite_command",
    "targeted_test_command",
    "file_test_command",
    "runtime",
    "runtime_check_command",
    "test_function_pattern",
}

# Keys recognized inside a "### <name>" block of the "## Package Managers"
# section. install/uninstall command templates use the {package} placeholder.
_MANAGER_KEYS = {
    "install_command",
    "uninstall_command",
    "requires_sudo",
    "interactive",
    "working_directory",
    "source_file_extensions",
    "dependency_scan_patterns",
}

# Keys recognized per entry inside the "## Bootstrap Commands" section.
# A "command:" line starts a new entry; the other keys attach to it.
_BOOTSTRAP_KEYS = {
    "command",
    "check",
    "check_command",
    "requires_sudo",
    "interactive",
    "reason",
}

# stack.md is Markdown, so a key may appear as a plain "key: value" line or
# as a numbered/bullet/bold/code variant ("1. - **key**: `value`").
_KEY_LINE = re.compile(
    r'^\s*(?:\d+[.)]\s*)?[-*]?\s*\**`?([a-z_]+)`?\**\s*:\s*(.*?)\s*$')
_HEADING = re.compile(r'^(#{2,3})\s+(.+?)\s*$')
_LIST_ITEM = re.compile(r'^\s*(?:\d+[.)]\s*)?[-*]\s+(.+?)\s*$')
_PRIMARY_MARK = re.compile(r'\(\s*primary\s*\)', re.IGNORECASE)


def load_stack_profile(agent_dir: Path) -> dict | None:
    """
    Parses .agent/stack.md into a dict.

    Flat scalar keys (test command templates, runtime info) are top-level
    string entries — unchanged from the original flat reader, so existing
    consumers keep working. Structured data is added under:

        package_managers:   list of dicts (name, primary, install_command,
                            uninstall_command, requires_sudo, interactive,
                            working_directory, source_file_extensions,
                            dependency_scan_patterns)
        bootstrap_commands: ordered list of dicts (command, check,
                            requires_sudo, interactive, reason)

    Returns None when the file is absent, unreadable, or contains nothing
    recognizable — the caller treats None as "stack profile unavailable, use
    legacy fallback behavior".
    """
    path = agent_dir / "stack.md"
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_stack_profile(content)


def stack_profile_missing(agent_dir: Path) -> bool:
    """
    True when .agent/stack.md does not exist. Used by
    steering_needs_generation() to trigger stack-profile-only regeneration.
    """
    return not (agent_dir / "stack.md").exists()


def is_true(value) -> bool:
    """Parses a stack.md boolean value: 'true', 'yes', 'y', '1' → True."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "y", "1"}


def get_primary_manager(profile: dict) -> dict | None:
    """The primary package manager dict, or None when none are declared."""
    managers = profile.get("package_managers") or []
    if not managers:
        return None
    for manager in managers:
        if manager.get("primary"):
            return manager
    return managers[0]


def parse_stack_profile(content: str) -> dict | None:
    """
    Parses stack.md content into the profile dict described in
    load_stack_profile(). Pure function of the text — used by steering
    generation to validate LLM output before writing it to disk.
    """
    profile: dict = {}
    managers: list[dict] = []
    bootstrap: list[dict] = []

    section = "flat"  # "flat" | "managers" | "bootstrap"
    manager: dict | None = None
    boot_entry: dict | None = None
    collecting_patterns = False

    for line in content.splitlines():
        heading = _HEADING.match(line)
        if heading:
            collecting_patterns = False
            level, title = heading.group(1), heading.group(2)
            if level == "##":
                manager = None
                boot_entry = None
                title_lower = title.lower()
                if "package manager" in title_lower:
                    section = "managers"
                elif "bootstrap" in title_lower:
                    section = "bootstrap"
                else:
                    section = "flat"
            elif level == "###" and section == "managers":
                primary = bool(_PRIMARY_MARK.search(title))
                name = _PRIMARY_MARK.sub("", title).strip().strip("`*").strip()
                if name:
                    manager = {
                        "name": name,
                        "primary": primary,
                        "requires_sudo": False,
                        "interactive": False,
                        "source_file_extensions": [],
                        "dependency_scan_patterns": [],
                    }
                    managers.append(manager)
            continue

        key_match = _KEY_LINE.match(line)
        key = key_match.group(1) if key_match else None

        # A dependency_scan_patterns list runs until the next recognized
        # manager key, heading, or blank line. Regex bullets may themselves
        # contain colons, so this check comes before normal key handling.
        if collecting_patterns:
            if key in _MANAGER_KEYS:
                collecting_patterns = False  # fall through to key handling
            elif not line.strip():
                collecting_patterns = False
                continue
            else:
                item = _LIST_ITEM.match(line)
                if item and manager is not None:
                    pattern = _strip_value(item.group(1))
                    if pattern:
                        manager["dependency_scan_patterns"].append(pattern)
                    continue
                collecting_patterns = False

        if not key_match:
            continue
        value = _strip_value(key_match.group(2))

        if section == "managers" and manager is not None and key in _MANAGER_KEYS:
            if key == "dependency_scan_patterns":
                if value:
                    manager[key].append(value)
                else:
                    collecting_patterns = True
            elif key == "source_file_extensions":
                manager[key] = _parse_extensions(value)
            elif key in {"requires_sudo", "interactive"}:
                manager[key] = is_true(value)
            elif value:
                manager[key] = value
        elif section == "bootstrap" and key in _BOOTSTRAP_KEYS:
            if key == "command":
                boot_entry = None
                if value:
                    boot_entry = {
                        "command": value,
                        "requires_sudo": False,
                        "interactive": False,
                    }
                    bootstrap.append(boot_entry)
            elif boot_entry is not None:
                if key in {"requires_sudo", "interactive"}:
                    boot_entry[key] = is_true(value)
                elif key in {"check", "check_command"}:
                    if value:
                        boot_entry["check"] = value
                elif value:
                    boot_entry[key] = value
        elif section == "flat" and key in _KNOWN_KEYS and value:
            profile[key] = value

    if managers:
        profile["package_managers"] = managers
        declared_primary = profile.get("primary_package_manager", "").lower()
        if declared_primary:
            for manager in managers:
                if manager["name"].lower() == declared_primary:
                    manager["primary"] = True
        if not any(m.get("primary") for m in managers):
            managers[0]["primary"] = True
        profile.setdefault(
            "primary_package_manager",
            next(m["name"] for m in managers if m.get("primary")))
    if bootstrap:
        profile["bootstrap_commands"] = bootstrap

    return profile or None


def _strip_value(value: str) -> str:
    """Strips whitespace and surrounding backticks from a parsed value."""
    return value.strip().strip("`").strip()


def _parse_extensions(value: str) -> list[str]:
    """Parses a comma-separated extension list, normalizing the leading dot."""
    extensions = []
    for part in value.split(","):
        part = _strip_value(part)
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        extensions.append(part)
    return extensions
