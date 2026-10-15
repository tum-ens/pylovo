# MV grid notebooks

Notebooks that run and analyse the medium-voltage (MV) stage of pylovo (`pylovo-mv`, package
`pylovo.mv_grid`). They only read from the database: the shared client of `mv_notebook_utils.py` runs read-only
transactions. MV grids are generated on the command line (`0_mv_generation.ipynb` shows how).

| Notebook | Content |
| --- | --- |
| `0_mv_generation.ipynb` | How to run the MV stage: `pylovo-mv generate/couple/list/delete/export/graph`, `--set`, the `pylovo-generate --mv` hook, the MV parameters and constants, the variant matrix; lists the MV variants of a version, their parameter differences and run summaries, the QA tables of the routing graph and the result readers. |
| `1_mv_results_overview.ipynb` | Tables per variant, region part, area class and district (substations, districts, stations, MV customers with own nodes or at station busbars, generators, rings, stubs, ties, circuit km of cable and overhead lines, km per station and per km², ring members, half-ring lengths, loading and voltage per case, repairs, band conflicts, taps, demand anchor); maps of the districts and of the rings; distributions of ring sizes and lengths, line loadings, MV voltages, taps and the voltage budget. |
| `2_mv_variant_comparison.ipynb` | The variants of one LV version against a reference variant: parameter differences, side-by-side metrics and their change, distributions with Wasserstein distances, paired districts, ring membership (adjusted Rand index), and sections on the solver, routing, sources, realism features and equipment; side-by-side maps; the same variant on two LV versions (station sets). |
| `3_mv_plausibility.ipynb` | Literature ranges (`metrics.LITERATURE_RANGES`) as traffic lights per area class, circuit km per km² and cable share of the region, eGon-data MV grid districts (area, substation match), Ding0 2023 grids of the same districts, the demand anchor, earth-fault currents and the pandapower net `mv_oberrhein` through the same metric code. |
| `4_mv_lv_coupling.ipynb` | Results of `pylovo-mv couple`: LV busbar voltages at the design point against the LV reference, analytic budget against the coupled LV nets, lowest LV voltage and feed-in, band conflicts and taps, the merged operating point, LV grids left out of the merged flow and station-busbar customers. |

## Parameters

Every notebook starts with a cell tagged `parameters`:

- `version_id`: the LV version (default: `VERSION_ID` of `config/config_generation.yaml`);
- `variants`: the MV variants (default: all MV variants of the version);
- `regions`, `plz`: region filter. The planning region of a variant is the union of its PLZ polygons; groups of
  PLZ more than 5 km apart are region parts, numbered by area (1 = largest). A district belongs to the part nearest
  to its substation. `regions` keeps the districts of some parts, `plz` the districts with nodes in some PLZ;
- `dbname`: the expected database name of the `.env` (the notebook stops if it differs); `repo_root`: the
  repository root (default: two levels above the notebook);
- notebook-specific ones: `grid_id` (district of the detail maps), `basemap` (OpenStreetMap tiles, needs internet
  access), `reference`, `compare_versions` and `compare_variant` (notebook 2), `complete_only`, `ding0_dir` and
  `egon_min_share` (notebook 3).

To execute a copy with other parameters, insert a cell with the new values after the `parameters` cell (as
papermill does, e.g. `papermill 1_mv_results_overview.ipynb out.ipynb -p version_id <version>` if papermill is
installed) and run it with `uv run --extra notebooks jupyter execute --inplace <copy>`. The notebooks change to
the repository root first, so a copy may live in any folder when `repo_root` is set.

## Prerequisites

- pylovo with the `notebooks` extra (`uv sync --extra notebooks`), see `../README.md` for the kernel.
- A `.env` that points to a database with MV variants (`pylovo-mv list --version <version>`).
- `4_mv_lv_coupling.ipynb`: coupling results (`pylovo-mv couple --version <version> --variant <variant>`).
- `3_mv_plausibility.ipynb`: the eGon-data tables `egon_hvmv_substation` and `egon_mv_grid_district`
  (`pylovo-import egon` or the opendata schema) and, for the Ding0 comparison, the Ding0 2023 MV grids
  (Zenodo record 10405129, licence CC BY-SA 4.0) as `<ding0_dir>/<bus_id>/topology/*.csv`.

Map data © OpenStreetMap contributors (ODbL).
