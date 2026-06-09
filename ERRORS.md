# Error Notes

## 2026-05-26 - Patch helper blocked before bubblewrap reboot

- What didn't work: `apply_patch` and sandboxed shell commands failed before the machine reboot because bubblewrap could not configure loopback networking.
- What worked instead: Temporary escalated `uv run python` edits were used before the reboot; after reboot, normal sandboxed shell commands worked again, but the `apply_patch` helper still hit the bubblewrap filesystem issue.
- Note for next time: If filesystem tooling fails with `bwrap: loopback: Failed RTM_NEWADDR`, if shell commands work but `apply_patch` still fails, keep edits scoped and use `uv run python` as a temporary workaround.

## 2026-06-02 - Large Python edit quoting after apply_patch failure

- What didn't work: `apply_patch` hit the known bubblewrap loopback error, and an escalated `uv run python -c` one-liner failed because the large embedded SQL/Python replacement was too quote-heavy for shell parsing.
- What worked instead: A scoped `uv run python` heredoc edit updated the target file successfully.
- Note for next time: For large SQL block replacements after `apply_patch` fails, use a scoped heredoc Python edit instead of a dense one-liner.

## 2026-06-02 - Markdown backticks in shell Python edit

- What did not work: `apply_patch` hit the known bubblewrap loopback failure, and the first `uv run python -c` memory append used Markdown backticks inside a double-quoted shell argument, so the shell treated them as command substitutions.
- What worked instead: Use a single-quoted shell argument for `python -c` when the embedded Markdown contains backticks, or avoid inline backticks in the shell string entirely.
- Note for next time: For scoped Markdown edits after `apply_patch` fails, protect backticks with single-quoted shell strings before running the Python edit.

## 2026-06-03 - Shell backticks damaged Markdown memory entry

- What did not work: Writing a `MEMORY.md` entry through `uv run python -c` inside a double-quoted shell command allowed Markdown backticks to be interpreted as shell command substitution.
- What worked instead: Rewrote the affected entry using a single-quoted shell command around the Python snippet so Markdown backticks were passed literally.
- Note for next time: When using the fallback Python edit path, wrap the outer shell command in single quotes or avoid literal Markdown backticks in double-quoted command strings.

## 2026-06-09 - Markdown backticks broke fallback Python edit again

- What did not work: After `apply_patch` failed with the known bubblewrap loopback issue, a double-quoted `uv run python -c` fallback for `improvements.md` allowed Markdown backticks to trigger shell command substitution.
- What worked instead: Reran the same scoped `uv run python -c` edit with a single-quoted outer shell string, so Markdown backticks were passed literally to Python.
- Note for next time: For fallback Python edits that include Markdown backticks, use a single-quoted outer command immediately.

## 2026-06-09 - Scoped fallback edits need small quote-safe replacements

- What did not work: apply_patch failed with the known bubblewrap loopback error. A full-block uv run python edit wrapped in single shell quotes broke when the embedded SQL contained a single quoted Transformer literal. A large triple-quoted test-file payload also produced an unterminated string error.
- What worked instead: Use small insertion or line-level replacements with quote-safe Python strings, or rewrite files with a single outer shell quote only when the payload avoids single quote characters.
- Note for next time: After apply_patch fails, prefer small marker-based replacements over embedding large original blocks, especially when SQL or Markdown text contains mixed quote styles.
