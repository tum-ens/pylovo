# pylovo-api

`pylovo-api` is the headless HTTP API of pylovo (FastAPI). It covers the whole workflow except
classification and the MV stage: set up the database, pick regions, prepare transformer data, edit
the generation config, generate grids and inspect the results (statistics, grid detail,
diagnostics, power flow, load editing). Long or database-writing steps run the existing
`pylovo-*` commands as background jobs with live logs. The MV stage is a command-line workflow
(`pylovo-mv`, `config_generation_mv.yaml`): a generation job passes `--no-mv`, so
`MV_GRID_GENERATION` has no effect on runs started through the API.

**The browser UI lives in GridPlanner.** GridPlanner serves the UI and puts this API behind a
reverse proxy (`/pylovo/api/*` → `pylovo-api:/api/*`). pylovo itself serves no page, no static
files and no plugins.

## Run it

```bash
uv sync --extra api                  # FastAPI, uvicorn, python-multipart (+ httpx for the tests)
uv run --extra api pylovo-api        # http://127.0.0.1:8765/api/health, API docs at /docs
```

`pylovo-api` (or `uv run python -m pylovo_api`) starts in your pylovo checkout: it looks for
`config/config_generation_lv.yaml` in `--root DIR`, `$PYLOVO_ROOT`, the current directory and its
parents, and the checkout the package was installed from, and switches to that directory (every
job runs there, exactly like the CLI). The database is the one in the project's `.env`, or in the
environment variables `DBNAME`, `DBUSER`, `PASSWORD`, `HOST`, `PORT` when there is no `.env`
(pylovo needs them at start-up, even for `/api/health`).

| Option | Default | |
| --- | --- | --- |
| `--host` | `127.0.0.1` | interface to bind; `0.0.0.0` accepts every `Host` header and prints a warning |
| `--port` | `8765` | |
| `--root` | auto-detect | project directory with `config/` |
| `--log-level` | `warning` | uvicorn log level |
| `--allowed-host HOST` | – | also accept this `Host` header (repeatable; also `PYLOVO_API_ALLOWED_HOSTS`, comma-separated), e.g. the proxy's host name |

### In a container

`api/docker/Dockerfile` builds the image `pylovo` (pylovo, GDAL and the API; no `.env`, no uv
cache, no dev tools, no plotting packages: the `plots` extra is not installed because no module of
the API or of its job commands imports them). Its default command is
`pylovo-api --host 127.0.0.1 --port 18765`, so run it with host networking or override the command.
The container runs as the unprivileged user `pylovo` (id 1000): mounted folders must be writable
for that id (`user:` in compose or `docker run --user` for another owner). The image has a
`HEALTHCHECK` that requests `/api/health` on port 18765. CI (`.github/workflows/pylovo-image.yml`) publishes
`ghcr.io/<owner>/pylovo` with the tags `<branch>` (`feature/x` → `feature-x`), `X.Y.Z` and `X.Y`
for version tags, `sha-<short>` and `latest` from the default branch; it passes the commit as
build argument `PYLOVO_REVISION`, which `/api/health` reports.

```bash
docker build -f api/docker/Dockerfile --build-arg PYLOVO_REVISION=$(git rev-parse HEAD) -t pylovo .
PYLOVO_REVISION=$(git rev-parse HEAD) docker compose -f api/docker/compose.yaml up --build
```

Compose uses host networking and mounts `.env` (read-only), `config/`, `data/`, `log/` and
`.pylovo-api/` of the checkout. They must exist before the first start (`cp .env.example .env` and
edit the database settings, `mkdir -p .pylovo-api`): otherwise compose stops with "bind source path
does not exist" instead of creating root-owned folders that the container user cannot write to.
`PYLOVO_REVISION` is the commit that `/api/health` reports (`null` without it).

## Security

The API can drop the `pylovo` schema, delete results and overwrite the configuration. There is no
user management.

* It binds to `127.0.0.1` by default and rejects requests whose `Host` header is not a loopback
  name, the `--host` value or an `--allowed-host` (DNS rebinding) with 421.
* Every state-changing call (not `GET`/`HEAD`/`OPTIONS`) under `/api/` needs the header
  `X-Pylovo-UI: 1` (browsers only send it from same-origin scripts: CSRF protection), else 403.
* Destructive actions need a typed confirmation (database name, version id, PLZ, DSO source) or
  an explicit `confirm` or `force` flag that the server checks.
* Only one database-writing job runs at a time; direct edits (transformers, configuration, loads)
  are refused with 409 while one runs, and no job can start while such an edit is being written
  (the edit holds a lease of the job manager for the duration of the write).

## Contract

The GridPlanner UI relies on this API. The rules:

* **Routes are stable.** Paths, methods, parameters and response shapes stay as they are,
  including the `X-Pylovo-UI` header, the `Host` allowlist, the Server-Sent Events of
  `/api/jobs/{id}/events` and the file downloads.
* **`GET /api/health`** (no database access) answers
  `{"ok": true, "service": "pylovo-api", "api": 2, "version": "<pylovo version>", "revision": "<git sha or null>"}`.
  `api` is the contract version `API_VERSION` in `pylovo_api/__init__.py` (also `info.version` of
  the schema; 2 at the time of writing). Bump it only for breaking changes (a removed or renamed route, parameter or
  response field, a new required parameter); the UI refuses API versions it does not know.
  `revision` is `$PYLOVO_REVISION` or `git rev-parse HEAD` of the checkout.
* **`api/openapi.json`** is the committed OpenAPI snapshot (FastAPI's schema with sorted keys).
  After changing a route, rewrite it and commit it with the change:

  ```bash
  uv run --extra api python api/scripts/export_openapi.py           # --check only compares
  ```

  `api/tests/test_openapi.py` fails while the app and the snapshot differ. On pull requests,
  `.github/workflows/api-contract.yml` runs that test and `oasdiff breaking` of the snapshot
  against the base branch's; breaking changes fail unless `API_VERSION` was increased.
  Most responses are plain JSON objects without a schema, so the check guards paths, methods,
  parameters and request bodies (and the typed `/api/health` answer), not every response field.

## Endpoints

Interactive documentation: `http://127.0.0.1:8765/docs`; the full list is `api/openapi.json`.

| Router (`pylovo_api/routers/`) | Endpoints | Purpose |
| --- | --- | --- |
| `status.py` | `GET /api/health` · `GET /api/status` | liveness and contract version · database, schema, counts, config version, running job, region check, code state (`ui.code.stale`: restart after a `git pull`) |
| `coverage.py` | `GET /api/regions/coverage` · `POST /api/regions/coverage/refresh` · `GET /api/regions/input?plz=` | region input check: summary · recount · one PLZ |
| `regions.py` | `GET /api/regions/search?q=` · `/available` · `/overview` · `/postcodes?bbox=&zoom=` · `/ags/{ags}` · `/{plz}` | PLZ / AGS / name search, PLZ with input data (paged), extent, postcode polygons (GeoJSON), PLZ of a municipality, PLZ details |
| `transformers.py` | `GET /api/transformers?plz=`/`?bbox=` · `/capacities` · `/osm-relations?q=`; `POST /api/transformers` · `/bulk` · `/clear-capacities` · `/dso-csv/preview`; `PATCH`/`DELETE /api/transformers/{osm_id}` | candidates (GeoJSON), ratings, Nominatim lookup; add, bulk ratings, clear, DSO CSV upload; set rating, delete (`force=true` if a grid uses it) |
| `config.py` | `GET`/`PUT /api/config` · `POST /api/config/validate` · `/values` · `/restore` · `GET /api/config/backups/{name}` | config text, form values and help; save validated text; form edits (`dry_run` for the diff); backups |
| `jobs.py` | `POST /api/jobs/{setup,reset,generate,analyze,delete-versions,delete-networks,export,import-osm,import-dso}` · `GET /api/jobs/generate/check` · `GET /api/jobs` · `/{id}` · `/{id}/log` · `/{id}/events` (SSE) · `/{id}/files[/{name}]` · `POST /api/jobs/{id}/cancel` | start a CLI job (`setup` is non-destructive; `reset` runs `pylovo-setup reset --database NAME --yes` after the typed confirmation of the database name), check a generation beforehand, observe, stream, download exports, cancel |
| `flow.py` | `GET /api/flow/generate-state` · `POST /api/flow/config-preflight` · `/regenerate` · `/reanalyse` · `/analyse-all` | per-PLZ state, saved config vs. the version's stored parameters, chained jobs |
| `results.py` | `GET /api/versions` · `/versions/{id}` · `/versions/compare?a=&b=` · `/results/{version}/summary?plz=` · `/results/{version}/{plz}/map` · `/grids/{id}` · `/grids/{id}/pandapower.json` · `/grids/{id}/lod2`; `POST /api/grids/{id}/powerflow?load_scaling=` | versions and parameter diff, statistics, grids of a PLZ (GeoJSON), grid detail, pandapower download, LoD2 mesh, on-demand power flow |
| `diagnostics.py` | `GET /api/grids/{id}/diagnostics` · `/results/{version}/diagnostics?plz=` · `/diagnostics/rules` | grid diagnostics of a grid or a PLZ, rule catalogue |
| `load_edits.py` | `GET /api/grids/{id}/load-edit/{building,check}` · `POST /api/grids/{id}/load-edit/{preview,revert}` · `GET`/`POST /api/grids/{id}/load-edits` · `POST /api/grids/{id}/load-edits/undo-all` · `POST /api/load-edits/{id}/undo` · `GET /api/versions/{v}/load-edits?format=csv` · `POST /api/maintenance/refresh-views` | load editor: inputs, reproduction check, preview, apply, undo, history |

## Behaviour

* **Jobs** run the `pylovo-*` command with the project root as working directory, stream its
  output (Server-Sent Events) and can be cancelled. Job logs survive a restart. The record of a
  job (`.pylovo-api/jobs/<id>.json`, with the pid of its process) is written when the process
  starts and updated when it ends. A job that was still running when the server stopped or
  crashed is `interrupted` after the next start; if its process still exists it stays `running`
  (and blocks other writers) until it has ended, and can be cancelled. When the server exits,
  running jobs get SIGTERM, and SIGKILL if they are still running after 3 s.
* **Region input check** (`coverage.py`, `gate.py`): a PLZ can be generated only when it has a
  postcode polygon, at least two buildings pylovo would import (the filters of
  `pylovo-generate`), a residential building, street segments and connection lines. It runs per
  AGS in the background on the InfDB indexes (only while the UI is used: requests to `/api/`
  other than `/api/health` count as use) and is cached. `POST /api/jobs/generate` refuses blocked
  PLZ unless they are included explicitly. Environment: `PYLOVO_API_COVERAGE_ENFORCE=0` (warn
  instead of block), `PYLOVO_API_COVERAGE_TTL_S`, `PYLOVO_API_COVERAGE_SMALL_ROWS`,
  `PYLOVO_API_COVERAGE_TOOLS`.
* **Transformer edits** go through the `*_trafo_ui` methods of `DatabaseClient`; new positions
  get the id `manual/<epoch ms>`.
* **Config edits** rewrite only the changed keys, validate the YAML, import
  `pylovo.config_loader` in a subprocess against a temporary copy, refuse to overwrite a file that
  changed meanwhile and keep a backup. Jobs always read the saved file; saving is refused with 409
  while a database-writing job runs, and two saves at once are processed one after the other.
* **Load edits** write in one transaction through `pylovo.load_editing.LoadEditor`
  (`buildings_result`, the grid's `pandapower_load` rows and stored net, `grid_result`, an audit
  row in `pylovo.load_edit`) and remove the load-dependent analysis rows of the PLZ. Undo is exact.
* **Files** (all inside the project, ignored by git): `.pylovo-api/config-backups/` (the newest 50
  backups), `uploads/` (DSO CSVs, deleted after 24 hours), `jobs/` (records and logs of the newest
  60 jobs), `exports/` (the output of those jobs), `tmp/`, `cache/input-coverage.json`. The files
  of older jobs are deleted when the server starts and when a job starts, old uploads at the start
  and with every new upload (`HISTORY_LIMIT` in `jobs.py`, `UPLOAD_MAX_AGE_H` in `routers/transformers.py`).

## Architecture

```
api/
  pylovo_api/
    cli.py            pylovo-api: finds the project root, chdir, uvicorn
    app.py            FastAPI app, Host/CSRF guard, error mapping
    openapi.py        OpenAPI snapshot rendering (api/openapi.json)
    settings.py       project root and .pylovo-api/ paths
    db.py             psycopg2 helper (pylovo's settings, one short connection per request)
    jobs.py           JobManager: pylovo-* subprocesses, logs, progress, cancel, history
    config_io.py      config read / form edits / validation / backups
    queries.py        read queries → GeoJSON in EPSG:4326 (6 decimals)
    topology.py       feeders and cable distances of a radial grid (pure Python)
    powerflow.py      on-demand pandapower power flow
    grid_metrics.py   generation-check figures read from the stored net (no new power flow)
    cabinets.py       cable cabinets K1…Kn (shared by grid detail and diagnostics)
    diagnostics.py    grid diagnostics rules (pure Python); diagnostics_data.py feeds them
    coverage.py       region input check (per-AGS engine, cache); gate.py applies it to generation
    preflight.py      saved config vs stored snapshot of VERSION_ID
    job_outcomes.py   per-PLZ outcomes parsed from generation logs
    chain.py, tasks.py  chained jobs (regenerate) and small DB tasks (clear analysis rows)
    load_edit_service.py  database adapter of pylovo.load_editing
    lod2.py           LoD2 models of a grid's buildings from citydb as a binary mesh
    code_state.py     running code vs code on disk, git revision
    deps.py           shared router helpers
    routers/          one router per area (table above)
  openapi.json        contract snapshot
  scripts/            export_openapi.py
  tests/              pytest (FastAPI TestClient)
  docker/             Dockerfile, compose.yaml
```

## Tests

```bash
uv run --extra api --extra plots pytest                                       # tests and api/tests, no database needed
PYLOVO_API_TEST_DATABASE=<sandbox db> uv run --extra api --extra plots pytest  # + database tests
```

The database tests only run when the variable names the database of the project's `.env` (use a
sandbox: they insert and delete a manual transformer and apply and undo load edits). The browser
tests (static JS checks, Playwright walk-throughs) live in GridPlanner.

## Known limitations

* The key figures of `pylovo-analyze` and `pylovo-export` always use the `VERSION_ID` of the config
  (the CLIs have no version option for them).
* Setup, generation and imports are exactly the CLI runs: a generation for a PLZ that already has
  grids of the config version is skipped by pylovo; regenerate (delete + generate) or use a new
  version instead (`/api/jobs/generate/check` reports this beforehand).
* Load editing changes loads only; cables and transformer stay as generated. `pylovo-analyze`
  run outside the API can re-insert pre-edit analysis values. Audit rows are deleted together
  with their version or PLZ.
* One server process = one project root and one database.
