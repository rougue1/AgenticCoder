import hashlib
import json
import re
from pathlib import Path
from engine.llm import query_llm, query_llm_with_json_retry, load_config
from spec.prompts import compile_agent_prompts, prompts_need_compilation
from spec.sdd import read_design_doc
from spec.stack import parse_stack_profile, stack_profile_missing


def load_steering_context(agent_dir: Path, tier: str) -> str:
    """
    Loads relevant steering files from .agent/steering/ and returns
    them as a combined string to prepend to any agent's system prompt.

    File loading rules per tier:
        architect:  AGENTS.md + structure.md
        surgeon:    AGENTS.md + tech.md + structure.md
        healer:     AGENTS.md + tech.md
        validator:  AGENTS.md + tech.md

    Returns empty string if .agent/steering/ doesn't exist yet.
    This is non-fatal — steering is enhancement, not requirement.
    """
    steering_dir = agent_dir / "steering"
    if not steering_dir.exists():
        return ""

    files_by_tier = {
        "architect": ["AGENTS.md", "structure.md"],
        "surgeon": ["AGENTS.md", "tech.md", "structure.md"],
        "healer": ["AGENTS.md", "tech.md", "structure.md"],
        "validator": ["AGENTS.md", "tech.md"],
    }

    target_files = files_by_tier.get(tier, ["AGENTS.md"])
    parts = []

    for filename in target_files:
        fpath = steering_dir / filename
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[STEERING — {filename}]\n{content}")

    if not parts:
        return ""

    return ("═══════════════════════════════════════════════════\n"
            "PROJECT STEERING CONTEXT (read before responding)\n"
            "═══════════════════════════════════════════════════\n" +
            "\n\n".join(parts) +
            "\n═══════════════════════════════════════════════════\n\n")


def build_system_prompt(
    base_prompt: str,
    tier: str,
    agent_dir: Path,
    app_dir: Path,
) -> str:
    """
    Assembles the final system prompt for any agent tier by combining:
        1. Steering context (project conventions, tech constraints, structure rules)
        2. Fixture registry (available pytest fixtures — Surgeon/Healer only)
        3. Base system prompt (the core task-specific instructions)

    Steering goes FIRST so the model reads project conventions before
    reading the task instructions — mirrors how Kiro injects steering files.
    """
    parts = []

    steering = load_steering_context(agent_dir, tier)
    if steering:
        parts.append(steering)

    if tier in {"surgeon", "healer", "validator"}:
        fixtures = get_fixture_registry(app_dir)
        if fixtures:
            parts.append(
                f"[AVAILABLE PYTEST FIXTURES — use only these in test function signatures]\n"
                f"{', '.join(fixtures)}\n")

    parts.append(base_prompt)
    return "\n".join(parts)


def get_fixture_registry(app_dir: Path) -> list[str]:
    """
    Parses all conftest.py files under app_dir and returns a deduplicated
    list of fixture names. Injected into Surgeon/Healer prompts to prevent
    the model from inventing fixture names that don't exist.

    Example return: ['app', 'db', 'client', 'user_a', 'user_b', 'auth_token']
    """
    fixtures = []
    seen = set()

    for conftest_path in app_dir.rglob("conftest.py"):
        try:
            content = conftest_path.read_text(encoding="utf-8")
            # Match @pytest.fixture (with optional params) followed by def name(
            matches = re.findall(
                r'@pytest\.fixture[^\n]*\n(?:(?:@[^\n]+\n)*)def\s+(\w+)\s*\(',
                content,
            )
            for name in matches:
                if name not in seen:
                    seen.add(name)
                    fixtures.append(name)
        except Exception:
            continue

    return fixtures


def steering_needs_generation(steering_dir: Path, root_dir: Path) -> bool:
    """
    Returns True if steering artifacts need to be (re)generated. Triggers when
    the steering files themselves are stale (see steering_files_stale), OR
    when .agent/stack.md is missing, OR when the compiled prompts under
    .agent/prompts/ are missing or incomplete.

    The orchestrator distinguishes the cases: stale steering files trigger
    full regeneration (which regenerates stack.md and recompiles prompts at
    the end), while intact steering files with only stack.md or compiled
    prompts missing trigger regeneration of just that artifact.
    """
    if steering_files_stale(steering_dir, root_dir):
        return True
    if stack_profile_missing(steering_dir.parent):
        return True
    return prompts_need_compilation(steering_dir.parent)


def steering_files_stale(steering_dir: Path, root_dir: Path) -> bool:
    """
    Returns True if the steering files themselves need regeneration. Triggers
    when ANY of:
        - steering_dir does not exist
        - AGENTS.md, tech.md, or structure.md is missing
        - .design_hash marker is missing
        - .agent/run.json is missing
        - sha256 of sdd-docs/design.md does not match the stored hash

    Returns False without checking the hash if design.md does not exist yet
    (SDD generation has not run) — steering will be re-evaluated once design.md
    is written on a subsequent boot.
    """
    required_files = ["AGENTS.md", "tech.md", "structure.md"]
    hash_path = steering_dir / ".design_hash"
    run_json_path = steering_dir.parent / "run.json"  # .agent/run.json

    if not steering_dir.exists():
        return True
    if any(not (steering_dir / f).exists() for f in required_files):
        return True
    if not hash_path.exists():
        return True
    if not run_json_path.exists():
        return True

    # No design.md yet — can't compute a current hash; don't regenerate
    design_content = read_design_doc(root_dir)
    if not design_content:
        return False

    current_hash = hashlib.sha256(design_content.encode()).hexdigest()
    try:
        stored_hash = hash_path.read_text(encoding="utf-8").strip()
    except OSError:
        return True

    return current_hash != stored_hash


def generate_steering_files(
    agent_dir: Path,
    root_dir: Path,
) -> None:
    """
    On first project initialization, asks the Architect to generate all three
    steering files from the design.md document. These files persist across all
    task cycles and give every agent consistent project knowledge.

    Steering files generated:
        .agent/steering/AGENTS.md    — coding conventions, import patterns, output format rules
        .agent/steering/tech.md      — tech stack constraints, library versions, patterns to avoid
        .agent/steering/structure.md — directory layout, naming conventions, module boundaries

    Also seeds AGENTS.md with universal agent rules that apply to every project
    (like "never wrap SEARCH content in code fences") on top of project-specific rules.
    """
    config = load_config(root_dir)
    steering_dir = agent_dir / "steering"
    steering_dir.mkdir(parents=True, exist_ok=True)

    design_content = read_design_doc(root_dir)

    if not design_content:
        _write_default_steering(steering_dir)
        # Write a sentinel hash so this state is not re-attempted on every boot.
        # Hash of empty bytes differs from any real design.md hash, so regeneration
        # triggers automatically once design.md is populated by SDD generation.
        hash_path = steering_dir / ".design_hash"
        hash_path.write_text(hashlib.sha256(b"").hexdigest(), encoding="utf-8")
        return

    print("[STEERING] Generating project steering files from design.md...")

    system_prompt = (
        "You are a Lead Architect generating persistent steering context files for an "
        "autonomous AI coding agent system. These files will be injected into every "
        "future LLM prompt so agents always have project-specific knowledge.\n\n"
        "FIRST — STACK DETECTION (complete this before writing any key):\n"
        "Identify from the design document: the primary language(s), framework(s), "
        "test framework, package manager, and runtime. Every rule you generate must be "
        "specific to THAT stack — never assume Python, Flask, or pytest.\n\n"
        "Return a SINGLE valid JSON object with exactly four keys. "
        "No markdown fences. No extra text.\n\n"
        "KEY 1 — 'agents' (AGENTS.md content):\n"
        "  Coding conventions specific to this project's detected language and framework. Include:\n"
        "  - Import style appropriate to the language (module resolution order, aliasing rules)\n"
        "  - Naming conventions matching the language's idioms (variables, constants, types, files)\n"
        "  - Error handling patterns: how errors are caught, logged, and propagated in this stack\n"
        "  - Data-access patterns: if the project uses a database or ORM, specify the correct\n"
        "    session/transaction/connection management pattern and any deprecated APIs to avoid\n"
        "  - MANDATORY: Add this exact section verbatim:\n"
        "    ## Output Format Rules (NEVER violate these)\n"
        "    - NEVER wrap SEARCH or REPLACE content in ``` code fences\n"
        "    - NEVER use ```python, ```typescript, or ``` anywhere inside a SEARCH/REPLACE block\n"
        "    - ALWAYS prefix file edits with '### FILE: path/relative/to/root'\n"
        "    - SEARCH content must be character-for-character identical to the file\n"
        "    - Include enough surrounding lines in SEARCH to make the anchor unique\n"
        "    - NEVER truncate, ellipsize, or summarize code in REPLACE blocks\n\n"
        "KEY 2 — 'tech' (tech.md content):\n"
        "  Technology constraints and patterns specific to this project's detected stack. Include:\n"
        "  - Exact library/package versions required and the API patterns they mandate\n"
        "    (for each version-sensitive API, name the correct current pattern AND the deprecated\n"
        "     equivalent to avoid — e.g., for any ORM, name both the current and forbidden query style)\n"
        "  - Framework-specific initialization, routing, and middleware patterns for this stack\n"
        "  - Patterns explicitly prohibited for this stack\n"
        "  - Test framework rules: how fixtures/setup work, test file naming, isolation requirements\n\n"
        "KEY 3 — 'structure' (structure.md content):\n"
        "  Directory and file organization rules for this project's layout. Include:\n"
        "  - Exact expected file tree for the /app directory\n"
        "  - Package/module initialization requirements for the detected language\n"
        "    (e.g., __init__.py for Python packages; index files for JS/TS modules;\n"
        "     mod.rs for Rust modules — use whichever applies)\n"
        "  - Where test files live relative to the code they test\n"
        "  - Module boundary rules: what may import from what\n\n"
        "KEY 4 — 'test_command' (JSON array of strings, required):\n"
        "  The exact command to run the full test suite as a JSON array of strings.\n"
        "  Derive this from the test framework identified in the design document.\n"
        "  Examples by stack:\n"
        "    Python/pytest:  [\"pytest\", \"--tb=short\", \"-v\"]\n"
        "    Node/npm:       [\"npm\", \"test\"]\n"
        "    Node/vitest:    [\"npx\", \"vitest\", \"run\", \"--reporter=verbose\"]\n"
        "    Go:             [\"go\", \"test\", \"./...\"]\n"
        "    Rust/cargo:     [\"cargo\", \"test\"]\n"
        "    Java/Maven:     [\"mvn\", \"test\"]\n\n"
        "OPTIONAL KEY — 'test_cwd' (string):\n"
        "  Working directory relative to the project root where the test command runs.\n"
        "  Omit this key entirely if the project root is the correct working directory.\n"
        "  Example: 'app' or 'app/backend'\n\n"
        "OPTIONAL KEY — 'test_file_glob' (string):\n"
        "  Filename glob pattern used to identify test source files for this stack.\n"
        "  The pipeline uses this to detect which files the Surgeon wrote as tests.\n"
        "  Examples: 'test_*.py' (pytest), '*.test.ts' (vitest/jest),\n"
        "  '*_test.go' (Go), '*_spec.rb' (RSpec).\n"
        "  Omit this key if 'test_*.py' is correct for this project.\n\n"
        "OPTIONAL KEY — 'bootstrap_packages' (JSON array of strings):\n"
        "  Packages the conda environment must have installed before the first task\n"
        "  cycle runs. Include the test framework, test plugins, and any CLI tools\n"
        "  the test_command requires. Do NOT include application dependencies — those\n"
        "  are detected and installed by the dependency scanner after each task.\n"
        "  Examples by stack:\n"
        "    Python/pytest:    [\"pytest\", \"pytest-flask\", \"pytest-cov\"]\n"
        "    Python/FastAPI:   [\"pytest\", \"httpx\", \"pytest-asyncio\"]\n"
        "    Node (vitest/npm): []  — npm manages all JS deps, leave empty\n"
        "    Go / Rust:         []  — test runner is built into the toolchain\n")

    user_prompt = (
        f"Generate steering files for this project:\n\n{design_content}")

    data = query_llm_with_json_retry(
        tier="architect",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        config=config,
        expected_keys=["agents", "tech", "structure", "test_command"],
        context_label="Steering files",
        fatal=False,
    )

    if data is None:
        print(
            "[WARN] Steering generation failed after retries. Using defaults.")
        _write_default_steering(steering_dir)
        return

    agents_raw = data.get("agents", "")
    if isinstance(agents_raw, dict):
        # Model returned a nested object instead of a string — flatten it
        agents_raw = "\n\n".join(
            f"## {k}\n{v}" if isinstance(v, str) else f"## {k}\n{json.dumps(v, indent=2)}"
            for k, v in agents_raw.items()
        )
    agents_content = _prepend_universal_rules(agents_raw)

    (steering_dir / "AGENTS.md").write_text(agents_content, encoding="utf-8")

    tech_raw = data.get("tech", "")
    if isinstance(tech_raw, dict):
        tech_raw = json.dumps(tech_raw, indent=2)

    structure_raw = data.get("structure", "")
    if isinstance(structure_raw, dict):
        structure_raw = json.dumps(structure_raw, indent=2)

    (steering_dir / "tech.md").write_text(tech_raw, encoding="utf-8")
    (steering_dir / "structure.md").write_text(structure_raw, encoding="utf-8")

    # Write machine-readable test runner config to .agent/run.json
    test_command = data.get("test_command")
    if isinstance(test_command, list) and test_command:
        run_config: dict = {"test_command": test_command}
        if data.get("test_cwd"):
            run_config["test_cwd"] = data["test_cwd"]
        if data.get("test_file_glob"):
            run_config["test_file_glob"] = data["test_file_glob"]
        if isinstance(data.get("bootstrap_packages"), list):
            run_config["bootstrap_packages"] = data["bootstrap_packages"]
        (agent_dir / "run.json").write_text(json.dumps(run_config, indent=2),
                                            encoding="utf-8")
        print(
            f"[STEERING] Test runner config written: .agent/run.json → {test_command}"
        )

    # Stamp the design hash so steering is not regenerated unless design.md changes.
    # Written last — only reachable on full success, never on failed or default paths.
    hash_path = steering_dir / ".design_hash"
    hash_path.write_text(hashlib.sha256(design_content.encode()).hexdigest(),
                         encoding="utf-8")

    print("[STEERING] Files written: AGENTS.md, tech.md, structure.md")

    # Generate the stack profile from design.md + the freshly written steering
    # files. Non-fatal — a missing stack.md only disables the stack-aware
    # toolchain paths, and steering_needs_generation() retriggers it next boot.
    generate_stack_profile(agent_dir, root_dir)

    # Compile stack-specific tier prompts from the freshly written steering
    # files. Only runs on the fully successful path — on default/failed paths
    # the tiers silently fall back to their generic base prompts, which is the
    # correct behavior when the stack is unknown.
    compile_agent_prompts(agent_dir, root_dir)


# System prompt for stack profile generation. This is one of the two places
# in the pipeline where a default stack may be named (the other is prompt
# compilation) — it is an instruction to the LLM, never a branch in code.
# The toolchain routing code reads whatever stack.md says and executes it.
_STACK_PROFILE_SYSTEM_PROMPT = (
    "You are a Lead Architect generating a stack profile document for an "
    "autonomous AI coding pipeline. The profile is the pipeline's single "
    "source of truth for how to interact with the target project's toolchain: "
    "which package managers to use, how to run tests, and what one-time setup "
    "a fresh environment needs. The pipeline itself knows nothing about any "
    "language or toolchain — it executes exactly what this document declares.\n\n"
    "Derive everything from the design document and steering files provided. "
    "If they do not identify a stack, assume a Flask/Python backend with a "
    "TypeScript frontend (pip as the primary package manager, npm for the "
    "frontend).\n\n"
    "Output ONLY a Markdown document with EXACTLY this structure. The section "
    "headings and key names are machine-parsed — reproduce them verbatim. No "
    "commentary before or after the document. No code fences around it.\n\n"
    "# Stack Profile\n\n"
    "## Runtime\n"
    "- runtime: <language runtime name and version, e.g. a language + version>\n"
    "- runtime_check_command: `<command that exits 0 if the runtime is installed>`\n\n"
    "## Commands\n"
    "- build_command: `<command to build the project — omit this line if there "
    "is no build step>`\n"
    "- test_suite_command: `<command to run the full test suite>`\n"
    "- targeted_test_command: `<command to run one named test — MUST contain "
    "the literal placeholders {file} and {test}>`\n"
    "- file_test_command: `<command to run one test file — MUST contain {file}>`\n"
    "- test_function_pattern: `<regex with ONE capture group matching a test "
    "declaration's name in this stack's test source code>`\n\n"
    "## Package Managers\n\n"
    "### <manager name> (primary)\n"
    "- install_command: `<install command template — MUST contain {package}>`\n"
    "- uninstall_command: `<uninstall command template — MUST contain {package}>`\n"
    "- requires_sudo: <true|false — does installing need elevated privileges?>\n"
    "- interactive: <true|false — do its commands prompt for user input?>\n"
    "- working_directory: <directory relative to project root the commands "
    "must run from — omit this line if the project root is correct>\n"
    "- source_file_extensions: `<.ext>`, `<.ext>`  <file extensions in which "
    "this manager's dependencies are declared>\n"
    "- dependency_scan_patterns:\n"
    "  - `<regex with ONE capture group capturing an external dependency name "
    "as it is declared in source files of those extensions>`\n\n"
    "Add one '### <name>' block per package manager the project needs — e.g. "
    "a backend manager and a frontend manager are both valid simultaneously. "
    "Mark exactly ONE block with '(primary)'.\n\n"
    "## Bootstrap Commands\n\n"
    "- command: `<shell command to run once on a fresh environment>`\n"
    "  check: `<command that exits 0 if this step is already satisfied — omit "
    "this line if there is no cheap check>`\n"
    "  requires_sudo: <true|false>\n"
    "  interactive: <true|false>\n"
    "  reason: <one line explaining why this step is needed>\n\n"
    "Bootstrap rules: an ordered list of one-time setup steps a fresh "
    "environment needs before the first task cycle — the test framework and "
    "its plugins, global CLI tools, runtime/build-target registration, "
    "anything the test_suite_command requires to even start. These are "
    "arbitrary shell commands, not package names. Prefer non-interactive "
    "flags (assume-yes options) wherever the toolchain supports them. Do NOT "
    "include application dependencies — those are detected and installed per "
    "task by the dependency scanner. If no bootstrap steps are needed, output "
    "the section heading with no entries.\n\n"
    "Accuracy rules:\n"
    "1. Every command must be real, correct syntax for the chosen toolchain.\n"
    "2. Be honest with requires_sudo and interactive — the pipeline pauses "
    "for user confirmation on any command marked true. A false negative "
    "hangs or fails an automated run; a false positive causes needless "
    "prompts.\n"
    "3. The placeholders {package}, {file}, and {test} must appear literally "
    "in their templates — the pipeline substitutes them at execution time.\n"
    "4. test_suite_command must mirror the test command declared during "
    "steering (run.json), if one was declared.\n")


def generate_stack_profile(agent_dir: Path, root_dir: Path) -> None:
    """
    Generates .agent/stack.md — the stack profile document that drives the
    toolchain layer (package manager routing, adaptive test commands, and
    environment bootstrap). Called at the end of generate_steering_files(),
    and standalone by the orchestrator when stack.md is the only missing
    steering artifact.

    One LLM call on the same tier as the other steering generation calls,
    with design.md and the three SDD steering files as context. The response
    is validated by parsing it with spec/stack.parse_stack_profile() before
    being written; an unparseable response gets one corrective retry.

    Non-fatal: on failure the file is simply not written and every consumer
    falls back to its legacy behavior.
    """
    config = load_config(root_dir)

    design_content = read_design_doc(root_dir)
    if not design_content:
        print("[STACK] design.md not available — "
              "skipping stack profile generation.")
        return

    steering_dir = agent_dir / "steering"
    steering_parts = []
    for filename in ("AGENTS.md", "tech.md", "structure.md"):
        fpath = steering_dir / filename
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            steering_parts.append(f"--- {filename} ---\n{content}")

    print("[STACK] Generating stack profile (.agent/stack.md) "
          "from design.md...")

    base_user_prompt = (
        f"DESIGN DOCUMENT:\n{design_content}\n\n"
        f"STEERING FILES:\n" +
        ("\n\n".join(steering_parts) if steering_parts else "(none)") +
        "\n\nGenerate the stack profile document now.")

    response = query_llm("architect", _STACK_PROFILE_SYSTEM_PROMPT,
                         base_user_prompt, config)
    content = _strip_markdown_fences(response)
    profile = parse_stack_profile(content)

    if not _stack_profile_usable(profile):
        print("[STACK] Generated profile failed validation — "
              "retrying once with a corrective prompt...")
        corrective_user = (
            "Your previous stack profile could not be parsed. It must follow "
            "the exact section headings ('## Runtime', '## Commands', "
            "'## Package Managers', '## Bootstrap Commands') and "
            "'key: value' bullet format from the instructions, with no code "
            "fences and no commentary.\n\n"
            f"Your previous output (first 500 chars):\n{response[:500]}\n\n" +
            base_user_prompt)
        response = query_llm("architect", _STACK_PROFILE_SYSTEM_PROMPT,
                             corrective_user, config)
        content = _strip_markdown_fences(response)
        profile = parse_stack_profile(content)

    if not _stack_profile_usable(profile):
        print("[WARN] Stack profile generation failed after retry. "
              "The toolchain layer will use legacy fallbacks until "
              "stack.md is generated on a future boot.")
        return

    (agent_dir / "stack.md").write_text(content.rstrip() + "\n",
                                        encoding="utf-8")

    managers = [m["name"] for m in profile.get("package_managers", [])]
    bootstrap_count = len(profile.get("bootstrap_commands", []))
    print(f"[STACK] Stack profile written: .agent/stack.md "
          f"(package managers: {', '.join(managers) or 'none'}; "
          f"bootstrap steps: {bootstrap_count})")


def _stack_profile_usable(profile: dict | None) -> bool:
    """
    Minimum bar for accepting a generated profile: it parsed, and it declares
    at least a full-suite test command or one package manager. Anything less
    gives the toolchain layer nothing to act on.
    """
    if not profile:
        return False
    return bool(
        profile.get("test_suite_command") or profile.get("package_managers"))


def _strip_markdown_fences(text: str) -> str:
    """
    Removes a single pair of markdown code fences wrapping the whole
    response, if present — models sometimes fence the entire document
    despite instructions. Inner content is left untouched.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline != -1 else ""
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    return cleaned.strip()


def _prepend_universal_rules(agent_content: str) -> str:
    """
    Ensures the universal output format rules are always present in AGENTS.md
    regardless of what the Architect generated. Prepended so they appear first.
    """
    if not isinstance(agent_content, str):
        agent_content = json.dumps(agent_content, indent=2) if isinstance(
            agent_content, dict) else str(agent_content)
    universal = """\
## Output Format Rules (NEVER violate these)

- NEVER wrap SEARCH or REPLACE content in ``` code fences of any kind
- NEVER use ```python, ```typescript, ```bash, or bare ``` inside SEARCH/REPLACE blocks
- ALWAYS prefix every file edit with exactly: ### FILE: path/relative/to/project/root
- SEARCH content must be character-for-character identical to what is in the file
- Include at least 3-5 surrounding lines in SEARCH blocks to ensure anchor uniqueness
- NEVER truncate, ellipsize (...), or summarize code inside REPLACE blocks
- For NEW files: use an empty SEARCH block (nothing between <<<<<<< SEARCH and =======)
- NEVER rewrite an entire existing file — always use targeted SEARCH/REPLACE patches

"""
    # Avoid duplicating if Architect already included it
    if "NEVER wrap SEARCH" in agent_content:
        return agent_content
    return universal + agent_content


def _write_default_steering(steering_dir: Path) -> None:
    """
    Writes minimal stack-neutral default steering files when design.md is
    unavailable or LLM steering generation fails after retries.

    AGENTS.md: universal output-format rules + language-agnostic conventions.
    tech.md:   empty placeholder — no stack assumptions.
    structure.md: empty placeholder — no layout assumptions.

    Both tech.md and structure.md explicitly signal that real rules are missing
    so agents running on defaults produce a visible diagnostic in their context
    rather than silently applying wrong-stack conventions.
    """

    agents_md = _prepend_universal_rules(
        "## General Conventions\n\n"
        "- Follow the naming idioms of the project's primary language\n"
        "- Use explicit imports — never wildcard or star imports\n"
        "- Catch specific error types — never use bare or empty catch blocks\n"
        "- If the project uses a database, perform all operations within the\n"
        "  framework's prescribed session or connection scope\n"
        "- If the project uses a database, commit or finalize transactions\n"
        "  explicitly — never rely on autocommit\n")

    (steering_dir / "AGENTS.md").write_text(agents_md, encoding="utf-8")

    (steering_dir / "tech.md").write_text(
        "## Tech Stack\n\n"
        "Stack-specific rules have not been generated yet.\n"
        "This file is populated automatically from design.md on first run.\n"
        "If you see this message in an agent prompt, steering generation failed —\n"
        "check that sdd-docs/design.md exists and that Ollama is reachable.\n",
        encoding="utf-8",
    )

    (steering_dir / "structure.md").write_text(
        "## Directory Structure\n\n"
        "Directory layout rules have not been generated yet.\n"
        "This file is populated automatically from design.md on first run.\n"
        "If you see this message in an agent prompt, steering generation failed —\n"
        "check that sdd-docs/design.md exists and that Ollama is reachable.\n",
        encoding="utf-8",
    )

    print(
        "[STEERING] Default (stack-neutral) steering files written.\n"
        "           Stack-specific rules will be generated once design.md is available."
    )
