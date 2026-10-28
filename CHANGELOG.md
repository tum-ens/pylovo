
# Changelog

All notable changes to this project will be documented in this file. 
See below for the format and guidelines for updating the changelog.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]
- Add new changes here before merging into the next official version.

### Added
- Add the `opendss` extra for the OpenDSS backend
- Select the lines a transformer supplies with a click in `pylovo-viewer`: the LV cables of an MV/LV substation or the MV lines of an HV/MV substation's supply area (see [Map viewer](https://pylovo.readthedocs.io/en/latest/user_guide/viewer.html))
- Run the MV stage with `USE_EXTERNAL_BASEDATA`: it reads the InfDB through a separate read-only connection (see [MV grids](https://pylovo.readthedocs.io/en/latest/user_guide/mv_grids.html#mv-prerequisites))
- Import the eGon-data HV/MV substations in `pylovo-setup` and with `pylovo-import egon-substations`, so the MV stage finds the eTraGo bus of each substation; run `pylovo-setup` once after upgrading (see [MV grids](https://pylovo.readthedocs.io/en/latest/user_guide/mv_grids.html#mv-egon-data))
- Add `MV_OSM_COVERAGE` and `pylovo-mv generate --no-osm-coverage` to skip the OSM coverage report, which needs about three times the memory of the rest of an MV run (see [MV parameters](https://pylovo.readthedocs.io/en/latest/user_guide/mv_parameters.html))

### Changed
- Speed up the MV routing graph of large regions: the settlement polygons no longer use a query plan that grows with the square of the number of settlements (364 municipalities: build in 1.7 instead of 4.8 minutes)
- Speed up the MV ties and links of large regions: their street subgraph is found through the edge index, their path searches stop at the length limit, and the takeover check reads the district nets when it needs them; the cells of the district areas find their stations through an index
- Find the street edges of the MV ties and links with a union of the edges near the ring members and the attachment edges; the former OR made the planner rerun the search for every edge of a large graph
- Run the path searches of the MV ties and links on the part of the routing graph that their length limit can reach, with the same routes (Bavaria: links in 51 s instead of 17 minutes)
- Speed up the district areas of large MV regions: the cells are united per district before they are clipped to the region, which before copied the whole region into every cell row (106 GB of temporary files for Bavaria)
- Speed up the OSM coverage report of the MV stage: only the land-use polygons across the border of the region are clipped
- Read only the grids and nodes of a variant in `pylovo-mv couple`
- Keep the done supply areas and their status when the MV ties or links fail: the failure becomes a warning of the summary
- Count the land use and OSM MV lines in the fingerprint of an MV routing graph only within its extent, so OSM changes elsewhere keep the graph; existing graphs get new ids, so resuming an MV variant of 1.0.0 plans its districts again

### Removed
- Remove the statistics report and CSV files that the OpenDSS backend wrote into the working directory

### Fixed
- Show all MV lines, MV/LV substations and LV grid areas of large versions and MV runs in `pylovo-viewer`: above 60,000 features they load for the map view instead of an arbitrary first 60,000, and a map view with more asks you to zoom in instead of leaving out grids (see [Map viewer](https://pylovo.readthedocs.io/en/latest/user_guide/viewer.html))
- Fix the OpenDSS backend, e.g. bus names with blanks, parallel cables, the base frequency, constant-power loads and the LV reference voltage, so that its LV power flow matches pandapower's (see [Electrical backends](https://pylovo.readthedocs.io/en/latest/concepts/electrical_backends.html))
- Attach an MV node at a vertex of several equally near street edges always to the edge with the lowest id; before, the order of the index scan decided, so two runs on the same data could differ

## [1.0.0] - 2026-10-22
### Changed
- Split the generation configuration into `config_generation_lv.yaml` and `config_generation_mv.yaml`; an existing `config_generation.yaml` is still read
- Update the default generation configuration, e.g. a higher household peak load, revised settlement thresholds, transformer sizes and cable types, a planned utilization of 80 % for transformers and cables, and merging only small greenfield clusters; grids generated with the former defaults need a new `VERSION_ID` (see [Configuration](https://pylovo.readthedocs.io/en/latest/user_guide/configuration.html))
- Replace the voltage band `V_BAND_LOW`/`V_BAND_HIGH` by design voltage-drop limits for feeders (5 %) and service cables (1 %) that share the band from the LV busbar (0.96 p.u.) to the customer (0.90 p.u.) (see [Grid dimensioning](https://pylovo.readthedocs.io/en/latest/concepts/grid_dimensioning.html))
- Refine the grid topology: house cables end at the building facade instead of the centroid, and feeders leave the station busbar directly
- Introduce schema migrations: `pylovo-setup` updates an existing database in place and keeps its grids, so run it once after upgrading (see [Database setup](https://pylovo.readthedocs.io/en/latest/user_guide/database_setup.html))
- Require PostgreSQL 15 or newer with pgRouting 3.8 or newer, and Python 3.12 or 3.13
- Speed up grid generation about three times with less than half the peak memory
- Rework the exit codes and prompts of the CLI: `pylovo-generate` exits with 4 when postcodes failed, `pylovo-delete` asks for confirmation unless `--yes` is given, and `pylovo-classify` runs without questions when its options are given (see [Command-line reference](https://pylovo.readthedocs.io/en/latest/user_guide/cli.html))
- Make the classification reproducible with a seeded sample and stored clustering choices, and count parallel cables and transformer units in the clustering parameters; analyze existing grids again to update them (see [Classifying grids](https://pylovo.readthedocs.io/en/latest/user_guide/classification.html))
- Rewrite the documentation with a user guide, concepts, a CLI and API reference and executed tutorial notebooks (see [pylovo.readthedocs.io](https://pylovo.readthedocs.io/en/latest/))
- Mark the OpenDSS backend as experimental because of known defects; use pandapower for results (see [Electrical backends](https://pylovo.readthedocs.io/en/latest/concepts/electrical_backends.html))

### Fixed
- Fix buildings missing from the generated grids, e.g. at postcode borders where the InfDB files the connection line under the neighboring postcode
- Fix the reproducibility of brownfield grids and edge cases that failed a whole postcode, e.g. two transformers at one point
- Fix the grid analysis, which counted a station of parallel transformers with one unit and swapped longitude and latitude in the average house distance
- Fix the cable and transformer catalogue, which read the equipment data of all versions instead of the current `VERSION_ID`
- Fix the OSM transformer download and several CLI commands, e.g. `pylovo-import transformers-osm`, `pylovo-delete transformers` and `pylovo-export`

### Added
- Add a medium-voltage (MV) stage that builds MV grids on the generated LV grids, with HV/MV substations from OpenStreetMap, open rings, (n-1) checks and the CLI `pylovo-mv` (see [MV grids](https://pylovo.readthedocs.io/en/latest/user_guide/mv_grids.html))
- Add the map viewer `pylovo-viewer` to browse the HV, MV and LV grids of a database in the browser (see [Map viewer](https://pylovo.readthedocs.io/en/latest/user_guide/viewer.html))
- Add the HTTP API `pylovo-api` with a Docker image, e.g. to set up databases, generate grids, edit transformers and building loads and run power flows (see [HTTP API](https://pylovo.readthedocs.io/en/latest/user_guide/http_api.html))
- Add mixed-use buildings whose residential and non-residential components get their own loads
- Add LV generation options, e.g. cabinet networks, parallel standard feeder cables, routing across postcode borders, randomized greenfield station positions near the optimum and excluding buildings without address (see [Configuration](https://pylovo.readthedocs.io/en/latest/user_guide/configuration.html))
- Add the option to read the InfDB input data from another database or server (`USE_EXTERNAL_BASEDATA`)
- Add coverage accounting that records why an input building is in no grid (`pylovo-analyze --coverage`)
- Add unit and database tests with CI workflows on a synthetic test database

### Removed
- Remove `pylovo-validate` and the comparison with real grids, which is no longer part of the package
- Remove unused functions, constants and repository files, e.g. `DatabaseConstructor.reset_schema` (use `pylovo-setup reset`) and `data/grid_examples`

## [0.8.0] - 2026-08-18
### Changed
- Rework feeder routing and cable dimensioning to size uniform sections between hard nodes, support parallel cables, and select connection cables based on cost
- Improve grid topology generation with optional and configurable feeder splitting, reuse of sufficiently long upstream feeder prefixes, and aggregation of nearby connection points.
- Refine transformer clustering with deterministic settlement types, configurable cluster sizes, optional residential-only dimensioning, and merging of feasible neighboring greenfield clusters
- Enable flexible use of open and DSO transformer positions and align building preprocessing with the latest InfDB data structures
- Introduce configurable projected coordinate reference systems throughout transformer import, database processing, plotting, and electrical backends
- Restructure grid result storage and views for incremental postcode-level updates, feeder-section visualization, and complete generation configuration metadata

### Fixed
- Fix routing and geometry edge cases affecting building-to-way and transformer-to-way connections
- Fix duplicate and null brownfield transformer records when transformer input sources are disabled or combined
- Fix schema qualification across database queries and errors during parallel result-view creation
- Fix logging, deletion workflows, and compatibility issues with updated pandas, GeoPandas, pandapower, and scikit-learn dependencies
- Remove perfectly collinear classification features to avoid singular factor-analysis inputs
- Fix sqlparse security dependency

### Added
- Add a CLI workflow for importing DSO transformer positions from CSV with selectable source metadata
- Add sampling from existing postcodes and revised clustering inputs for classification workflows
- Add QGIS templates and visualization logic for parallel feeder sections and grid expansion costs

## [0.7.0] - 2026-05-11
### Changed
- Enable feeder branching with split-point reuse to model cable branching controlled by a minimum shared-prefix distance and a sizing workflow for upstream segments that considers the aggregated downstream load
- Rework transformer placement and cluster validation by adding a maximum greenfield transformer distance for transformer locations
- Introduce separated feeder and consumer connection cable sets
- Update the grid default generation configuration, e.g. a higher household peak load, revised settlement thresholds, and an updated brownfield assignment distance
- Reduce heavy and add optional dependencies 
- Update database table structures to include analysis parameters and powerflow status in grid results
- Adjust ways query to latest InfDB version v4.0.0
- Added metrics calculation functions for grid comparison

### Fixed
- Fix percentage incrementation in analysis
- Fix `.env.example` variable names and path examples for a clearer and more accurate environment setup
- Remove non-standard extra MV-LV line

### Added
- Add a municipal register import pipeline and region resolver that combine Regiostar and Gemeindeverzeichnis data for postcode- and municipality-based workflows
- Add installable CLI entry points for setup, generation, classification, analysis, import, export, deletion, and validation, together with a public `pylovo` package interface
- Add comprehensive plotting defaults to `config_analysis.yaml`
- Add convergence summary for grids

## [0.6.0] - 2026-01-07
### Changed
- Introduce electrical backend architecture that decouples grid construction algorithms from electrical simulation softwares such as pandapower
- Restructure config files and move all relevant user input to config files (e.g. equipment data, cable parameters, etc.)
- Add a more flexible transformer selection based on new settlement types
- Change analysis and plotting structure for easier integration into real data analysis.

### Fixed
- Fixed cable naming mismatch between config and pandapower standard types

### Added
- Add UI to add known transformer locations and/or capacities to be considered in the brownfield grid generation approach.
- Add automated target schema creation for easier setup

## [0.5.0] - 2025-08-11
### Changed
- Adjust grid generation to be completely deterministic
- Rework ways preprocessing to be more performand and transparent
- Update QGIS templates
- Update docs

### Added
- Implement connection to InfDB (https://github.com/tum-ens/InfDB) with new LOD2 building Zensus2022, street (basemap) and postcode data
- Add materialized views for better performance in QWC
- Add uv with pyproject.toml file as alternative for installation
- Add parallel grid generation depending on cores

## [0.4.0] - 2025-06-26
### Changed
- Restructure database communication: split pgReaderWriter into multiple database modules for better organization
- Restructure src and notebook directories
- Restructure executable functions in runme categories
- Adjust directory naming according to best practices
- Convert configs into yaml files
- Move parameter calculation class from classification into src to be used for validation

### Fixed
- Fix unique constraint issue when generating the same grid for different versions

### Added
- Add improved QGIS templates for local and remote visualization with QWC
- Add modules for analyzing grid independent from grid generation
- Add schema selection to config for more flexible database handling
- Add warnings when analyzing grids

## [0.3.0] - 2025-05-23
### Changed
- Refine codebase for better readability and maintainability
- Restructure database: reduce redundancies, add primary & foreign keys, add views, edit datatypes, rename columns
- Improve transformer queries and pipelines
- Improve pipelines of the classification module and enable its automated and stable use

## [0.2.1] - 2025-02-18
### Fixed
- Fix buggy issue templates for github
- Fix buildings directory structure for import

## [0.2.0] - 2025-01-14
### Added
- Add best practice files (issue/PR templates, changelog, contributing, release_procedure) to repository
- Add module to generate all grids for an AGS region
- Add function to get all grids from a PLZ
- Add flags for grid analysis and plotting as it is a time-consuming process

### Changed
- Update documentation for the tool's installation.
- Update the minimum required Python version to 3.12 (from 3.9) and related packages, dropping support for older versions.
- Restructure directories and naming
- Change installation with setup.py to more common approach with requirements.txt file
- Change from conda environment to python virtual environment
- Update QGIS project

### Fixed
- Fix crs problems in transformer import for fresh database
- Fix ssl-syscall database connection error due to large input files to read

### Removed
- The gui is not stable and has been removed (basic files as well as functions in pgReaderWriter)
- Unit test are not finished and have been removed from main (basic files as well as functions in pgReaderWriter)

## [0.1.0] Initial Release of the Pylovo Tool - 2024-04-12
### Added
- Project release.

---

# Guidelines for Updating the Changelog
## [Version X.X.X] - YYYY-MM-DD
### Added
- Description of newly implemented features or functions, with a reference to the issue or MR number if applicable (e.g., `#42`).

### Changed
- Description of changes or improvements made to existing functionality, where relevant.

### Fixed
- Explanation of bugs or issues that have been resolved.
  
### Deprecated
- Note any features that are marked for future removal.

### Removed
- List of any deprecated features that have been fully removed.

---

## Example Entries

- **Added**: `Added feature to analyze time-series data from smart meters. Closes #10.`
- **Changed**: `Refined energy demand forecast model for better accuracy.`
- **Fixed**: `Resolved error in database connection handling in simulation module.`
- **Deprecated**: `Marked support for legacy data formats as deprecated.`
- **Removed**: `Removed deprecated API endpoints no longer in use.`

---

## Versioning Guidelines

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html):
- **Major** (X): Significant changes, likely with breaking compatibility.
- **Minor** (Y): New features that are backward-compatible.
- **Patch** (Z): Bug fixes and minor improvements.

**Example Versions**:
- **[2.1.0]** for a backward-compatible new feature.
- **[2.0.1]** for a minor fix that doesn’t break existing functionality.

## Best Practices

1. **One Entry per Change**: Each update, bug fix, or new feature should have its own entry.
2. **Be Concise**: Keep descriptions brief and informative.
3. **Link Issues or MRs**: Where possible, reference related issues or merge requests for easy tracking.
4. **Date Each Release**: Add the release date in `YYYY-MM-DD` format for each version.
5. **Organize Unreleased Changes**: Document ongoing changes under the `[Unreleased]` section, which can be merged into the next release version.

