## What This Project Is

You are helping me build **agentic-coder**, a fully local, autonomous, multi-tier LLM coding agent pipeline that runs on my machine. It takes a plain-English project description and automatically builds a complete working software application — writing code, running tests, healing failures, and iterating until all tests pass — with zero manual intervention after the initial prompt.

The pipeline is inspired by Kiro (Amazon's spec-driven IDE agent) and Devin, but runs entirely locally using Ollama-served open-source models. No cloud APIs, no subscriptions, no data leaving the machine.

---

## Core Philosophy: Spec-Driven Development (SDD)

Before writing a single line of code, the pipeline generates three **foundation documents** from the user's project description:

- `requirements.md` — EARS-format functional/non-functional requirements and user stories
- `design.md` — Architecture overview, tech stack, data models, API contracts, directory structure
- `tasks.md` — A sequential atomic checklist of implementation steps, each as a `- [ ]` checkbox

These three files drive everything downstream. The agent loop reads one unchecked task at a time, implements it, tests it, and marks it complete. This is the entire loop.

---

## The Four Agent Tiers

Each tier is a separate LLM call with a distinct role:

### Tier 1 — Architect (`deepseek-r1:32b`)

- Reads the current project directory tree and `design.md`
- Produces a JSON plan: `context_files` (files to read) + `surgeon_prompt` (detailed implementation brief)
- Never writes code — only plans

### Tier 2 — Surgeon (`qwen2.5-coder:32b`)

- Reads all context files identified by Architect
- Writes BOTH implementation code AND test code in one response
- Output format is strictly `### FILE: path` + `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks
- The `engine/splicer.py` module applies these patches to disk

### Tier 2.5 — Validator (`qwen2.5-coder:7b`)

- Pre-flight check BEFORE running tests
- Check 1: Audits source files for stubs, missing imports, syntax issues
- Check 2: Audits test files for fixture mismatches, wrong assertion patterns, SQLAlchemy 2.x anti-patterns
- Auto-applies fixes; non-blocking (never halts the pipeline)

### Tier 3 — Healer (`qwen2.5-coder:7b`, escalates to `32b`)

- Runs pytest, reads failure output
- Diagnoses root cause and applies SEARCH/REPLACE patches
- Retries up to `max_healer_retries` (default 3) times
- Escalates to the Surgeon model after `healer_escalate_after` (default 1) failed passes
- If all retries exhausted → pipeline halts, logs to `healing_telemetry.jsonl`, exits with code 1

---

## The Steering System (Kiro-Inspired)

On first project initialization, the Architect generates three **steering files** stored in `.agent/steering/`:

- `AGENTS.md` — Coding conventions, import patterns, error handling rules, output format rules (NEVER use code fences inside SEARCH/REPLACE)
- `tech.md` — Tech stack constraints, SQLAlchemy 2.x patterns, prohibited patterns
- `structure.md` — Directory layout, module boundary rules, naming conventions, required `__init__.py` files

These are prepended to EVERY agent prompt throughout the entire project lifetime, giving all agents consistent project knowledge without re-explaining it each call.

---

## Complete File Structure

```
agentic-coder/
├── main.py                          ← Entry point (--resume, --tasks-only, --status flags)
├── agentic-coder.yaml               ← Project config (models, API, healer retries, etc.)
├── healing_telemetry.jsonl          ← Auto-generated JSONL log of every task cycle
│
├── engine/
│   ├── __init__.py
│   ├── llm.py                       ← All LLM calls, config loading, JSON parsing
│   ├── splicer.py                   ← Parses ### FILE + SEARCH/REPLACE blocks, applies to disk
│   ├── patch.py                     ← Snapshot/restore (.bak files), cleanup
│   └── syntax.py                    ← Python syntax verification via py_compile
│
├── spec/
│   ├── __init__.py
│   ├── sdd.py                       ← Architect: generates requirements.md, design.md, tasks.md
│   ├── tasks.py                     ← get_next_task(), commit_task_complete(), count_tasks()
│   └── steering.py                  ← Steering file generation + injection into prompts
│
├── testing/
│   ├── __init__.py
│   ├── runner.py                    ← pytest runner (unit + integration tiers, frontend optional)
│   ├── preflight.py                 ← Validator pre-flight checks (source + test audits)
│   └── fixtures.py                  ← Fixture drift detection, __init__.py enforcement
│
├── core/
│   ├── __init__.py
│   ├── agents.py                    ← get_architect_plan(), execute_surgeon(), execute_healer_loop()
│   ├── orchestrator.py              ← boot() + run_task_cycle() — the main pipeline loop
│   ├── telemetry.py                 ← JSONL logging, print_summary() table
│   └── checkpoint.py                ← save/load/clear checkpoint.json for session resume
│
├── tools/
│   ├── __init__.py
│   ├── conda.py                     ← conda env creation, run_in_env() subprocess wrapper
│   ├── deps.py                      ← Dependency scanning, pipreqs, pip install, ledger
│   └── git.py                       ← STUBBED OUT — gated behind git_autocommit: false in config
│
├── app/                             ← The project being built lives here (created by agents)
│   └── (generated by agents)
│
├── .agent/
│   ├── checkpoint.json              ← Resume state (last task, index, files modified)
│   └── steering/
│       ├── AGENTS.md                ← Output format rules + coding conventions
│       ├── tech.md                  ← Tech stack constraints
│       └── structure.md             ← Directory layout + module boundaries
│
├── requirements.md                  ← Generated by Architect (SDD doc 1)
├── design.md                        ← Generated by Architect (SDD doc 2)
└── tasks.md                         ← Generated by Architect (SDD doc 3) — the task queue
```

---

## Key Implementation Details

### `engine/llm.py`

- Loads `agentic-coder.yaml` using PyYAML
- Calls the OpenAI-compatible local API (`api_base`, default `http://localhost:11434/v1`)
- `query_llm(tier, system_prompt, user_prompt, config, override_model=None)` — routes to correct model per tier
- `clean_and_parse_json(text)` — strips markdown fences then `json.loads()`
- `load_config(root_dir)` — reads `agentic-coder.yaml`, returns dict
- `get_healer_escalation_model(config)` — returns `healer_escalation_model` from config

### `engine/splicer.py`

- `splice_multi_file_response(response, root_dir)` — finds all `### FILE:` blocks, applies each SEARCH/REPLACE patch
- `extract_file_chunks(response)` — returns list of `(rel_path, patch_text)` tuples
- SEARCH block must match file content exactly (character-perfect); if not found, tries normalized whitespace fallback
- Creates parent directories automatically
- Takes `.bak` snapshots before every write (used by `engine/patch.py` for restore)
- Empty SEARCH block = new file creation

### `engine/patch.py`

- `take_snapshot(file_path)` — copies file to `file_path.bak`
- `restore_snapshot(file_path)` — restores from `.bak`
- `cleanup_snapshots(root_dir)` — deletes all `.bak` files after successful task cycle

### `engine/syntax.py`

- `verify_syntax(file_path, conda_env)` — runs `python -m py_compile` inside conda env
- Returns `True` if valid, `False` on syntax error
- Called after every Surgeon/Healer patch on `.py` files

### `spec/tasks.py`

- `get_next_task(tasks_path)` — returns first `- [ ]` item text, or `None` if all done
- `commit_task_complete(tasks_path, task_desc, root_dir)` — marks `[ ]` → `[x]`
    - Strategy 1: exact string replacement
    - Strategy 2: normalized whitespace replacement
    - Strategy 3: LLM fallback (Healer model rewrites tasks.md)
    - Strategy 4: truncation guard — rejects LLM output if >8% shorter than original

### `tools/conda.py`

- `ensure_conda_env(conda_env)` — creates env if it doesn't exist
- `run_in_env(cmd, conda_env, cwd, extra_env, timeout)` — runs subprocess inside conda env, returns `(exit_code, output)`

### `tools/deps.py`

- `update_dependencies(app_dir, root_dir, conda_env)` — scans for new imports, installs missing packages
- `get_ledger_content(root_dir)` — returns formatted string of all installed packages (injected into Surgeon prompt so it only uses packages that exist)

### `tools/git.py`

- **FULLY STUBBED** — all functions are no-ops
- Gated by `git_autocommit: false` in `agentic-coder.yaml`
- Do NOT wire it into the pipeline — it is excluded from all orchestrator calls

### `core/orchestrator.py` — `run_task_cycle()` sequence

1. Architect plans → JSON with `context_files` + `surgeon_prompt`
2. Surgeon writes code → SEARCH/REPLACE blocks
3. `splice_multi_file_response()` applies patches to disk
4. `ensure_init_files()` — creates missing `__init__.py` files
5. Validator preflight check 1 — source completeness audit
6. Collect newly written test files (modified in last 3 mins)
7. Validator preflight check 2 — test correctness audit
8. Fixture drift warning check
9. **Healer loop** — run tests → repair → repeat
10. `update_dependencies()` — scan + install new imports
11. `commit_task_complete()` — mark task `[x]` in tasks.md
12. `cleanup_snapshots()` — delete `.bak` files
13. `save_checkpoint()` — write `.agent/checkpoint.json`

### `agentic-coder.yaml` key settings

- `conda_env` — name of conda environment
- `models.architect/surgeon/healer/validator` — Ollama model names
- `healer_escalation_model` — model to use after first healer pass fails
- `api_base` — Ollama server URL (default `http://localhost:11434/v1`)
- `max_healer_retries: 3`
- `healer_escalate_after: 1`
- `git_autocommit: false` — KEEP THIS FALSE
- `auto_install_deps: true`
- `surgeon_min_output_chars: 100` — retry threshold for truncated surgeon output

---

## What Has Been Built (All Rounds Completed)

| Round | Files                                                                               |
| ----- | ----------------------------------------------------------------------------------- |
| 1     | `engine/llm.py`, `engine/splicer.py`, `engine/patch.py`, `engine/syntax.py`         |
| 2     | `tools/conda.py`, `tools/deps.py`, `tools/git.py` (stub)                            |
| 3     | `spec/sdd.py`, `spec/tasks.py`, `spec/steering.py`                                  |
| 4     | `testing/runner.py`, `testing/preflight.py`, `testing/fixtures.py`                  |
| 5     | `core/telemetry.py`, `core/checkpoint.py`, `core/agents.py`, `core/orchestrator.py` |
| 6     | `main.py`, `agentic-coder.yaml`, `.agent/steering/` templates                       |

All `__init__.py` files exist for every package.

---

## Current Status & Next Steps

All code has been written. The pipeline has **NOT been run yet**. The first thing to do in the new chat is:

1. Confirm all files are present and correct in the repo
2. Verify `tools/conda.py` and `tools/deps.py` are fully implemented (Round 2 — confirm these weren't lost)
3. Do a **dry-run trace** through `main.py → boot() → orchestrator` to catch any import errors or missing references before running
4. Run `python main.py` and work through any runtime errors that surface
5. Git is intentionally disabled — do not re-enable it

## Important Constraints to Maintain

- **Git integration stays OFF** (`git_autocommit: false`) — do not wire `tools/git.py` into anything
- All LLM calls go through `engine/llm.py` — never call the OpenAI client directly from other modules
- Steering files are prepended to ALL agent prompts via `spec/steering.build_system_prompt()`
- The Healer must never modify test files unless the test itself has an outright bug — it targets application code first
- `tasks.md` truncation guard: if LLM rewrite is >8% shorter than original, reject and return False
- SQLAlchemy 2.x patterns only: `db.session.get(Model, id)` not `Model.query.get(id)`
