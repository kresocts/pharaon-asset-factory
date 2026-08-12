#!/usr/bin/env python3
"""Run the repository's canonical baseline CI checks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
STAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "tests": (
        "automated tests",
        (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
    ),
    "metadata": (
        "repository metadata validation",
        (sys.executable, "validation/validate_repository.py"),
    ),
}


def run_stage(stage: str) -> int:
    """Run one named stage from the repository root and return its exit code."""
    label, command = STAGES[stage]
    print(f"==> Running {label}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    outcome = "passed" if result.returncode == 0 else f"failed (exit {result.returncode})"
    print(f"==> {label}: {outcome}", flush=True)
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        action="append",
        choices=tuple(STAGES),
        help="Run only this stage; repeat to select multiple stages. Defaults to all stages.",
    )
    args = parser.parse_args(argv)
    selected_stages = args.stage or list(STAGES)
    for stage in selected_stages:
        exit_code = run_stage(stage)
        if exit_code:
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
