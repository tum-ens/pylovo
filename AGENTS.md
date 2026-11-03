# Project Instructions
These facts are always true for this project. Apply them to every session without exception. If any task conflicts with one of these, flag it before proceeding. The workflow (branches, one-line commit messages, changelog, pull requests) is in `CONTRIBUTING.md`, the code conventions and the tests in
`docs/source/development/index.rst`. These notes apply to human contributors as well.

## Technical Requirements
Tech stack for this project. Always use these. Never suggest alternatives unless asked:

- OS: Ubuntu
- Language: Python
- Package manager: `uv`
- Database: PostgreSQL with PostGIS

Additional technical rules:

- Find database connection details in the `.env` file.
- Always use `uv run` and the `pyproject.toml` environment to test changes.
- Use `<package> --help` to get further information on dependencies if required.
- Save all AI generated plans and summaries to be used by other AI agents in the respective directory within ../AI. This way I want to avoid adding those files to the git of the repositories.
- If required, define agentic workflows in the AI directory that can be followed by other agents. Either directly by orchestrating low ressource agents from the session or if that is not possible you may also define a file-structure for distributing and communication between different agents.

## Core Requirements
1. Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements.
2. Simplest solution first. Always implement the simplest thing that could work. Do not add abstractions or flexibility that weren't explicitly requested.
3. Don't touch unrelated code. If a file or function is not directly part of the current task, do not modify it, even if you think it could be improved. If you notice something worth fixing elsewhere, mention it in a note at the end. Do not touch it. Ever.
4. Flag uncertainty explicitly. If you are not confident about an approach or technical detail, say so before proceeding. Confidence without certainty causes more damage than admitting a gap.

Additional core rules:

- You are working in a greenfield project environment, so you don't need to add fallbacks for backward compatibility if not explicitly specified.
- For any task involving architecture decisions, debugging complex issues, or non-trivial features, work through the problem step by step before writing any code. Show your reasoning. Identify where you're uncertain. Then implement.
- Before any significant task, show me 2-3 ways you could approach this work and point out pros and cons. Wait for me to choose before proceeding.
- If you are uncertain about any fact, statistic, date, or piece of technical information, say so explicitly before including it. Never fill gaps in your knowledge with plausible-sounding information. When in doubt, say so.


## Databases

- The `.env` of a checkout usually points to a real database whose results took hours to generate. Do not run
  `pylovo-*` commands, scripts or tests against it unless you are asked to.
- The database tests run on a disposable container (`tests/db/run_database_tests.py`). It sets `PYLOVO_ENV_FILE=`
  (empty), so the `.env` is not read; do the same for any other command that must not reach the `.env` database.

## Data

- Never commit input data of grid operators, credentials or `.env` files.
- Every data file in git (tables, geodata, archives, dumps) needs its source and licence in `DATA_LICENSES.md`;
  the pre-commit hook `data-licenses` and `tests/test_packaging.py` check it.

## Before you push

The pre-commit hooks (`.pre-commit-config.yaml`) run on every commit. Do not skip them with `--no-verify`; if a hook
corrects files, add the corrections and commit again. Then run the checks of the CI:

```bash
uv run ruff check
uv run --extra api --extra plots --extra viewer --extra opendss pytest
uv run --extra api --extra plots python tests/db/run_database_tests.py   # needs Docker
uv run --no-project --with-requirements docs/requirements.txt sphinx-build -W --keep-going -b html docs/source docs/build/html
```

## Lint and tests

- The ruff version and rules are in `pyproject.toml`. Fix a finding at its cause; do not add `# noqa`, per-file
  ignores or remove rules unless you are asked to.
- A bug fix comes with a test that fails without the fix.
- Never make a check pass by skipping, deleting or loosening a test: fix the cause, or ask.
- `tests/db/toy_town_results.json` holds the key figures of the toy-town grids. If a change alters them on purpose,
  write the snapshot again (`PYLOVO_UPDATE_TOY_TOWN_SNAPSHOT=1 uv run --extra api --extra plots python
  tests/db/run_database_tests.py -- -k snapshot`) and commit it with the change and a changelog line. If the change
  should not alter the grids, a difference is a bug.
- A change of the HTTP API writes `api/openapi.json` again (`uv run --extra api python api/scripts/export_openapi.py`).

## Dependencies

- Do not upgrade the locked dependencies (`uv lock --upgrade`) unless you are asked to: a new numpy, pandas or
  pandapower can change the grids. After a change of `pyproject.toml` or `uv.lock`, `requirements.txt` is the export
  of `uv.lock` (the pre-commit hook writes it).
