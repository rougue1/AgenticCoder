import re
from pathlib import Path
from engine.llm import query_llm, load_config


def get_next_task(tasks_path: Path) -> str | None:
    """
    Returns the description text of the first unchecked task in tasks.md.
    Handles multiple checkbox format variants produced by different LLMs:
        - [ ] task       (canonical)
        -[ ] task        (missing space before bracket)
        * [ ] task       (bullet variant)
        - [ ]task        (missing space after bracket)

    Returns None if all tasks are complete or file is empty.
    """
    if not tasks_path.exists():
        return None

    with open(tasks_path, "r", encoding="utf-8") as f:
        for line in f:
            task = _parse_unchecked_line(line)
            if task is not None:
                return task

    return None


def count_tasks(tasks_path: Path) -> tuple[int, int]:
    """
    Returns (completed_count, total_count) by scanning tasks.md.
    Used for telemetry summary and progress display.
    Returns (0, 0) if file doesn't exist.
    """
    if not tasks_path.exists():
        return 0, 0

    completed = 0
    total = 0

    with open(tasks_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if _is_task_line(stripped):
                total += 1
                if _is_checked_line(stripped):
                    completed += 1

    return completed, total


def commit_task_complete(
    tasks_path: Path,
    task_desc: str,
    root_dir: Path,
) -> bool:
    """
    Marks the completed task checkbox from '- [ ]' to '- [x]' in tasks.md.

    Strategy (in order):
        1. Deterministic exact string replacement — fast, reliable, no LLM call
        2. Normalized string replacement — strips extra whitespace variants
        3. LLM fallback — for cases where task text drifted from original wording
        4. Truncation guard — rejects LLM output if it's >8% shorter than original

    Returns True on success, False if all strategies fail.
    """
    if not tasks_path.exists():
        print("[STATE] tasks.md not found — cannot commit task state change.")
        return False

    current_md = tasks_path.read_text(encoding="utf-8")

    # ── Strategy 1: exact replacement ──
    for prefix in ["- [ ] ", "-[ ] ", "* [ ] ", "- [ ]"]:
        search = f"{prefix}{task_desc}"
        replace = search.replace("[ ]", "[x]", 1)
        if search in current_md:
            tasks_path.write_text(current_md.replace(search, replace, 1),
                                  encoding="utf-8")
            print(f"[STATE] ✓ Task complete (exact match): {task_desc[:60]}")
            return True

    # ── Strategy 2: normalized whitespace replacement ──
    normalized_desc = re.sub(r'\s+', ' ', task_desc).strip()
    lines = current_md.splitlines()
    for i, line in enumerate(lines):
        parsed = _parse_unchecked_line(line)
        if parsed is not None:
            normalized_line = re.sub(r'\s+', ' ', parsed).strip()
            if normalized_line == normalized_desc:
                lines[i] = line.replace("[ ]", "[x]", 1)
                tasks_path.write_text("\n".join(lines), encoding="utf-8")
                print(
                    f"[STATE] ✓ Task complete (normalized match): {task_desc[:60]}"
                )
                return True

    # ── Strategy 3: LLM fallback ──
    print(
        f"[STATE] Exact match failed — using LLM to locate task: {task_desc[:60]}"
    )
    config = load_config(root_dir)

    system_prompt = (
        "You are a task state manager. Your only job is to locate one specific unchecked "
        "task in the provided tasks.md content and change its checkbox from '[ ]' to '[x]'.\n\n"
        "STRICT RULES:\n"
        "1. Return the COMPLETE, UNMODIFIED tasks.md content with ONLY that one checkbox changed.\n"
        "2. Do NOT truncate, summarize, reorder, remove, or modify any other line.\n"
        "3. Do NOT add markdown code fences around your output.\n"
        "4. Do NOT change any other unchecked items — only the one described.\n"
        "5. Match the task by its meaning, not just exact wording — it may have minor rephrasing.\n"
        "6. If you genuinely cannot find the task, return the file exactly as given with no changes.\n"
    )

    user_prompt = (f"Task to mark complete:\n{task_desc}\n\n"
                   f"Current tasks.md:\n{current_md}")

    response = query_llm("healer", system_prompt, user_prompt, config)

    # Strip LLM formatting wrappers
    updated = response.strip()
    for prefix in ("```markdown\n", "```md\n", "```\n"):
        if updated.startswith(prefix):
            updated = updated[len(prefix):]
            break
    if updated.endswith("```"):
        updated = updated[:-3].strip()

    # ── Strategy 4: truncation guard ──
    if len(updated) < len(current_md) * 0.92:
        print(
            "[CRITICAL] Truncation guard triggered: LLM output is >8% shorter than original.\n"
            "           State change aborted to protect tasks.md integrity.")
        return False

    tasks_path.write_text(updated, encoding="utf-8")
    print(f"[STATE] ✓ Task complete (LLM match): {task_desc[:60]}")
    return True


def normalize_task_line(line: str) -> str | None:
    """
    Normalizes any recognized task line format to canonical '- [ ] text' or '- [x] text'.
    Returns None if the line is not a task line at all.
    """
    parsed = _parse_unchecked_line(line)
    if parsed is not None:
        return f"- [ ] {parsed}"

    parsed = _parse_checked_line(line)
    if parsed is not None:
        return f"- [x] {parsed}"

    return None


# ==========================================
# PRIVATE HELPERS
# ==========================================

# Matches: - [ ] text, -[ ] text, * [ ] text, - [ ]text
_UNCHECKED_PATTERN = re.compile(r'^[-*]\s*\[\s\]\s*(.*)', re.IGNORECASE)

# Matches: - [x] text, - [X] text, * [x] text
_CHECKED_PATTERN = re.compile(r'^[-*]\s*\[[xX]\]\s*(.*)', re.IGNORECASE)


def _parse_unchecked_line(line: str) -> str | None:
    """Returns task description text if line is an unchecked task, else None."""
    match = _UNCHECKED_PATTERN.match(line.strip())
    return match.group(1).strip() if match else None


def _parse_checked_line(line: str) -> str | None:
    """Returns task description text if line is a checked task, else None."""
    match = _CHECKED_PATTERN.match(line.strip())
    return match.group(1).strip() if match else None


def _is_task_line(line: str) -> bool:
    """Returns True if line is any task line (checked or unchecked)."""
    return bool(_UNCHECKED_PATTERN.match(line) or _CHECKED_PATTERN.match(line))


def _is_checked_line(line: str) -> bool:
    """Returns True if line is a completed task."""
    return bool(_CHECKED_PATTERN.match(line))
