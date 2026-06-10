import hashlib
import json
import re
from pathlib import Path
from engine.llm import query_llm, query_llm_with_json_retry, load_config
from spec.sdd import read_design_doc


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
    Returns True if steering files need to be (re)generated. Triggers when ANY of:
        - steering_dir does not exist
        - AGENTS.md, tech.md, or structure.md is missing
        - .design_hash marker is missing
        - sha256 of sdd-docs/design.md does not match the stored hash

    Returns False without checking the hash if design.md does not exist yet
    (SDD generation has not run) — steering will be re-evaluated once design.md
    is written on a subsequent boot.
    """
    required_files = ["AGENTS.md", "tech.md", "structure.md"]
    hash_path = steering_dir / ".design_hash"

    if not steering_dir.exists():
        return True
    if any(not (steering_dir / f).exists() for f in required_files):
        return True
    if not hash_path.exists():
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
        "  Example: 'app' or 'app/backend'\n")

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

    agents_content = _prepend_universal_rules(data.get("agents", ""))
    (steering_dir / "AGENTS.md").write_text(agents_content, encoding="utf-8")
    (steering_dir / "tech.md").write_text(data.get("tech", ""),
                                          encoding="utf-8")
    (steering_dir / "structure.md").write_text(data.get("structure", ""),
                                               encoding="utf-8")

    # Write machine-readable test runner config to .agent/run.json
    test_command = data.get("test_command")
    if isinstance(test_command, list) and test_command:
        run_config: dict = {"test_command": test_command}
        test_cwd = data.get("test_cwd")
        if test_cwd:
            run_config["test_cwd"] = test_cwd
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


def _prepend_universal_rules(agent_content: str) -> str:
    """
    Ensures the universal output format rules are always present in AGENTS.md
    regardless of what the Architect generated. Prepended so they appear first.
    """
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
