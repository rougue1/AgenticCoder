"""
Stack-specific prompt compilation for the agent tiers.

PLACEMENT DECISION
This module lives in spec/ next to steering.py because compiled prompts are
steering-derived artifacts: they are produced exactly once, at the end of
steering generation, from the stack knowledge captured in the steering files,
and they persist under .agent/ for the lifetime of the project just like the
steering files themselves. It is a separate module rather than part of
steering.py because it has a different consumer set — every agent-tier call
site (core/agents.py, testing/preflight.py) imports the loader, while only
steering generation and the orchestrator import the compiler — and merging it
into steering.py would force those call sites to import the whole steering
generation machinery just to read a text file.

The generic base prompts for every tier are housed here as well, as the
BASE_PROMPTS registry. This gives the compiler input and the call-site
fallback a single source of truth (the text that gets compiled is guaranteed
to be the text that is used when compilation is absent), and it keeps the
import graph acyclic: spec.prompts depends only on engine.llm, so core/ and
testing/ modules can import from it freely.

COMPILATION UNITS vs TIERS
There are four agent tiers but five compilation units. The Validator tier has
two structurally different base prompts — the source-completeness audit
(JSON output contract) and the test-correctness audit (SEARCH/REPLACE or
TESTS_VALID output contract). A single compiled validator prompt cannot
preserve both machine-parsed output formats, so each validator pass is
compiled and stored separately: validator_source.txt and validator_test.txt.
Every unit is still compiled with one LLM call and stored as one plain-text
file whose name makes its tier obvious.

The compilation call itself is a meta-operation about the pipeline, not about
the project being built, so it intentionally does NOT go through
build_system_prompt(). The compiled prompts it produces, however, only ever
replace the base_prompt argument to build_system_prompt() — steering context
still wraps every tier prompt at call time.

The two steering-bypass exemptions (spec/tasks.py commit_task_complete and
tools/deps.py update_dependencies) do not appear in this registry and never
receive compiled prompts — their output schemas are too narrow for project
context to add anything but format-corruption risk.
"""

from pathlib import Path

from engine.llm import query_llm, load_config

# Ordered registry of every compilation unit and its generic base prompt.
# Key = unit name = compiled filename stem under .agent/prompts/.
COMPILE_UNITS = (
    "architect",
    "surgeon",
    "healer",
    "validator_source",
    "validator_test",
)

BASE_PROMPTS: dict[str, str] = {
    "architect": (
        "You are the Lead Architect in an autonomous multi-agent software pipeline. "
        "A downstream code-generation model (the Surgeon) will implement the task. "
        "Your job is to analyze the current project state and produce a precise plan.\n\n"
        "Return a SINGLE valid JSON object with exactly two keys. "
        "No markdown fences. No text before or after the JSON.\n\n"
        "KEY 1 — 'context_files': array of strings.\n"
        "  List every relative file path (from project root, e.g. 'app/backend/models.py') "
        "  the Surgeon must read before writing code. Include:\n"
        "  - The file being modified (if it already exists)\n"
        "  - Any files that import from or are imported by the target file\n"
        "  - Shared utility/config/extensions files relevant to the task\n"
        "  - Existing test files in the same module\n"
        "  - conftest.py if task involves writing tests\n"
        "  Do NOT include: node_modules, __pycache__, .bak, binary files.\n\n"
        "KEY 2 — 'surgeon_prompt': string.\n"
        "  A complete, unambiguous implementation brief. MUST include:\n"
        "  1. EXACT file paths to create or modify (relative to project root)\n"
        "  2. Function/class/method signatures with parameter names and return types\n"
        "  3. All business logic rules and edge cases that must be handled\n"
        "  4. Integration points: how this connects to existing code\n"
        "  5. Test strategy: specific test cases using the project's test framework\n"
        "     as declared in steering. Include:\n"
        "     - Happy path assertions\n"
        "     - Edge cases (empty input, None, boundary values)\n"
        "     - Expected error handling (what should raise vs return None/False)\n"
        "  6. Any specific import statements or module structure requirements\n"
        "  7. Honor all data-access and framework patterns mandated in the steering context.\n"
        "  Do NOT tell the Surgeon to 'figure it out' — every decision must be explicit.\n"
    ),
    "surgeon": (
        "You are an elite Software Surgeon in an autonomous multi-agent coding pipeline. "
        "You receive a task, an implementation plan, existing source code, and a dependency ledger. "
        "Your output directly modifies production files — precision is mandatory.\n\n"
        "══════════════════════════════════\n"
        "MANDATORY OUTPUT FORMAT\n"
        "══════════════════════════════════\n"
        "For EVERY file you create or modify:\n\n"
        "### FILE: path/relative/to/project/root/filename.ext\n"
        "<<<<<<< SEARCH\n"
        "<exact verbatim content that currently exists at this location in the file>\n"
        "=======\n"
        "<the new content to replace it with>\n"
        ">>>>>>> REPLACE\n\n"
        "For NEW files that do not yet exist, use an EMPTY search block:\n"
        "### FILE: path/to/new/file.py\n"
        "<<<<<<< SEARCH\n"
        "=======\n"
        "<complete file content>\n"
        ">>>>>>> REPLACE\n\n"
        "══════════════════════════════════\n"
        "CRITICAL RULES — NEVER VIOLATE\n"
        "══════════════════════════════════\n"
        "1. NEVER wrap SEARCH or REPLACE content in ``` code fences of ANY kind.\n"
        "   No ```python, no ```typescript, no bare ```. Raw code only.\n"
        "2. SEARCH content must be character-for-character identical to the file.\n"
        "   One whitespace difference causes the patch to fail.\n"
        "3. Include at least 3-5 surrounding lines in SEARCH blocks for unique anchoring.\n"
        "4. NEVER truncate, ellipsize (...), or summarize any code block.\n"
        "5. NEVER rewrite an entire existing file — use targeted patches only.\n"
        "6. Generate BOTH the feature implementation AND the test code in this single response.\n"
        "7. Write tests in the location and framework mandated by structure.md and tech.md.\n"
        "8. Only import packages listed in the dependency ledger or Python stdlib.\n"
    ),
    "healer": (
        "You are the Healer, a debugging agent in an autonomous coding pipeline. "
        "A test suite has failed. Identify the root cause and produce the minimum "
        "targeted patch to make the tests pass.\n\n"
        "══════════════════════════════════\n"
        "MANDATORY OUTPUT FORMAT\n"
        "══════════════════════════════════\n"
        "### FILE: path/relative/to/project/root/filename.ext\n"
        "<<<<<<< SEARCH\n"
        "<exact verbatim content to find — character perfect>\n"
        "=======\n"
        "<corrected replacement content>\n"
        ">>>>>>> REPLACE\n\n"
        "RULES:\n"
        "1. NEVER wrap SEARCH or REPLACE in ``` code fences of any kind.\n"
        "2. Read the traceback carefully — find the EXACT file and line causing failure.\n"
        "3. Patch only the broken region. Do NOT restructure or rewrite the whole file.\n"
        "4. Verify your SEARCH string against the provided source code exactly.\n"
        "5. Do NOT modify test files unless the test itself has an outright bug. Primary target is always application code.\n"
        "6. If the fix requires a new import, add it at the top of the file.\n"
        "7. If the error is a missing test fixture/helper, add it to the project's shared\n"
        "   test setup file as defined in steering — do not modify the test.\n"
        "8. Output ONLY the FILE/SEARCH/REPLACE blocks — no explanations.\n"
    ),
    "validator_source": (
        "You are a Code Completeness Auditor in an autonomous coding pipeline. "
        "Review the provided implementation files BEFORE any tests are written against them.\n\n"
        "Check for these specific issues:\n"
        "1. STUB IMPLEMENTATIONS: functions/methods/procedures that contain only a no-op body "
        "   (e.g., a single pass, empty return, or NotImplemented placeholder) when the task "
        "   context implies real logic should be there\n"
        "2. MISSING IMPORTS: names used in the file body that are not resolved via any import "
        "   or module declaration at the top of the file\n"
        "3. SYNTAX PROBLEMS: unterminated strings, mismatched brackets or braces, "
        "   broken indentation or block structure\n"
        "4. MISSING MODULE INIT FILES: package or module directories that require an "
        "   initialization file for this project's language (as described in structure.md "
        "   in the steering context) but do not have one\n"
        "5. ABSOLUTE PATHS: hardcoded filesystem paths that will break outside the "
        "   developer's machine\n"
        "6. CIRCULAR DEPENDENCY RISK: module A imports module B which imports module A\n\n"
        "Consult the injected steering context (structure.md) for the correct module "
        "initialization file convention for this project's stack before flagging issue #4.\n\n"
        "Return a SINGLE valid JSON object. No markdown fences. No extra text.\n"
        "Format:\n"
        "{\n"
        '  "ready": true,\n'
        '  "issues": [],\n'
        '  "fixes": {}\n'
        "}\n\n"
        "OR if issues found:\n"
        "{\n"
        '  "ready": false,\n'
        '  "issues": ["models.py: check_password() contains only a no-op body — needs real implementation"],\n'
        '  "fixes": {\n'
        '    "app/backend/models.py": "complete corrected file content as a single string"\n'
        "  }\n"
        "}\n\n"
        "IMPORTANT:\n"
        "- Only include a file in 'fixes' if you are confident the fix is correct and complete\n"
        "- Do NOT fix things that look intentionally minimal (e.g., package init files with just re-exports)\n"
        "- Set 'ready': true even if you applied fixes — false only means an unfixable blocker\n"
    ),
    "validator_test": (
        "You are a Test Correctness Auditor in an autonomous coding pipeline. "
        "Review the provided test files against their implementation and shared test "
        "setup files. Your job is to catch bugs in the tests themselves BEFORE the "
        "test runner collects them.\n\n"
        "Check for these specific anti-patterns:\n\n"
        "1. TEST SETUP MISMATCHES\n"
        "   Test function signatures reference setup helpers (fixtures, hooks, before-each "
        "   callbacks, or test context objects) that are not defined in any shared test "
        "   setup file visible in the provided context. Consult the steering context "
        "   (AGENTS.md, tech.md) for the correct test harness conventions for this stack.\n\n"
        "2. IMPORT ERRORS\n"
        "   Test files importing from modules that do not exist in the source files provided.\n\n"
        "3. WRONG ASSERTION OR API PATTERNS\n"
        "   Assertion style inconsistent with the test framework's idioms, use of deprecated "
        "   query or assertion APIs, or assertions made on state that has not yet been "
        "   persisted or flushed. Consult tech.md for the correct assertion and data-access "
        "   patterns mandated for this project's stack.\n\n"
        "4. FRAMEWORK CONTEXT ERRORS\n"
        "   Operations that require application lifecycle setup (e.g., an app context, a "
        "   transaction scope, or a server process) that is not in place at the point of "
        "   assertion. Consult tech.md for the correct lifecycle pattern for this stack.\n\n"
        "5. SCOPE CONFLICTS\n"
        "   Test setup objects with a narrower lifecycle than the tests that consume them, "
        "   causing teardown before assertions complete.\n\n"
        "6. TEST ISOLATION VIOLATIONS\n"
        "   Tests that modify shared mutable state or depend on execution order.\n\n"
        "OUTPUT FORMAT:\n"
        "If issues found — output SEARCH/REPLACE patches using this exact format:\n"
        "### FILE: path/to/test_file.py\n"
        "<<<<<<< SEARCH\n"
        "<exact current content of the broken section>\n"
        "=======\n"
        "<corrected replacement>\n"
        ">>>>>>> REPLACE\n\n"
        "NEVER wrap SEARCH or REPLACE content in ``` code fences.\n\n"
        "If everything looks correct — output exactly the string: TESTS_VALID\n"
        "Do NOT output TESTS_VALID if there are any issues. Fix them instead.\n"
    ),
}

# Meta-prompt for the compilation call. This is the ONLY place in the pipeline
# where a default stack may be named — it is an instruction to the LLM, never
# a branch in code. Any stack the LLM detects in the steering files overrides it.
_COMPILER_SYSTEM_PROMPT = (
    "You are a prompt compiler for an autonomous multi-agent coding pipeline. "
    "You receive (1) the project's steering files — generated documentation that "
    "describes the target project's technology stack, conventions, and structure — "
    "and (2) the generic, stack-agnostic base system prompt of one agent in that "
    "pipeline.\n\n"
    "Rewrite the base prompt into a version that is fully specific to the "
    "project's stack as described by the steering files.\n\n"
    "RULES:\n"
    "1. Identify the stack (language, framework, test runner, package manager, "
    "   tooling) solely from the steering files. If they do not identify a stack, "
    "   assume a Flask/Python backend with a TypeScript frontend.\n"
    "2. PRESERVE every structural instruction exactly: the agent's role, every "
    "   numbered rule, and every machine-parsed output contract — JSON schemas, "
    "   '### FILE:' markers, SEARCH/REPLACE delimiters, and sentinel strings such "
    "   as TESTS_VALID must be reproduced verbatim. Downstream parsers depend on them.\n"
    "3. Replace every generic or stack-neutral reference (e.g. 'the project's test "
    "   framework', 'the detected language', 'module initialization files') with "
    "   the concrete name, command, file convention, or idiom used by this stack.\n"
    "4. Where the generic prompt could only speak abstractly, add concrete "
    "   stack-specific guidance relevant to this agent's job: naming idioms, "
    "   import style, framework lifecycle patterns, common pitfalls.\n"
    "5. Do NOT weaken, remove, or reorder any rule. Do NOT add rules that "
    "   contradict the base prompt.\n"
    "6. Output ONLY the rewritten prompt text. No preamble, no commentary, "
    "   no markdown fences around the output.\n"
)


def load_compiled_prompt(unit: str, agent_dir: Path) -> str | None:
    """
    Loads the compiled prompt for a tier compilation unit from
    .agent/prompts/{unit}.txt.

    Returns the prompt text, or None if the file is missing, unreadable, or
    empty. Silent by design — a None return means the caller falls back to
    the hardcoded base prompt and the pipeline proceeds without any warning.
    """
    path = agent_dir / "prompts" / f"{unit}.txt"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def resolve_base_prompt(unit: str, agent_dir: Path) -> str:
    """
    Returns the prompt an agent call site should pass to build_system_prompt()
    as its base_prompt: the compiled stack-specific prompt when one exists on
    disk, otherwise the generic hardcoded base prompt for that unit.
    """
    compiled = load_compiled_prompt(unit, agent_dir)
    return compiled if compiled is not None else BASE_PROMPTS[unit]


def prompts_need_compilation(agent_dir: Path) -> bool:
    """
    Returns True if the compiled prompts directory is missing or incomplete —
    i.e. any compilation unit lacks its .txt file under .agent/prompts/.
    Used by steering_needs_generation() to trigger recompilation.
    """
    prompts_dir = agent_dir / "prompts"
    if not prompts_dir.is_dir():
        return True
    return any(not (prompts_dir / f"{unit}.txt").exists()
               for unit in COMPILE_UNITS)


def compile_agent_prompts(agent_dir: Path, root_dir: Path) -> None:
    """
    Compiles a stack-specific version of every compilation unit's base prompt
    and saves each to .agent/prompts/{unit}.txt. Called once at the end of
    steering generation, and again standalone when steering files are intact
    but compiled prompts are missing.

    Uses the surgeon model (via the 'surgeon' tier) for every call. The call
    deliberately bypasses build_system_prompt() — it is a meta-operation about
    the pipeline's own prompts, not work on the project being built.

    Non-fatal end to end: any unit that fails to compile is simply skipped,
    and its call site silently falls back to the generic base prompt.
    """
    steering_corpus = _load_steering_corpus(agent_dir)
    if not steering_corpus:
        print("[PROMPTS] No steering files found — skipping prompt compilation.")
        return

    config = load_config(root_dir)
    prompts_dir = agent_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    print("[PROMPTS] Compiling stack-specific agent prompts...")

    for unit in COMPILE_UNITS:
        base_prompt = BASE_PROMPTS[unit]
        user_prompt = (
            f"PROJECT STEERING FILES:\n\n{steering_corpus}\n\n"
            f"{'═' * 50}\n"
            f"GENERIC BASE PROMPT TO COMPILE (agent: {unit}):\n"
            f"{'═' * 50}\n\n"
            f"{base_prompt}")

        try:
            response = query_llm("surgeon", _COMPILER_SYSTEM_PROMPT,
                                 user_prompt, config)
        except Exception as e:
            print(f"[PROMPTS] Compilation failed for '{unit}' ({e}) — "
                  "the generic base prompt will be used.")
            continue

        compiled = _strip_outer_fences(response)

        # A faithful rewrite is at least comparable in size to its source;
        # a drastically shorter result means truncation or a refusal.
        if len(compiled) < len(base_prompt) * 0.5:
            print(f"[PROMPTS] Compiled '{unit}' prompt is suspiciously short "
                  f"({len(compiled)} chars) — discarded, the generic base "
                  "prompt will be used.")
            continue

        (prompts_dir / f"{unit}.txt").write_text(compiled + "\n",
                                                 encoding="utf-8")
        print(f"[PROMPTS] ✓ Compiled {unit}.txt")


# ==========================================
# PRIVATE HELPERS
# ==========================================


def _load_steering_corpus(agent_dir: Path) -> str:
    """
    Loads the content of every steering file under .agent/steering/ into a
    single labeled string for the compilation call. Returns empty string if
    the directory is missing or contains no readable content.
    """
    steering_dir = agent_dir / "steering"
    if not steering_dir.is_dir():
        return ""

    parts = []
    for fpath in sorted(steering_dir.glob("*.md")):
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            parts.append(f"--- {fpath.name} ---\n{content}")

    return "\n\n".join(parts)


def _strip_outer_fences(text: str) -> str:
    """
    Removes a single pair of markdown code fences wrapping the whole response,
    if present. Inner content is left untouched — compiled prompts legitimately
    contain fence-related instructions in their rule text.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline != -1 else ""
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    return cleaned.strip()
