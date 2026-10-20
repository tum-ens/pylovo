# pylovo-viewer

Read-only browser map of a pylovo database: LV grids, MV grids, input data and grid context. The
user guide is `docs/source/user_guide/viewer.rst`; this file is for developers.

## Run and test

```bash
uv sync --extra viewer
uv run --extra viewer pylovo-viewer                 # http://127.0.0.1:8770/ (--env FILE, --port, --no-browser)
uv run --extra viewer pylovo-viewer fetch etrago    # eTraGo grid of Germany into ~/.cache/pylovo-viewer
uv run --extra viewer pytest viewer/tests           # no database needed
```

## Layout

| File | Role |
| --- | --- |
| `pylovo_viewer/cli.py` | `pylovo-viewer` and `pylovo-viewer fetch etrago`; parses arguments before anything else |
| `pylovo_viewer/settings.py` | the `.env` (pylovo's lookup, or `--env`), read without loading it into `os.environ` |
| `pylovo_viewer/db.py` | pooled connections, started read-only; every transaction read-only with a statement timeout |
| `pylovo_viewer/databases.py` | the databases offered on the server; one site (pool, catalog, layers, cache) per database |
| `pylovo_viewer/catalog.py` | tables and columns of the database; decides which layers are available |
| `pylovo_viewer/layers.py` | the layer registry: one `Layer` (SQL, scope, requirements, identify query) per map layer |
| `pylovo_viewer/topology.py` | neighbour-aware colour slots and feeder numbers |
| `pylovo_viewer/regions.py`, `summary.py` | versions, MV runs, regions, search, bounds; key figures |
| `pylovo_viewer/lod2.py` | LoD2 meshes of a region (query here, triangulation and format from `pylovo_api.lod2`) |
| `pylovo_viewer/etrago.py` | download and cache of the eTraGo grid from the Open Energy Platform |
| `pylovo_viewer/app.py` | FastAPI app: the page, static files and the GET routes under `/api` (parameter `db`) |
| `pylovo_viewer/static/` | the page: MapLibre GL JS and Vue from jsDelivr (import map, no build step) |

In `static/js/`: `main.js` (app state, loading, inspector, URL), `map.js` (MapLibre, draw order,
picking), `styles.js` (colour modes, MapLibre layers, tooltip facts), `palette.js` (colours and
schemes), `lod2.js` (LoD2 custom layer, adapted from the GridPlanner UI).

## Adding a layer

1. `layers.py`: a `Layer` in `build_layers` whose SQL returns `fid`, `geometry` (GeoJSON text in
   EPSG:4326) and the properties the style needs, with its scope, required tables and identify query.
2. `static/js/styles.js`: an entry in `STYLES` (colour modes, MapLibre layers, facts).
3. `static/js/map.js`: its place in `Z_ORDER`.

## Rules

- Read only: no writes, no `pylovo` imports (they load the checkout's `.env` with `override=True`),
  no `pylovo-*` commands. The one piece of pylovo code the viewer runs is `pylovo/version_snapshot.py`
  (no configuration, no database), loaded from its file in `regions.py` to tell retired MV runs.
- Colours: validated palettes only (see `palette.js`); map styles are MapLibre expressions, not
  colours written into the data.
