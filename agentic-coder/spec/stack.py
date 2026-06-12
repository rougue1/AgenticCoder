"""
Reader for the stack profile at .agent/stack.md.

stack.md is the single source of truth for how to interact with the target
project's toolchain (see CLAUDE.md — "The stack.md Contract"). Generation of
the file is not implemented yet; this module only reads a profile when one
exists so the adaptive test runner can derive its command templates from it
instead of hardcoding any test runner syntax.

Every consumer must tolerate a None return — when stack.md is absent the
pipeline falls back to single-phase full-suite execution via run.json.
"""

import re
from pathlib import Path

# Keys the parser recognizes. Values are free-form strings; command templates
# use {file} and {test} placeholders that the test runner substitutes.
_KNOWN_KEYS = {
    "primary_package_manager",
    "requires_sudo",
    "interactive",
    "build_command",
    "test_suite_command",
    "targeted_test_command",
    "file_test_command",
    "runtime",
    "test_function_pattern",
}

# stack.md is Markdown, so a key may appear as a plain "key: value" line or
# as a bullet/bold/code variant ("- **key**: `value`").
_KEY_LINE = re.compile(r'^\s*[-*]?\s*\**`?([a-z_]+)`?\**\s*:\s*(.+?)\s*$')


def load_stack_profile(agent_dir: Path) -> dict | None:
    """
    Parses .agent/stack.md into a flat dict of known keys.

    Surrounding backticks are stripped from values. Returns None when the
    file is absent, unreadable, or contains none of the known keys — the
    caller treats None as "adaptive execution unsupported, run full suite".
    """
    path = agent_dir / "stack.md"
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    profile: dict[str, str] = {}
    for line in content.splitlines():
        match = _KEY_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key not in _KNOWN_KEYS:
            continue
        value = value.strip("`").strip()
        if value:
            profile[key] = value

    return profile or None
