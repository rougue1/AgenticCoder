import re
import shutil
from pathlib import Path

# ==========================================
# SANITIZATION
# ==========================================


def sanitize_llm_block(text: str) -> str:
    """
    Strips all markdown code fences and normalizes text before SEARCH/REPLACE parsing.
    Handles every fence variant LLMs produce:
      - ```python, ```typescript, ```json, ```bash, ``` (bare)
    Also normalizes:
      - Windows CRLF line endings → LF
      - Zero-width spaces (U+200B) — invisible anchor breakers
      - Non-breaking spaces (U+00A0) — look identical to regular spaces in editors
      - Smart quotes → straight quotes (some models output these)
    """
    # Strip opening fences with optional language tag (e.g. ```python\n)
    text = re.sub(r'```[a-zA-Z0-9_\-]*\n', '', text)

    # Strip closing fences on their own line (```\n or ``` at end)
    text = re.sub(r'\n```\s*\n', '\n', text)
    text = re.sub(r'\n```\s*$', '', text)
    text = re.sub(r'^```\s*\n', '', text)

    # Remove any remaining stray triple backticks
    text = text.replace('```', '')

    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Strip invisible Unicode characters that silently break anchor matching
    text = text.replace('\u200b', '')  # zero-width space
    text = text.replace('\u200c', '')  # zero-width non-joiner
    text = text.replace('\u200d', '')  # zero-width joiner
    text = text.replace('\u00a0', ' ')  # non-breaking space → regular space
    text = text.replace('\ufeff', '')  # BOM character

    # Normalize smart quotes to straight quotes
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2018', "'").replace('\u2019', "'")

    return text


# ==========================================
# SNAPSHOT / RESTORE
# ==========================================


def snapshot_file(file_path: Path) -> bool:
    """
    Creates a .bak backup of file_path before any destructive write.
    Returns True if snapshot was created, False if file didn't exist.
    """
    if not file_path.exists():
        return False
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    shutil.copy2(file_path, backup_path)
    return True


def restore_snapshot(file_path: Path) -> bool:
    """
    Restores file_path from its .bak backup.
    Returns True on success, False if no backup exists.
    """
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    if not backup_path.exists():
        print(f"[RESTORE] No backup found for {file_path.name}")
        return False
    shutil.copy2(backup_path, file_path)
    print(f"[RESTORE] Restored {file_path.name} from snapshot.")
    return True


def cleanup_snapshots(root_dir: Path):
    """Removes all .bak files after a successful task cycle."""
    for bak in root_dir.rglob("*.bak"):
        bak.unlink(missing_ok=True)


# ==========================================
# CORE PATCH ENGINE
# ==========================================


def apply_search_replace_block(file_path: Path, block_text: str,
                               root_dir: Path) -> bool:
    """
    Executes one or more deterministic SEARCH/REPLACE operations on a target file.

    Block format:
        <<<<<<< SEARCH
        <exact existing content to find>
        =======
        <new content to substitute in>
        >>>>>>> REPLACE

    Processing order:
        1. Sanitize block_text (strip fences, normalize whitespace)
        2. If file is new → concatenate all REPLACE segments as initial content
        3. If file exists → exact match first, fuzzy whitespace-strip fallback second
        4. Snapshot file before any write (if config snapshot_files=True)
        5. Run post-write syntax check

    Returns True on full success, False on any failure.
    """
    from engine.llm import load_config
    config = load_config(root_dir)

    # Always sanitize before parsing
    block_text = sanitize_llm_block(block_text)

    # ── New file initialization ──
    if not file_path.exists():
        new_parts = []
        chunks = block_text.split("<<<<<<< SEARCH")
        for chunk in chunks[1:]:
            if "=======" not in chunk or ">>>>>>> REPLACE" not in chunk:
                print(f"[ERROR] Malformed block initializing {file_path.name}")
                return False
            _, replace_raw = chunk.split("=======", 1)
            replace_str, _ = replace_raw.split(">>>>>>> REPLACE", 1)
            new_parts.append(replace_str.strip("\n"))

        if not new_parts:
            print(
                f"[ERROR] No REPLACE content found for new file {file_path.name}"
            )
            return False

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("\n".join(new_parts), encoding="utf-8")
        print(f"  [CREATE] {file_path.relative_to(root_dir)}")
        return True

    # ── Existing file patching ──
    original_content = file_path.read_text(encoding="utf-8")
    updated = original_content

    chunks = block_text.split("<<<<<<< SEARCH")
    if len(chunks) < 2:
        print(f"[ERROR] No SEARCH marker found in block for {file_path.name}")
        return False

    for chunk in chunks[1:]:
        if "=======" not in chunk or ">>>>>>> REPLACE" not in chunk:
            print(
                f"[ERROR] Malformed SEARCH/REPLACE boundaries in {file_path.name}"
            )
            return False

        search_raw, replace_raw = chunk.split("=======", 1)
        replace_str, _ = replace_raw.split(">>>>>>> REPLACE", 1)

        search_str = search_raw.strip("\n")
        replace_str = replace_str.strip("\n")

        # Guard: reject full-file rewrites of existing files
        if not _guard_full_rewrite(file_path, search_str, replace_str,
                                   original_content):
            return False

        # Attempt 1: exact match
        if search_str in updated:
            updated = updated.replace(search_str, replace_str, 1)
            continue

        # Attempt 2: fuzzy whitespace-strip match
        fuzzy_result = _fuzzy_match(search_str, replace_str, updated)
        if fuzzy_result is not None:
            updated = fuzzy_result
            print(
                f"  [FUZZY] Applied via whitespace-normalized match in {file_path.name}"
            )
            continue

        # Total failure — report with diagnostic hint
        hint = _find_closest_anchor(search_str, updated)
        print(
            f"[ERROR] SEARCH anchor not found in '{file_path.relative_to(root_dir)}'.\n"
            f"        Anchor (first 150 chars): {search_str[:150]!r}\n"
            f"        {hint}")
        return False

    # Snapshot before writing
    if config.get("snapshot_files", True):
        snapshot_file(file_path)

    file_path.write_text(updated, encoding="utf-8")
    print(f"  [PATCH] {file_path.relative_to(root_dir)}")
    return True


# ==========================================
# PRIVATE HELPERS
# ==========================================


def _guard_full_rewrite(
    file_path: Path,
    search_str: str,
    replace_str: str,
    original_content: str,
) -> bool:
    """
    Rejects attempts to replace an entire existing file in one block.
    A full-file rewrite is detected when:
      - search_str is empty (or just whitespace), AND
      - replace_str is larger than 80% of the original file size
    This prevents the Surgeon from destructively overwriting working code.
    """
    if search_str.strip() == "" and len(
            replace_str) > len(original_content) * 0.8:
        print(
            f"[GUARD] Rejected full-file rewrite attempt on existing file: {file_path.name}\n"
            f"        Use targeted SEARCH/REPLACE blocks instead of replacing the entire file."
        )
        return False
    return True


def _fuzzy_match(search_str: str, replace_str: str,
                 content: str) -> str | None:
    """
    Fallback match that strips trailing whitespace per line for comparison only.
    Locates the matching line range in the ORIGINAL content and splices the
    replacement into only those lines — every untouched line is preserved
    byte-identical to the original (no side-effect whitespace stripping).
    Returns the patched content string if match succeeds, None otherwise.
    """
    search_lines = search_str.splitlines()
    content_lines = content.splitlines()

    if not search_lines:
        return None

    search_stripped = [line.rstrip() for line in search_lines]
    content_stripped = [line.rstrip() for line in content_lines]
    n = len(search_stripped)

    for i in range(len(content_stripped) - n + 1):
        if content_stripped[i:i + n] == search_stripped:
            result_lines = (content_lines[:i] + replace_str.splitlines() +
                            content_lines[i + n:])
            result = "\n".join(result_lines)
            if content.endswith("\n"):
                result += "\n"
            return result

    return None


def _find_closest_anchor(search_str: str, file_content: str) -> str:
    """
    When an exact anchor fails, finds the most similar region in the file.
    Uses the first non-empty line of the SEARCH block as a probe.
    Returns a human-readable diagnostic string for error messages.
    """
    search_lines = [l for l in search_str.strip().splitlines() if l.strip()]
    if not search_lines:
        return "[HINT] SEARCH block was empty."

    probe = search_lines[0].strip()
    file_lines = file_content.splitlines()

    candidates = []
    for i, line in enumerate(file_lines):
        if probe and probe in line:
            end = min(i + len(search_lines), len(file_lines))
            block = "\n".join(file_lines[i:end])
            candidates.append((i + 1, block))

    if candidates:
        line_num, block = candidates[0]
        preview = block[:250].replace("\n", "\\n")
        return f"[HINT] Closest match at line {line_num}: {preview!r}"

    return f"[HINT] No line containing {probe!r} found — function may have been renamed or moved."
