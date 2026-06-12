# AgenticCoder — Project Context for AI Assistants

This file provides authoritative project context for AI coding assistants (Claude, Fable, Copilot, etc.) working on this codebase. Read it before making any changes.

---

## Project Overview

AgenticCoder is a fully local, autonomous coding pipeline. It takes a design.md file describing an application and a tasks.md file listing discrete implementation tasks, and executes those tasks one by one using a tiered system of local LLM agents. No cloud models. No user intervention during a run. The pipeline plans, writes, validates, tests, and repairs code entirely on the local machine.

The entry point is main.py. The pipeline is configured via agentic-coder.yaml. All runtime state is stored under .agent/ in the target project directory.

---

## Repository Structure

agentic-coder/
├── main.py                  — Entry point. Boots the pipeline, loads config, starts the task loop.
├── agentic-coder.yaml       — User-facing config: model names, retry limits, feature flags, paths.
├── conftest.py              — Root pytest conftest. Injects project root onto sys.path. Permanent — never delete.
├── core/
│   ├── orchestrator.py      — run_task_cycle(): the main per-task loop. Coordinates all tiers.
│   ├── agents.py            — LLM call wrappers for each tier: architect(), surgeon(), healer(), validator().
│   ├── checkpoint.py        — save_checkpoint() and load_checkpoint(). Reads/writes .agent/checkpoint.json.
│   └── telemetry.py         — Logs healer attempts to healing_telemetry.jsonl.
├── spec/
│   ├── steering.py          — generate_steering_files(): produces SDD files, run.json, stack.md (planned).
│   │                          build_system_prompt(): prepends steering context to every tier prompt.
│   │                          steering_needs_generation(): detects missing/stale steering artifacts.
│   ├── prompts.py           — compile_agent_prompts(): compiles stack-specific tier prompts.
│   │                          load_compiled_prompt(): loads compiled prompt for a tier from .agent/prompts/.
│   ├── stack.py             — load_stack_profile(): reads .agent/stack.md into a flat key/value dict
│   │                          (command templates for the adaptive runner). Generation of stack.md
│   │                          itself is still planned in spec/steering.py.
│   ├── tasks.py             — Task list parser. commit_task_complete(): marks tasks [x] in tasks.md.
│   └── sdd.py               — SDD file schema and helpers (if present).
├── engine/
│   ├── llm.py               — ALL LLM calls go through here. query_llm() is the only entry point.
│   ├── splicer.py           — splice_multi_file_response(): parses ### FILE: / SEARCH/REPLACE blocks,
│   │                          applies patches to disk. Returns list of patched file paths.
│   ├── patch.py             — Low-level patch application helpers.
│   └── syntax.py            — Syntax validation helpers used by the Validator.
├── testing/
│   ├── runner.py            — Runs the test suite. Loads run.json for test_command. Legacy fallback
│   │                          (structure sniffing) only activates when run.json is absent/invalid.
│   │                          Three-phase adaptive runner (targeted → file → suite): command
│   │                          templates come from stack.md; full-suite-only when stack.md absent.
│   └── preflight.py         — Validator preflight checks: source audit (pass 1) and test audit (pass 2).
└── tools/
    ├── deps.py              — update_dependencies(): scans new source files for imports, installs via pip.
    │                          Planned: package manager routing via stack.md.
    ├── conda.py             — Environment bootstrap. Creates conda env, installs bootstrap_packages.
    │                          Planned: stack.md-driven bootstrap command execution.
    └── git.py               — STUB ONLY. Never wire this in. git_autocommit is always false.

Runtime artifacts (written to the target project's .agent/ directory):
    .agent/steering/         — SDD files generated from design.md
    .agent/run.json          — Stack-specific test command and config (machine-readable)
    .agent/stack.md          — Stack profile: toolchain commands, package manager, bootstrap (planned)
    .agent/prompts/          — Compiled stack-specific tier prompts: architect.txt, surgeon.txt, etc. (planned)
    .agent/checkpoint.json   — Last completed task + last_exit_code
    healing_telemetry.jsonl  — Per-attempt healer log

---

## The Four Agent Tiers

Tier 1 — Architect (deepseek-r1:32b): Reads project tree + design.md + all conftest.py files found
in app_dir (always injected unconditionally, capped at 4000 chars), outputs a JSON plan with
context_files and surgeon_prompt. Never writes code. Only plans. If JSON output is malformed,
retries up to 2 times with a corrective prompt before halting.

Tier 2 — Surgeon (qwen2.5-coder:32b): Reads context files, writes implementation AND tests using
### FILE: path blocks with SEARCH/REPLACE format. This is the only agent that writes code to disk.
If splice_multi_file_response() returns an empty patch list, the pipeline retries the Surgeon once
before entering the Healer loop (gated by surgeon_patch_retry config key, default: true).

Tier 2.5 — Validator (qwen2.5-coder:7b): Pre-flight audits source and test files before tests run.
Non-blocking. Auto-applies fixes. Must catch structural and config errors before the test runner
ever executes. Both validator passes (steps 5 and 7) are skipped entirely when patched == [] —
they do not run on empty patch sets.

Tier 3 — Healer (qwen2.5-coder:7b, escalates to 32b): Reads test runner output, diagnoses root
cause, applies SEARCH/REPLACE patches. max_healer_retries defines the number of repair attempts;
total test runs = retries + 1 (one initial run before the repair loop, one verification run after
each repair). Escalates model after healer_escalate_after failed repairs. If all repair attempts
exhausted and the final verification still fails — pipeline halts, logs to healing_telemetry.jsonl.
Before applying each patch, the Healer snapshots all files it will modify. If the post-patch
failure count is higher than the pre-patch count, all modified files are restored from snapshots
(rollback). The next repair iteration begins from the rolled-back state.

---

## run_task_cycle() — The Full Loop Sequence

1.  Architect plans → JSON: context_files + surgeon_prompt
2.  Surgeon writes code → SEARCH/REPLACE blocks returned as text
3.  Splice output to disk — patches applied via splice_multi_file_response()
    If patched == [] and surgeon_patch_retry is true, Surgeon is retried once before continuing.
4.  ensure_init_files() — creates missing package init files for the project's stack
5.  Validator preflight check 1 — source completeness audit (auto-applies fixes)
    SKIPPED if patched == []. Prints "[VALIDATOR] Skipped — no files patched."
6.  Collect new test files — derived from Surgeon output chunks, filtered by
    test_file_glob from run.json (default: test_*.py); never a filesystem scan
7.  Validator preflight check 2 — test correctness audit (runs only when new test files exist)
    SKIPPED if patched == []. Prints "[VALIDATOR] Skipped — no files patched."
8.  Fixture drift warning check
9.  update_dependencies() — scan and install new imports (gated by auto_install_deps config key)
10. Healer loop — initial test run → (snapshot → diagnose → patch → verify → rollback if regression)
    × max_healer_retries → halt if all fail
11. commit_task_complete() — mark task [x] in tasks.md
12. cleanup_snapshots() — delete .bak files
13. save_checkpoint() — write .agent/checkpoint.json including last_exit_code (final healer exit code)

Dependencies are installed at step 9, before the Healer loop at step 10. This ensures all packages
introduced by the Surgeon are present on the first test run, reducing healer passes triggered by
missing imports rather than logic bugs.

On resume after a crash: if checkpoint.last_exit_code == 0, the pipeline prints
"[BOOT] Last session ended green — resuming from next task." and skips re-running the healer
for the checkpointed task.

---

## Adaptive Three-Phase Test Runner (implemented in testing/runner.py + core/agents.py)

The test runner uses an adaptive three-phase model to keep healer context focused and cycle
times fast. The phase plan is built per task by core/agents.py _build_phase_plan(); phase
commands execute in testing/runner.py. Phase 3 frequency is the suite_run_every_n_tasks
config key (1 = every task, N = every N tasks, 0 = only via AGENT_FORCE_SUITE=1).

Phase 1 — Targeted run: Run only the specific test function(s) the Surgeon just wrote.
  - Parse test function names from Surgeon output chunks before or after disk write.
  - New tests added to existing file → run only those functions by name.
  - Brand new test file → run the whole file (all tests are new).
  - Command derived from stack.md targeted_test_command template. Falls back to full file run
    when stack.md is absent or targeted execution is unsupported.

Phase 2 — File run: Run only if Phase 1 passes.
  - Run the entire test file containing the new tests.
  - Catches fixture collisions, shared state conflicts, import side effects.
  - On failure: Healer receives the new test function(s) + the conflicting existing function(s) only.
    Not the whole file.

Phase 3 — Suite run: Run only if Phase 2 passes.
  - Run the full test suite. Catches cross-file regressions.
  - Configurable frequency: every task (default), every N tasks, or on explicit request.

Healer context under this model:
  - Phase 1 failure → failing test function code + targeted run output only.
  - Phase 2 failure → new test function(s) + conflicting existing function(s) + file run output.
  - Phase 3 failure → newly failing functions (diff of Phase 2 vs Phase 3 results) + suite output.
  - In all cases: subject to the 6000-character source context budget.
  - Phase label must appear in Healer prompt so the model knows the scope of the regression.

All test command templates (targeted, file, suite) are read from stack.md. No test runner
syntax is hardcoded anywhere in testing/runner.py.

---

## Compiled Prompts (implemented in spec/prompts.py)

At the end of steering generation, the pipeline calls compile_agent_prompts() once. This function
calls the LLM once per tier (architect, surgeon, healer, validator) using the surgeon model,
passing all steering file contents as context, and asks it to rewrite each tier's generic base
prompt into a fully stack-specific version. Compiled prompts are saved to .agent/prompts/{tier}.txt.

On every subsequent LLM call, each agent tier loads its compiled prompt via
load_compiled_prompt(tier, agent_dir). If the file is missing or unreadable, the agent falls back
to its hardcoded base prompt silently — the pipeline never breaks due to a failed compilation.

steering_needs_generation() returns True if .agent/prompts/ is missing or contains fewer than
4 .txt files, triggering recompilation alongside steering regeneration.

Compiled prompts replace the base_prompt argument to build_system_prompt(). They do not replace
the build_system_prompt() call itself — steering context still wraps every tier prompt.

The two steering-bypass exemptions (commit_task_complete, update_dependencies) do not receive
compiled prompts.

Default stack when design.md specifies no stack: Flask/Python backend + TypeScript frontend.
This default is expressed only in the LLM prompt used for compilation — never hardcoded in logic.

---

## Stack-Agnostic Design — Non-Negotiable

The pipeline makes no assumptions about the target project's language, framework, or test runner.
All stack-specific knowledge is derived from design.md at first boot and stored in
.agent/steering/, .agent/run.json, and .agent/stack.md. Never hardcode any language, framework,
package manager, test runner, or toolchain name into the pipeline core
(core/, engine/, spec/, testing/, tools/). The code is a template engine. The LLM fills the templates.

---

## The run.json Contract

Steering generation (spec/steering.py) writes .agent/run.json during every project's first boot.
The test runner (testing/runner.py) loads this file on every invocation and executes its
test_command as the primary path. The legacy tiered-detection fallback (structure sniffing) only
activates when run.json is absent or invalid.

Schema — test_command is required; all other keys are optional:

    test_command:           JSON array — the exact command to run the full test suite
    test_cwd:               string    — working directory relative to project root (default: project root)
    test_file_glob:         string    — filename glob identifying test source files (default: "test_*.py")
    bootstrap_packages:     array     — legacy pip packages for fresh conda env; superseded by stack.md
                                        bootstrap commands when stack.md is present

The test_file_glob field is matched against filenames extracted from Surgeon output chunks
(step 6 of run_task_cycle). It is never applied as a filesystem glob against the working directory.

---

## The stack.md Contract (reader implemented in spec/stack.py; generation still planned in spec/steering.py)

stack.md is generated once during steering and lives at .agent/stack.md. It is the single source
of truth for how to interact with the project's toolchain. It is a Markdown document — human and
LLM readable — not a JSON/YAML config file.

The schema must express at minimum:
    primary_package_manager:        name + install/uninstall command templates using {package}
    requires_sudo:                  whether install commands need elevated privileges
    interactive:                    whether any commands require user input
    build_command:                  command to build the project
    test_suite_command:             full suite run command (mirrors run.json test_command)
    targeted_test_command:          template using {file} and {test} for single-test execution
    file_test_command:              template using {file} for file-level test execution
    runtime:                        language runtime name + version check command
    bootstrap_commands:             ordered list of one-time setup commands for fresh environments
    dependency_scan_patterns:       patterns identifying external dependency declarations per file type

stack.md is generated by calling the LLM with design.md + SDD files as context. Default when no
stack is specified: Flask/Python + TypeScript. This default lives only in the generation prompt.
steering_needs_generation() treats missing stack.md as a trigger for regeneration.

Permission gating: any command stack.md marks as requires_sudo or interactive must pause and
prompt the user for confirmation before executing. Configurable to auto-approve for headless envs.

---

## Hard Constraints — Never Violate

- Git stays off. git_autocommit: false. tools/git.py is a stub. Never wire it in.
- All LLM calls go through engine/llm.py only. No direct OpenAI client calls from other modules.
- Steering files prepend all four agent-tier prompts. spec/steering.build_system_prompt() is called
  for architect, surgeon, healer, and validator. Never bypass it for these tiers.
- Steering-bypass exemption: two narrow utility LLM calls are exempt from steering injection —
  spec/tasks.py commit_task_complete() LLM fallback and tools/deps.py update_dependencies().
  All other LLM call sites must route through build_system_prompt().
- Compiled prompts replace base_prompt only. build_system_prompt() is always called on top.
  Never pass a compiled prompt directly to query_llm() without wrapping it first.
- Healer targets application code first. It does not touch test files unless the test has an
  outright logic bug.
- Healer rollback is unconditional. If a patch increases the failure count, restore all modified
  files from pre-patch snapshots before the next repair attempt. Never skip rollback to "save time."
- Healer source context budget: 6000 characters total across all loaded traceback files. Files are
  loaded in full (no truncation), sorted by traceback mention frequency, most-mentioned first.
  Stop adding files once the budget is exhausted.
- No language, package manager, test runner, or toolchain name is hardcoded in core/, engine/,
  spec/, testing/, or tools/ except in fallback paths. All such knowledge lives in stack.md.
- tasks.md truncation guard: if LLM rewrite is more than 8% shorter than original, reject it.
- SQLAlchemy 2.x patterns only: db.session.get(Model, id), never Model.query.get(id).
- conftest.py at root is permanent. It injects the project root onto sys.path. Never delete it.
- Architect JSON retry: if Architect returns malformed JSON, retry up to 2 times before halting.
- Architect always receives conftest.py context. All conftest.py files found under app_dir are
  unconditionally appended to the Architect's user_prompt. Not optional, not in context_files.
- checkpoint.last_exit_code must be written on every save_checkpoint() call. Default is 0.