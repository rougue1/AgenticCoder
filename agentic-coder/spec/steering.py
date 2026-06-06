import re
from pathlib import Path
from engine.llm import query_llm, load_config


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
        "healer": ["AGENTS.md", "tech.md"],
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

    design_content = ""
    design_path = root_dir / "design.md"
    if design_path.exists():
        design_content = design_path.read_text(encoding="utf-8")

    if not design_content:
        _write_default_steering(steering_dir)
        return

    print("[STEERING] Generating project steering files from design.md...")

    system_prompt = (
        "You are a Lead Architect generating persistent steering context files for an "
        "autonomous AI coding agent system. These files will be injected into every "
        "future LLM prompt so agents always have project-specific knowledge.\n\n"
        "Generate three steering documents based on the design.md provided.\n\n"
        "Return a SINGLE valid JSON object with exactly three string keys. "
        "No markdown fences. No extra text.\n\n"
        "KEY 1 — 'agents' (AGENTS.md content):\n"
        "  Universal coding conventions for this project. Include:\n"
        "  - Import style (absolute vs relative, ordering)\n"
        "  - Naming conventions (snake_case models, UPPER constants, etc.)\n"
        "  - Error handling patterns (how exceptions should be caught and logged)\n"
        "  - ORM patterns (session management, commit timing, relationship loading)\n"
        "  - MANDATORY: Add this exact section verbatim:\n"
        "    ## Output Format Rules (NEVER violate these)\n"
        "    - NEVER wrap SEARCH or REPLACE content in ``` code fences\n"
        "    - NEVER use ```python, ```typescript, or ``` anywhere inside a SEARCH/REPLACE block\n"
        "    - ALWAYS prefix file edits with '### FILE: path/relative/to/root'\n"
        "    - SEARCH content must be character-for-character identical to the file\n"
        "    - Include enough surrounding lines in SEARCH to make the anchor unique\n"
        "    - NEVER truncate, ellipsize, or summarize code in REPLACE blocks\n\n"
        "KEY 2 — 'tech' (tech.md content):\n"
        "  Technology constraints and patterns. Include:\n"
        "  - Exact library versions and why (e.g., SQLAlchemy 2.x — use session.get() not query.get())\n"
        "  - Framework-specific patterns (Flask app factory, Blueprint registration)\n"
        "  - Patterns explicitly prohibited (e.g., no circular imports, no synchronous calls in async context)\n"
        "  - Test framework rules (pytest fixture scopes, conftest locations)\n\n"
        "KEY 3 — 'structure' (structure.md content):\n"
        "  Directory and file organization rules. Include:\n"
        "  - Exact expected file tree for /app\n"
        "  - Which __init__.py files must exist and what they export\n"
        "  - Where test files live relative to the code they test\n"
        "  - Module boundary rules (what can import what)\n")

    user_prompt = (
        f"Generate steering files for this project:\n\n{design_content}")

    try:
        from engine.llm import clean_and_parse_json
        response = query_llm("architect", system_prompt, user_prompt, config)
        data = clean_and_parse_json(response)

        agents_content = _prepend_universal_rules(data.get("agents", ""))
        (steering_dir / "AGENTS.md").write_text(agents_content,
                                                encoding="utf-8")
        (steering_dir / "tech.md").write_text(data.get("tech", ""),
                                              encoding="utf-8")
        (steering_dir / "structure.md").write_text(data.get("structure", ""),
                                                   encoding="utf-8")

        print("[STEERING] Files written: AGENTS.md, tech.md, structure.md")

    except Exception as e:
        print(f"[WARN] Steering file generation failed: {e}. Using defaults.")
        _write_default_steering(steering_dir)


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
    Writes minimal default steering files when design.md is unavailable
    or Architect steering generation fails. Contains universal rules only.
    """
    agents_md = _prepend_universal_rules(
        "## General Conventions\n\n"
        "- Use snake_case for all Python identifiers\n"
        "- Use absolute imports within the project\n"
        "- All database operations must occur within an application context\n"
        "- Commit database sessions explicitly — never rely on autocommit\n")
    (steering_dir / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    (steering_dir / "tech.md").write_text(
        "## Tech Stack\n\n- Python 3.11\n- Flask 3.x\n- SQLAlchemy 2.x\n- pytest\n",
        encoding="utf-8",
    )
    (steering_dir / "structure.md").write_text(
        "## Directory Structure\n\n- app/backend/ — Flask application\n"
        "- app/backend/tests/ — pytest test suite\n",
        encoding="utf-8",
    )
    print("[STEERING] Default steering files written.")
