# MV grid notebooks

Notebooks that run and analyse the medium-voltage (MV) stage of pylovo (`pylovo-mv`, package
`pylovo.mv_grid`). They only read from the database: the shared client of `mv_notebook_utils.py` runs read-only
transactions. MV grids are generated on the command line (`0_mv_generation.ipynb` shows how).

| Notebook | Content |
| --- | --- |
| `0_mv_generation.ipynb` | How to run the MV stage: `pylovo-mv generate/couple/list/delete/export/graph`, `--set`, the `pylovo-generate --mv` hook, the MV parameters and constants, the variant matrix; lists the MV variants of a version, their parameter differences and run summaries, the QA tables of the routing graph and the result readers. |
| `1_mv_results_overview.ipynb` | A compact run and electrical health table, region and district maps, one ring, and key distributions for one selected variant. Set `show_details=True` for the full region, area-class, district, repair, and voltage-budget detail. |
| `2_mv_variant_comparison.ipynb` | The variants of one LV version against a reference variant: parameter differences, side-by-side metrics and their change, distributions with Wasserstein distances, paired districts, ring membership (adjusted Rand index), and sections on the solver, routing, sources, realism features and equipment; side-by-side maps; the same variant on two LV versions (station sets). |
| `3_mv_plausibility.ipynb` | Literature ranges (`metrics.LITERATURE_RANGES`) as traffic lights per area class, circuit km per km² and cable share of the region, eGon-data MV grid districts (area, substation match), Ding0 2023 grids of the same districts, the demand anchor, earth-fault currents and the pandapower net `mv_oberrhein` through the same metric code. |
| `4_mv_lv_coupling.ipynb` | Results of `pylovo-mv couple`: LV busbar voltages at the design point against the LV reference, analytic budget against the coupled LV nets, lowest LV voltage and feed-in, band conflicts and taps, the merged operating point, LV grids left out of the merged flow and station-busbar customers. |

`circuit_km` counts installed parallel circuits (`length_km × parallel`); `route_km` counts each routed line
once. Ring and feeder distances remain geometric paths. The region-clipped inventory includes ties, while
district inventory reports ties separately. Load-only variants omit generation topology and feed-in checks, so
their shorter networks cannot be read as equal-service improvements over generation-enabled variants.

## Parameters

Every notebook starts with a cell tagged `parameters`:

- `version_id`: the LV version (this review checkout defaults to `"mv2"`; `None` uses `VERSION_ID` of `config/config_generation_lv.yaml`);
- `variants`: the MV variants. In notebooks 1, 3, and 4, `None` selects `load1`, then `d1`, then the first available variant. In notebooks 0 and 2, `None` selects all variants. An explicit list is kept as selected;
- `show_details` (notebook 1): include the full tables and additional diagnostic figures (default: `False`);
- `map_variants` (notebook 1): variants for every map and district/ring detail; `None` means the selected variants and `[]` suppresses those figures;
- `reference` (notebook 2): `None` prefers `load1`, then `d1`; an explicit id must be in `variants`;
- `regions`, `plz`: region filter. The planning region of a variant is the union of its PLZ polygons; groups of
  PLZ more than 5 km apart are region parts, numbered by area (1 = largest). A district belongs to the part nearest
  to its substation. `regions` keeps the districts of some parts, `plz` the districts with nodes in some PLZ;
- `dbname`: the expected database name of the `.env` (this review checkout uses `"infdb_mv"`; the notebook stops if it differs); `repo_root`: the
  repository root (default: two levels above the notebook);
- notebook-specific ones: `grid_id` (district of the detail maps in the first mapped variant), `basemap` (OpenStreetMap tiles, needs internet
  access), `reference`, `compare_versions` and `compare_variant` (notebook 2), `complete_only`, `ding0_dir` and
  `egon_min_share` (notebook 3).

To execute a copy with other parameters, insert a cell with the new values after the `parameters` cell (as
papermill does, e.g. `papermill 1_mv_results_overview.ipynb out.ipynb -p version_id <version>` if papermill is
installed) and run it with `uv run --extra notebooks jupyter execute --inplace <copy>`. The notebooks change to
the repository root first, so a copy may live in any folder when `repo_root` is set.

## Kernel and existing results

Select the `.venv/bin/python` interpreter **inside the same checkout** as these notebooks. A kernel named
`pylovo` may point to another worktree that does not contain `pylovo.mv_grid`. The setup cell checks the package
location and gives the required interpreter path before loading configuration. Restart the kernel after changing it.
For a separate MV worktree, register a distinct kernel from its repository root:

```bash
uv run --extra notebooks python -m ipykernel install --user --name pylovo-mv --display-name "pylovo-mv (.venv)"
```

Set `version_id` in the parameters cell to the LV version whose MV results you want to inspect. Leaving it as
`None` uses the generation configuration, which may name a version with no MV results even when other versions
have completed runs. Set `dbname` as well to verify the intended database. Executed copies outside the checkout
also need an explicit `repo_root`.

## Prerequisites

- pylovo with the `notebooks` extra (`uv sync --extra notebooks`), see `../README.md` for the kernel.
- A `.env` that points to a database with MV variants (`pylovo-mv list --version <version>`).
- The default `MV_INCLUDE_GENERATION=false` needs no MaStR import. Set it to `true` under a separate variant id
  to plan generator connections and request the feed-in cases. Older snapshots without this field retain the
  historical generation-enabled meaning.
- `4_mv_lv_coupling.ipynb`: coupling results (`pylovo-mv couple --version <version> --variant <variant>`).
- `3_mv_plausibility.ipynb`: the eGon-data tables `egon_hvmv_substation` and `egon_mv_grid_district`
  (populated in `pylovo` or available in the opendata schema) and, for the Ding0 comparison, the Ding0 2023 MV grids
  (Zenodo record 10405129, licence CC BY-SA 4.0) as `<ding0_dir>/<bus_id>/topology/*.csv`.

Map data © OpenStreetMap contributors (ODbL).
