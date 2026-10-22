"""Run several commands one after the other as one UI job.

``python -m pylovo_api.chain CMD1 ARGS… :: CMD2 ARGS… [:: …]``

The UI uses this for actions that consist of more than one ``pylovo-*`` command, e.g.
"regenerate" (``pylovo-delete networks`` then ``pylovo-generate``). The commands share the
job's process group and log; the chain stops at the first command that fails and exits with
its code. Each step starts with a ``### Step i/n: … ###`` line (shown as the job phase).
"""
from __future__ import annotations

import os
import subprocess
import sys

SEPARATOR = "::"


def split(argv: list[str]) -> list[list[str]]:
    """Split ``argv`` at ``::`` into the individual commands (empty parts are dropped)."""
    commands, current = [], []
    for arg in argv:
        if arg == SEPARATOR:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(arg)
    if current:
        commands.append(current)
    return commands


def build(*commands: list[str]) -> list[str]:
    """The argv of a chain job running ``commands`` in order."""
    argv = [sys.executable, "-m", "pylovo_api.chain"]
    for i, command in enumerate(commands):
        if i:
            argv.append(SEPARATOR)
        argv.extend(command)
    return argv


def main(argv: list[str] | None = None) -> int:
    commands = split(sys.argv[1:] if argv is None else argv)
    for i, command in enumerate(commands, start=1):
        shown = " ".join(os.path.basename(command[0]) if j == 0 else a for j, a in enumerate(command))
        print(f"### Step {i}/{len(commands)}: {shown} ###", flush=True)
        proc = subprocess.Popen(command)
        try:
            code = proc.wait()
        except KeyboardInterrupt:  # the child got the same SIGINT (one process group): let it clean up
            try:
                code = proc.wait(timeout=30)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                proc.kill()
                code = 130
            print(f"Chain cancelled during step {i}/{len(commands)}.", flush=True)
            return code or 130
        if code != 0:
            print(f"✗ Step {i}/{len(commands)} failed with exit code {code}; the remaining steps were not run.",
                  flush=True)
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
