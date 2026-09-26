
# Changelog

All notable changes to this project will be documented in this file. 
See below for the format and guidelines for updating the changelog.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]
### Added
- HTTP API `pylovo-api` (`api/`, extra `api`, image `ghcr.io/tum-ens/pylovo`) for the GridPlanner browser UI: database setup, region input check, transformer editing and imports, configuration editing with backups, generation, analysis and export jobs with live logs, statistics, grid details, an on-demand power flow, diagnostics and load editing
- `pylovo.load_editing` and the audit table `pylovo.load_edit`: edit the building loads of stored grids
- `pylovo.feeder_planning`: feeder branch planning and feeder cable sizing as a separate module
- Optional extras `notebooks`, `api` and `legacy-ui`; `igraph` in `plots`
- `USE_MANUAL_TRANSFORMER_POSITIONS`: use manually placed transformer positions (GridPlanner UI) without the OSM and LoD2 candidates
- `pylovo-generate --parallel`; `PARALLEL` in `config_generation.yaml` is now the default for several regions
- `GET /api/health` of `pylovo-api` (no database access: contract version `api`, pylovo version, git revision) and the OpenAPI contract snapshot `api/openapi.json` (`api/scripts/export_openapi.py`), checked by a test and on pull requests by `oasdiff breaking` against the base branch

### Changed
- `pylovo-setup` has `--help`, asks to type the database name before dropping the schema (`--yes` for scripts), creates extensions in `public` and refuses to drop the schema while PostGIS lives in it
- `DatabaseClient` is composed of dedicated mixins (`ResultsMixin`, `TransformerUiMixin` added); SQL values are parameterised
- `utils.simultaneousPeakLoad` / `oneSimultaneousLoad` renamed to `simultaneous_peak_load` / `category_simultaneous_load`
- Documentation rewritten (furo, autoapi, reproducible figures); CI builds the docs and publishes them to GitHub Pages
- Tutorial notebooks rewired to the current package and executed on an OSM demo region
- `pylovo-generate` requires exactly one of `--plz`/`--ags`; the Flask transformer map binds `127.0.0.1` and kills port owners only with `--auto-cleanup`
- Analysis counts transformer stations with their full rating: `plz_parameters` keys, `clustering_parameters.transformer_mva` and the comparison metric use `sn_mva x parallel` (an 800 kVA station of two 400 kVA units was counted as 400)
- The version snapshot records `MERGE_GREENFIELD_CLUSTERS`, `GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA` and `USE_MANUAL_TRANSFORMER_POSITIONS`; older snapshots without them stay usable
- A transformer rating only sizes a station when the candidate's source is enabled; among several candidates the cluster's own station wins
- The transformer's street vertex is the station busbar `LVbus 1`: cables starting there leave the busbar directly, without the 1 m link line (one bus and one line fewer per grid; voltage drops change by less than 0.03 percentage points)

### Deprecated
- `pylovo-import transformers-ui` (Flask transformer map): use the transformer editor of the GridPlanner UI (on `pylovo-api`); Flask moved to the extra `legacy-ui`

### Removed
- `pylovo-validate` (`compare-grids`), `pylovo.analysis.synthetic_metric_export` and `validations/grid_comparison`, superseded by `validations_new`
- The double-transformer comparison in the greenfield clustering (it could never win), `pylovo.analysis.powerflow_calculation` and `ParameterCalculator.count_consumers` (no callers)

### Fixed
- Overpass and OSM tile requests send an identifying User-Agent (Overpass answered HTTP 406)
- `pylovo-import transformers-osm` (called a non-existent method; failed for small areas), `pylovo-delete transformers` (TRUNCATE rejected by a foreign key), `pylovo-export` with kcid/bcid 0 or negative ids, `pylovo-validate --help`, `pylovo-classify --help`
- int16 overflow of vertex ids in brownfield assignment; aborted transactions in several database helpers
- On-map plots (pandapower lon/lat check), several network plots, radar plot for 7 clusters
- `pylovo-classify` used the clustering settings of the previous run
- Transformer candidates with an empty `type` (e.g. manual positions without a comment) were never used as positions
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

