# Notebook tutorials

Jupyter notebooks that show what pylovo produces and how to use it. Start with the learning path below. The
folders hold the classification notebooks (`classification/`), the development notebooks of the MV stage
(`mv_grids/`) and the previous LV notebooks (`grid_generation/`). The notebooks only read; grids are generated on
the command line with `pylovo-generate` and `pylovo-mv`.

## Learning path

| Notebook | Question |
| --- | --- |
| `01_tour.ipynb` | What does pylovo produce? All grids of a postcode area, one grid, its voltage profile. |
| `02_building_grids.ipynb` | How does pylovo build the grids? Settlement type, buildings and loads, connections, clusters and stations, cables, checks. |
| `03_one_grid.ipynb` | What is inside one grid? The pandapower model, feeders, loading and voltage, the grid as a tree, export. |
| `04_comparing_areas.ipynb` | How do the grids of a city, a suburb and a village differ, and how do all Bavarian grids spread by settlement type? |
| `05_versions.ipynb` | How much do the settings matter? Four variants with one setting changed each. |
| `06_voltage_levels.ipynb` | How is an LV grid connected to the MV ring and the 110 kV grid? |

They need only the `notebooks` extra: without a database (or without these areas in it) they read the example
data in `data/examples`, three typical Bavarian postcode areas of a pylovo run for all of Bavaria
(`pylovo.examples.TutorialData`). With a database in your `.env`, the parameter cell of each notebook takes any
postcode area and version. The stored outputs are made from the example data with

```bash
uv run --extra notebooks python notebook_tutorials/refresh_outputs.py
```

which runs the notebooks without a database, stores the figures as small palette PNGs and refuses outputs with
errors, local paths or credentials.

## Folders

### `grid_generation/`

The LV notebooks before the learning path, kept with their stored outputs for comparison. Four of them call
plotting functions that were removed (`plot_contextily`, the spacing trees and the pie, histogram and box plots), so
they no longer run. In this order: Each has a parameter cell
at the top (`plz`, and `kcid`/`bcid` of one grid; `None` selects the first grid of the PLZ).

| Notebook | Content |
| --- | --- |
| `0_basic_grid_generation.ipynb` | How grids are identified (`plz`, `kcid`, `bcid`), read one pandapower network from the database and plot it. |
| `1_map_visualization_examples.ipynb` | Build up a grid on a map: postcode area, buildings, building clusters, peak loads, transformer, streets and cables. |
| `2_0_LV_pandapower_network.ipynb` | The pandapower tables of a grid: buses, lines, transformer, loads, power flow results and geometries. |
| `2_1_pandapower_networks_generic_coord.ipynb` | Plot a grid with generic (topology-based) coordinates instead of geographic ones. |
| `3_LV_networkx_graph.ipynb` | Convert a grid into a networkx graph, draw it as a tree or radially and compute the cable length from the transformer to the consumers. |
| `4_basic_plotting.ipynb` | Statistics of all grids of a PLZ: transformer sizes, cable types and lengths, parameters per transformer size, transformers on a map. |

The stored outputs were generated for the demo postcode area 85653 (Aying), whose buildings,
streets and transformers are derived from OpenStreetMap. Map data © OpenStreetMap contributors
(ODbL).

### `classification/`

The classification notebooks need a classification data set of many postcode areas (sample
set, generated and analysed grids, clustering parameters), which `pylovo-classify` creates; see
the user guide page [Classifying grids](https://pylovo.readthedocs.io/en/latest/user_guide/classification.html)
(`docs/source/user_guide/classification.rst`). Start with the two consolidated notebooks, whose stored outputs come
from a classification of 501 Bavarian postcode areas:

| Notebook | Question |
| --- | --- |
| `01_sample_and_key_figures.ipynb` | Which key figures describe an LV grid, and which should the clustering use? |
| `02_clusters_and_representatives.ipynb` | Which types of grids are there, and which grid represents each? |

The previous classification notebooks are kept for comparison; their stored outputs come from an earlier
classification run. In the order of the classification workflow:

1. `sampling/`: the sample set of postcode areas (`analyse_sampling_results.ipynb`).
2. `plz_parameter_analysis/`: the grid parameters of the sample (`analyse_clustering_parameters.ipynb`,
   `vsw_analysis.ipynb`).
3. `correlation_and_factor_analysis/`: choose the clustering parameters (`1_0` factor analysis,
   `1_1` explained variance, `1_2` correlation matrix).
4. `indices/`: choose the number of clusters (`1` Calinski-Harabasz index, `2` Davies-Bouldin
   index, `3` adjusted Rand index between the algorithms).
5. `clustering/`: cluster the grids with k-means (`2_0`, `2_1`) and a Gaussian mixture model
   (`3_0`, `3_1`); `*_0` shows the representative grids, `*_1` the clusters. The k-medoids
   notebooks were removed together with the k-medoids algorithm.

`config-example/` contains example values for `config/config_classification.yaml` and
`config/config_clustering.yaml`.

### `mv_grids/`

The MV grids of the MV stage (`pylovo-mv`); `mv_grids/README.md` describes the parameters and
prerequisites. They need MV variants of an LV version (`pylovo-mv generate`) and carry no
stored outputs.

| Notebook | Content |
| --- | --- |
| `0_mv_generation.ipynb` | How to run the MV stage (`pylovo-mv`, `--set`, the `pylovo-generate --mv` hook, MV variants) and the variants of a version. |
| `1_mv_results_overview.ipynb` | Tables per variant, region part, area class and district; maps of districts and rings; distributions of ring sizes, lengths, loadings, voltages and taps. |
| `2_mv_variant_comparison.ipynb` | Variants against a reference: solver, routing, sources, realism features, equipment, station sets of two LV versions. |
| `3_mv_plausibility.ipynb` | Literature ranges as traffic lights, eGon-data districts, Ding0 2023 grids, demand anchor, earth-fault currents. |
| `4_mv_lv_coupling.ipynb` | Results of `pylovo-mv couple`: LV busbar voltages, band conflicts, taps, merged operating point. |

## Prerequisites of the previous notebooks

- A pylovo database: a `.env` in the repository root with the connection settings (see
  `.env.example`) and a completed `pylovo-setup`.
- Generated grids for the postcode area of the parameter cell, e.g.

  ```bash
  uv run pylovo-generate --plz 85653
  ```

  Grids are read for the `VERSION_ID` of `config/config_generation_lv.yaml`. `4_basic_plotting.ipynb`
  also needs the PLZ analysis (`ANALYZE_GRIDS: True` in `config/config_generation_lv.yaml`, or
  `uv run pylovo-analyze --plz 85653`).
- Internet access for the basemap tiles of the map plots (OpenStreetMap) and for plotly.js,
  which the interactive plots load from a CDN.

## Installation

The `notebooks` extra installs the plotting extra (`plots`), a Jupyter kernel (`ipykernel`) and
`nbclient` for headless execution. From the repository root:

```bash
uv sync --extra notebooks
```

(`uv sync --all-extras` installs it as well.) No Jupyter front end is included; use VS Code or
start JupyterLab temporarily with `uv run --with jupyterlab jupyter lab`.

## Selecting the kernel and running the notebooks

Use the Python interpreter of the project environment, `.venv/bin/python`
(`.venv\Scripts\python.exe` on Windows), of the same checkout the notebooks belong to.

- **VS Code** (extensions *Python* and *Jupyter*): open a notebook, click *Select Kernel* (top
  right), choose *Python Environments* and then the `.venv` interpreter of the repository.
  Run the cells with *Run All* or one by one.
- **JupyterLab**: start it from the repository root with `uv run --with jupyterlab jupyter lab`,
  open a notebook in `notebook_tutorials/` and choose the kernel *Python 3 (ipykernel)*; it runs
  with the packages of the project environment.
- **Named kernel** (optional, e.g. for a Jupyter installation outside the project):

  ```bash
  uv run python -m ipykernel install --user --name pylovo --display-name "pylovo (.venv)"
  ```

  A registered kernel always starts the interpreter it was registered with. With several
  checkouts or worktrees, register one kernel per checkout under a distinct name.
- **Headless**, e.g. to refresh the stored outputs:

  ```bash
  uv run jupyter execute --inplace notebook_tutorials/grid_generation/*.ipynb
  ```

How pylovo finds its settings when it runs in a notebook (the kernel runs in the notebook's
directory):

- `.env`: the first `.env` found walking up from the notebook's directory, normally the one in
  the repository root. Its values override environment variables of the same name.
- YAML configuration: `config/` of the checkout (a `config/` directory next to the notebook would
  take precedence).
- Log files: `log/log.txt` in the notebook's directory (ignored by git).
