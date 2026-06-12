from pathlib import Path
from engine.patch import apply_search_replace_block, sanitize_llm_block


def extract_file_chunks(ai_response: str) -> list[tuple[str, str]]:
    """
    Parses a multi-file LLM response into (relative_path, block_content) tuples.
    Does NOT write any files — pure parsing only.
    Used by preflight validators to inspect Surgeon output before applying it.

    Expected format:
        ### FILE: path/to/file.py
        <<<<<<< SEARCH
        ...
        =======
        ...
        >>>>>>> REPLACE

    Returns list of (path_str, block_content) tuples.
    Returns empty list if no valid FILE markers found.
    """
    chunks = ai_response.split("### FILE: ")
    results = []

    for chunk in chunks[1:]:  # index 0 is pre-marker preamble — always skip
        if not chunk.strip():
            continue

        lines = chunk.strip().splitlines()
        if not lines:
            continue

        relative_path = lines[0].strip()
        block_content = "\n".join(lines[1:])

        # Skip sections with no SEARCH/REPLACE content
        if "<<<<<<< SEARCH" not in block_content:
            continue

        results.append((relative_path, block_content))

    return results


def splice_multi_file_response(ai_response: str, root_dir: Path) -> list[str]:
    """
    Parses and applies all file patches from a unified LLM response.
    Each file section must be prefixed with '### FILE: relative/path/to/file'.

    Processing:
        1. Split response on '### FILE:' markers
        2. For each chunk: extract path + block content
        3. Sanitize block (strip fences, normalize whitespace)
        4. Delegate to apply_search_replace_block()

    Returns the list of relative paths whose patches were fully applied.
    An empty list means nothing was written to disk. Failed patches are
    logged and skipped — processing continues with the remaining files to
    maximize the number of patches applied.

    Logs a warning (not error) if no valid blocks are found — the caller
    (orchestrator) decides whether that's fatal for the current task.
    """
    chunks = extract_file_chunks(ai_response)

    if not chunks:
        if "<<<<<<< SEARCH" not in ai_response:
            print(
                "[WARN] No '### FILE:' markers or SEARCH/REPLACE blocks found in response."
            )
        else:
            # Has SEARCH blocks but no FILE markers — try treating entire response
            # as a single block for a known root file (shouldn't happen but defensive)
            print(
                "[WARN] SEARCH/REPLACE found but no ### FILE: markers. Cannot determine target file."
            )
        return []

    patched: list[str] = []
    for relative_path, block_content in chunks:
        target_path = root_dir / relative_path
        print(f"[SPLICE] → {relative_path}")
        if apply_search_replace_block(target_path, block_content, root_dir):
            patched.append(relative_path)
        # On failure: continue to next file rather than aborting

    return patched


def count_file_patches(ai_response: str) -> int:
    """Returns the number of file patches detected in an LLM response. Used for validation."""
    return len(extract_file_chunks(ai_response))
