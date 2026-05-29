# Error Notes

## 2026-05-26 - Patch helper blocked before bubblewrap reboot

- What didn't work: `apply_patch` and sandboxed shell commands failed before the machine reboot because bubblewrap could not configure loopback networking.
- What worked instead: Temporary escalated `uv run python` edits were used before the reboot; after reboot, normal sandboxed shell commands worked again, but the `apply_patch` helper still hit the bubblewrap filesystem issue.
- Note for next time: If filesystem tooling fails with `bwrap: loopback: Failed RTM_NEWADDR`, if shell commands work but `apply_patch` still fails, keep edits scoped and use `uv run python` as a temporary workaround.
