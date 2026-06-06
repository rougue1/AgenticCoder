#!/usr/bin/env python3
"""
agentic-coder — entry point.

Usage:
    python main.py                  # Full run (SDD → Architect → Surgeon → Healer loop)
    python main.py --resume         # Resume from last checkpoint (skips prompt)
    python main.py --tasks-only     # Skip SDD generation, go straight to task loop
    python main.py --status         # Print checkpoint + task queue status and exit
"""

import sys
import argparse
from pathlib import Path

# Ensure project root is on sys.path so all engine/spec/core imports resolve
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from core.orchestrator import boot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agentic-coder",
        description=
        "Local multi-tier LLM autopilot for Spec-Driven Development.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Auto-resume from last checkpoint without prompting.",
    )
    parser.add_argument(
        "--tasks-only",
        action="store_true",
        help="Skip SDD document generation and go directly to the task loop.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current checkpoint and task queue status, then exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    root_dir = Path(__file__).parent.absolute()

    if args.status:
        _print_status(root_dir)
        sys.exit(0)

    # Inject CLI flags as env-like signals the orchestrator reads
    if args.resume:
        import os
        os.environ["AGENT_AUTO_RESUME"] = "1"

    if args.tasks_only:
        import os
        os.environ["AGENT_TASKS_ONLY"] = "1"

    boot()


def _print_status(root_dir: Path) -> None:
    """Prints checkpoint and task queue state without running the pipeline."""
    from core.checkpoint import load_checkpoint
    from spec.tasks import count_tasks

    checkpoint = load_checkpoint(root_dir)
    completed, total = count_tasks(root_dir / "tasks.md")

    print("\n── AGENTIC-CODER STATUS ─────────────────────────")
    if checkpoint:
        print(f"  Checkpoint:     FOUND")
        print(
            f"  Last task:      {checkpoint.get('last_completed_task', '?')[:60]}"
        )
        print(
            f"  Progress:       {checkpoint.get('task_index', '?')}/{checkpoint.get('total_tasks', '?')}"
        )
        print(f"  Last updated:   {checkpoint.get('last_updated', '?')}")
    else:
        print(f"  Checkpoint:     None")

    print(f"  tasks.md:       {completed}/{total} complete")

    sdd_exists = ((root_dir / "requirements.md").exists()
                  and (root_dir / "design.md").exists()
                  and (root_dir / "tasks.md").exists())
    print(f"  SDD docs:       {'✓ Present' if sdd_exists else '✗ Missing'}")
    print("─────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
