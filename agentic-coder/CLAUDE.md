# AgenticCoder — Project Context for AI Assistants

This file provides authoritative project context for AI coding assistants (Claude, Fable, Copilot, etc.)
working on this codebase. Read it before making any changes.

---

## The Four Agent Tiers

Tier 1 — Architect (deepseek-r1:32b): Reads project tree + design.md, outputs a JSON plan with context_files and surgeon_prompt. Never writes code. Only plans.

Tier 2 — Surgeon (qwen2.5-coder:32b): Reads context files, writes implementation AND tests using ### FILE: path blocks with SEARCH/REPLACE format. This is the only agent that writes code to disk.

Tier 2.5 — Validator (qwen2.5-coder:7b): Pre-flight audits source and test files before tests run. Non-blocking. Auto-applies fixes. Must catch structural and config errors before the test runner ever executes.

Tier 3 — Healer (qwen2.5-coder:7b, escalates to 32b): Reads test runner output, diagnoses root cause, applies SEARCH/REPLACE patches. max_healer_retries defines the number of repair attempts; total test runs = retries + 1 (one initial run before the repair loop, one verification run after each repair). Escalates model after healer_escalate_after failed repairs. If all repair attempts exhausted and the final verification still fails — pipeline halts, logs to healing_telemetry.jsonl.

---

## run_task_cycle() — The Full Loop Sequence

1.  Architect plans → JSON: context_files + surgeon_prompt
2.  Surgeon writes code → SEARCH/REPLACE blocks returned as text
3.  Splice output to disk — patches applied via splice_multi_file_response()
4.  ensure_init_files() — creates missing package init files for the project's stack
5.  Validator preflight check 1 — source completeness audit (auto-applies fixes)
6.  Collect new test files — derived from Surgeon output chunks, filtered by
    test_file_glob from run.json (default: test_*.py); never a filesystem scan
7.  Validator preflight check 2 — test correctness audit (runs only when new test files exist)
8.  Fixture drift warning check
9.  update_dependencies() — scan and install new imports (gated by auto_install_deps config key)
10. Healer loop — initial test run → (diagnose → patch → verify) × max_healer_retries → halt if all fail
11. commit_task_complete() — mark task [x] in tasks.md
12. cleanup_snapshots() — delete .bak files
13. save_checkpoint() — write .agent/checkpoint.json

Dependencies are installed at step 9, before the Healer loop at step 10. This ensures
all packages introduced by the Surgeon are present on the first test run, reducing healer
passes triggered by missing imports rather than logic bugs.

---

## Stack-Agnostic Design — Non-Negotiable

The pipeline makes no assumptions about the target project's language, framework, or test runner.
All stack-specific knowledge is derived from design.md at first boot and stored in
.agent/steering/ and .agent/run.json. Never hardcode Python, Flask, pytest, or any other
stack assumption into the pipeline core (core/, engine/, spec/, testing/, tools/).

---

## The run.json Contract

Steering generation (spec/steering.py) writes .agent/run.json during every project's
first boot. The test runner (testing/runner.py) loads this file on every invocation and
executes its test_command as the primary path. The legacy tiered-detection fallback
(pytest/vitest structure sniffing) only activates when run.json is absent or invalid.

Schema — test_command is required; all other keys are optional:

    test_command:       JSON array — the exact command to run the full test suite
    test_cwd:           string    — working directory relative to project root (default: project root)
    test_file_glob:     string    — filename glob identifying test source files (default: "test_*.py")
    bootstrap_packages: array     — pip packages installed once on fresh conda env creation,
                                    before the first task cycle; leave empty for toolchains
                                    that manage their own dependencies (Node, Go, Rust)

run.json is produced by the Architect model using stack detection from design.md. If
steering generation fails, run.json is not written and the legacy fallback activates.
If run.json is absent on a subsequent boot, steering_needs_generation() detects it and
triggers regeneration automatically before the task loop begins. The legacy fallback is
retained for one milestone and then removed — do not rely on it for new projects.

The test_file_glob field is matched against filenames extracted from Surgeon output
chunks (step 6 of run_task_cycle). It is never applied as a filesystem glob against
the working directory.

---

## Hard Constraints — Never Violate

- Git stays off. git_autocommit: false. tools/git.py is a stub. Never wire it in.
- All LLM calls go through engine/llm.py only. No direct OpenAI client calls from other modules.
- Steering files prepend all four agent-tier prompts. spec/steering.build_system_prompt()
  is called for architect, surgeon, healer, and validator. Never bypass it for these tiers.
- Steering-bypass exemption: two narrow utility LLM calls are exempt from steering injection —
  spec/tasks.py commit_task_complete() LLM fallback (must remain laser-focused on one checkbox;
  steering injection would corrupt the truncation guard) and tools/deps.py update_dependencies()
  (schema-bounded JSON output; project context adds no value and increases hallucination risk).
  All other LLM call sites must route through build_system_prompt().
- Healer targets application code first. It does not touch test files unless the test has an outright logic bug.
- tasks.md truncation guard: if LLM rewrite is more than 8% shorter than original, reject it.
- SQLAlchemy 2.x patterns only: db.session.get(Model, id), never Model.query.get(id).
- conftest.py at root is permanent. It injects the project root onto sys.path. Never delete it.
- Architect JSON retry: if Architect returns malformed JSON, retry up to 2 times with a corrective prompt before halting.
