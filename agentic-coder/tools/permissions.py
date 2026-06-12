"""
Permission gating for toolchain commands.

Any command that stack.md marks as requires_sudo or interactive must pass
through confirm_command() before it executes. The gate prints what the
pipeline wants to run and why, then waits for explicit user confirmation.
A declined gate is logged and the command is skipped — the pipeline never
halts over a declined permission prompt.

This module is stack-agnostic by construction: it never inspects or
interprets the command string. Whether a command needs gating is decided
entirely by the flags stack.md declares for it.

Configuration: the auto_approve_privileged key in agentic-coder.yaml
(default false) pre-approves all requires_sudo commands without prompting,
for passwordless-sudo or fully automated environments. Commands marked
interactive always pause — they need a human at the terminal by definition,
and in a headless session (stdin closed) they are declined and skipped
rather than left to hang.
"""

from engine.llm import load_config


def confirm_command(
    command: str,
    reason: str,
    requires_sudo: bool = False,
    interactive: bool = False,
    config: dict | None = None,
) -> bool:
    """
    Gate for a single command (or a displayed batch of commands) the
    toolchain layer wants to execute.

    Returns True when the command may run:
        - neither flag is set (no gating needed), or
        - requires_sudo with auto_approve_privileged: true in config, or
        - the user answered yes at the prompt.

    Returns False when the user declines (or stdin is unavailable). The
    decline is logged here; the caller skips the command and continues.
    """
    if not requires_sudo and not interactive:
        return True

    if config is None:
        config = load_config()

    if config.get("auto_approve_privileged", False) and not interactive:
        print("[PERMISSION] Auto-approved privileged command "
              f"(auto_approve_privileged=true):\n             {command}")
        return True

    print("\n" + "─" * 60)
    print("[PERMISSION] The pipeline wants to run a gated command.")
    print(f"  Command: {command}")
    print(f"  Reason:  {reason}")
    if interactive:
        print("  This command needs interactive input. If approved, it runs\n"
              "  attached to this terminal — answer its prompts directly.\n"
              "  (Alternatively: decline here, run it yourself in another\n"
              "  terminal, and the pipeline will continue without it.)")
    elif requires_sudo:
        print("  This command requires elevated privileges and may ask for\n"
              "  your password.")
    print("─" * 60)

    try:
        answer = input("Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    approved = answer in {"y", "yes"}
    if not approved:
        print(f"[PERMISSION] Declined — skipping: {command}")
    return approved
