import json
import sys
from pathlib import Path
from engine.llm import query_llm, clean_and_parse_json, load_config, query_llm_with_json_retry


def sdd_documents_exist(root_dir: Path) -> bool:
    """
    Returns True only if all three SDD foundation documents exist.
    All three must be present — partial state is treated as missing.
    """
    sdd_dir = root_dir / "sdd-docs"
    return ((sdd_dir / "requirements.md").exists()
            and (sdd_dir / "design.md").exists()
            and (sdd_dir / "tasks.md").exists())


def read_design_doc(root_dir: Path) -> str:
    """
    Returns the content of design.md as a string.
    Used by the Architect to inject architectural context into planning prompts.
    Returns empty string if file doesn't exist yet.
    """
    design_path = root_dir / "sdd-docs" / "design.md"
    if design_path.exists():
        return design_path.read_text(encoding="utf-8")
    return ""


def generate_sdd_documents(project_desc: str, root_dir: Path) -> None:
    """
    Tier 1 — Architect (DeepSeek-R1-32B):
    Generates the three Spec-Driven Development foundation documents from a
    plain-language project description.

    Outputs written to:
        root_dir/requirements.md  — EARS-format requirements + user stories
        root_dir/design.md        — architecture, tech stack, data models, API contracts
        root_dir/tasks.md         — atomic checkbox implementation steps

    These three files drive all downstream Surgeon and Healer work.
    Exits process on JSON parse failure — no SDD docs = no pipeline.
    """
    config = load_config(root_dir)
    print(f"[ARCHITECT] Generating SDD documents...")

    system_prompt = (
        "You are a Lead Software Architect specializing in Spec-Driven Development (SDD). "
        "Your job is to generate three structured Markdown planning documents from the "
        "user's project description. These documents will be consumed by downstream "
        "autonomous code-generation agents — accuracy, completeness, and atomicity are critical.\n\n"
        "Return a SINGLE valid JSON object with exactly three string keys. "
        "Do NOT wrap the JSON in markdown fences. "
        "Do NOT include any text before or after the JSON object.\n\n"
        "═══════════════════════════════════════════════\n"
        "KEY 1 — 'requirements' (requirements.md content)\n"
        "═══════════════════════════════════════════════\n"
        "Use EARS (Easy Approach to Requirements Syntax) notation:\n"
        "  UBIQUITOUS:   'The system shall <capability>.'\n"
        "  EVENT-DRIVEN: 'When <trigger>, the system shall <response>.'\n"
        "  OPTIONAL:     'Where <feature is included>, the system shall <capability>.'\n\n"
        "Structure:\n"
        "  ## Functional Requirements  (numbered R-001, R-002...)\n"
        "  ## Non-Functional Requirements  (performance, security, scalability)\n"
        "  ## Constraints  (runtime, language, framework, OS constraints)\n"
        "  ## User Stories  ('As a <role>, I want <goal>, so that <benefit>.')\n\n"
        "═══════════════════════════════════════════\n"
        "KEY 2 — 'design' (design.md content)\n"
        "═══════════════════════════════════════════\n"
        "Required sections:\n"
        "  ## Architecture Overview\n"
        "    ASCII diagram or prose showing all component boundaries and data flow\n"
        "  ## Technology Stack\n"
        "    Language, framework, runtime for EACH tier with justification\n"
        "  ## Directory Structure\n"
        "    Proposed file tree for the /app directory\n"
        "  ## Data Models\n"
        "    All entities with field names, types, constraints, and relationships\n"
        "  ## API Contracts\n"
        "    All endpoints: HTTP method, path, request body shape, response shape, status codes\n"
        "  ## Key Dependencies\n"
        "    All external libraries with version constraints and purpose\n\n"
        "═══════════════════════════════════════════\n"
        "KEY 3 — 'tasks' (tasks.md content)\n"
        "═══════════════════════════════════════════\n"
        "This is a sequential implementation checklist consumed by an autonomous agent loop.\n\n"
        "STRICT RULES:\n"
        "1. Every task line MUST start with exactly '- [ ] ' (hyphen, space, bracket, space, bracket, space)\n"
        "2. Each task must be a single, atomic, independently-verifiable unit of work\n"
        "3. Tasks must be ordered: scaffolding first → models → routes → tests → integration → polish\n"
        "4. Each task must be precise enough for a code-generation LLM to implement without ambiguity:\n"
        "   GOOD: '- [ ] Create app/backend/models.py: define User model with id, email, "
        "password_hash, created_at fields using SQLAlchemy declarative base'\n"
        "   BAD:  '- [ ] Create the user model'\n"
        "5. No single task should require more than ~60 lines of code\n"
        "6. Include at least one dedicated test task for every implementation task\n"
        "7. Group tasks under markdown headers: ## Phase 1: Scaffolding, ## Phase 2: Models, etc.\n"
    )

    user_prompt = (
        f"Generate the three SDD planning documents for the following project:\n\n"
        f"{project_desc}")

    # Hard constraint: retry up to 2 times with a corrective prompt before halting.
    data = query_llm_with_json_retry(
        tier="architect",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        config=config,
        expected_keys=["requirements", "design", "tasks"],
        context_label="SDD documents",
    )

    requirements_content = data.get("requirements", "# Requirements\n")
    design_content = data.get("design", "# Architecture Design\n")
    tasks_content = data.get("tasks", "# Project Tasks\n")

    # Validate tasks.md has at least some checkbox items
    if "- [ ]" not in tasks_content:
        print(
            "[WARN] Architect generated tasks.md with no '- [ ]' items. Check output."
        )

    sdd_dir = root_dir / "sdd-docs"
    sdd_dir.mkdir(exist_ok=True)
    (sdd_dir / "requirements.md").write_text(requirements_content,
                                             encoding="utf-8")
    (sdd_dir / "design.md").write_text(design_content, encoding="utf-8")
    (sdd_dir / "tasks.md").write_text(tasks_content, encoding="utf-8")

    task_count = tasks_content.count("- [ ]")
    print(
        f"[ARCHITECT] SDD documents written:\n"
        f"  requirements.md, design.md, tasks.md ({task_count} tasks queued)")
