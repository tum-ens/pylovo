# Notes for coding agents

pylovo generates synthetic low- and medium-voltage grids for research. The workflow (branches, one-line commit
messages, changelog, pull requests) is in `CONTRIBUTING.md`, the code conventions and the tests in
`docs/source/development/index.rst`. These notes apply to human contributors as well.

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
