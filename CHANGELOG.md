
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
- `BROWNFIELD_NEAREST_ONLY`: a building joins only its nearest existing station; when that station is full, the building gets a greenfield station instead of joining a farther existing station with capacity left (off by default)
- `USE_MANUAL_TRANSFORMER_POSITIONS`: use manually placed transformer positions (GridPlanner UI) without the OSM and LoD2 candidates
- `pylovo-generate --parallel`; `PARALLEL` in `config_generation.yaml` is now the default for several regions
- `CABLE_PLANNING_UTILIZATION`: planned coincident current per cable ampacity for feeder and service cables, the cable counterpart of `TRANSFORMER_PLANNING_UTILIZATION`; a cable with design current *I* gets the smallest design rated above *I* / utilisation (voltage drops still at *I*); the shipped configuration plans at `0.8`, like the transformers (a maximum planned line utilisation of 80 %), and a configuration without the key keeps the full-ampacity sizing (`1.0`); its former name `FEEDER_PLANNING_UTILIZATION`, which applied to the feeders only, is still read; the snapshot records `feeder_planning_utilization` and `service_planning_utilization` (older versions need `1.0` for the service cables), so generating with `0.8` needs a new `VERSION_ID`
- `GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS`: with `MERGE_GREENFIELD_CLUSTERS`, merge a pair of neighbouring greenfield clusters only if the smaller one has at most this many loaded buildings, so small fragments join a neighbour while larger clusters stay as split; `null` (default) merges clusters of any size as before; the key is in the version snapshot (older versions need `null`)
- Station voltage of the validation power flow after Niederle et al. (2026): the LV busbar is at `LV_REFERENCE_VOLTAGE_PU` (0.96 p.u.) at the operating point, with a neutral transformer tap; the key is in the version snapshot (older versions need `null`)
- `GET /api/health` of `pylovo-api` (no database access: contract version `api`, pylovo version, git revision) and the OpenAPI contract snapshot `api/openapi.json` (`api/scripts/export_openapi.py`), checked by a test and on pull requests by `oasdiff breaking` against the base branch
- Documentation page on the GridPlanner browser UI (`user_guide/browser_ui`) with screenshots of the pylovo steps; the README and the documentation start page show a grid in the UI's 3D view
- `FEEDER_STANDARD_CABLE` and `MAX_PARALLEL_FEEDER_CABLES` (default 4): a standard feeder cable (Regelquerschnitt, e.g. NAYY 4x150) laid in parallel for the load and the voltage drop before larger cables from `FEEDER_CABLES` are used; `null` (default) keeps the smallest sufficient design; both are in the version snapshot (older versions need `null`)
- `HOUSE_CONNECTION` (`joint` | `cabinet`) and `CABINET_SPACING_M` (default 100): `cabinet` models cabinet networks (Kabelverteilertechnik): cabinets at the split points, the branch ends and at most `CABINET_SPACING_M` apart, feeders from cabinet to cabinet and every house with its own cable from its nearest cabinet; the cabinets are stored in `split_points` (`end_cabinet`, `added_cabinet`); `joint` (default) keeps the tee-joint connections; both are in the version snapshot (older versions need `joint`)
- MV stage `pylovo.mv_grid`: medium-voltage grids on stored LV versions, implemented from the published methods without Ding0 code. Every LV grid is one MV node at its greenfield or brownfield station, together with the MV customers. The HV/MV substations come from OpenStreetMap (filter after Hülk et al. 2017) and are matched to their 110 kV bus in the eTraGo grid model of eGon-data. Every node belongs to the substation nearest to it along the streets (supply areas); a substation above its (n-1) capacity keeps its load and gets more transformer units. The routing graph is built from the InfDB street graph with settlement flags and the MV overhead lines of OpenStreetMap (`INFDB_OSM_SCHEMA.infrastructure_line` with `tags`; `line` and `minor_line` in service with an MV voltage, untagged `minor_line` unless LV), which are split at street crossings and joined to the streets within `osm_line_snap_m` (100 m); routes and rings plan an OSM line at `osm_line_cost_factor` (0.7) times its length, and an MV line is overhead exactly where its route runs on one, else cable (`mixed` lines with length-weighted parameters). Open rings from the busbar: Clarke-Wright savings with local search, normally-open points at the voltage balance, rural members counting ½ against `MV_MAX_STATIONS_PER_RING` (`rural_ring_factor`), busbar legs along the streets with exits to every street vertex within `busbar_exit_radius_m` (200 m), rural stubs with a load-dependent depth on the true route length (at most 1 MVA; 4 km up to 0.5 MVA, 2 km at 1 MVA), satellites (rural stations outside settlements or alone in theirs) on stubs after the rings are solved, busbar radials, two-leg feeders for MV customers above 5.5 MW, normally-open ties between supply areas and normally-open links inside a supply area (`mv_line.role = 'link'`): between rings and from stub ends to another feeder (`link_stub_ends`), within a reach by the settlement type of the PLZ (`link_max_length_m`: 1,000 m rural, 750 m semi-urban, 500 m urban; a link takes the shorter reach of its ends). pandapower checks the peak load case and both (n-1) cases with a repair loop (larger conductors first, as Ding0's reinforcement, then ring splits and parallel cables), the HV/MV transformers are sized with a fixed OLTC tap, and every station gets a voltage budget with its MV/LV tap. Defaults: NA2XS2Y 1x240 RM/25 cables and 122-AL1/20-ST1A overhead lines, at most 20 ring members (40 in rural rings), and 10 kV for supply areas whose installed capacity lies mostly in municipalities with at least 5 MVA/km² installed MV/LV capacity, else 20 kV (`MV_NOMINAL_VOLTAGE_KV: auto`). MV customers below 200 kW own peak (after counting large halls as one storey) hang on the busbar of the nearest station within 1 km, and a station they overload gets a larger rating in the MV model. Documented in `concepts/mv_grids`, `user_guide/mv_grids` and `user_guide/mv_parameters`
- The MV stage stops before it starts when `USE_EXTERNAL_BASEDATA` points to another database: it reads the InfDB streets, OSM layers and eGon-data tables on the pylovo connection, so it needs them in `DBNAME` (`pylovo-generate --mv` stops before the LV grids)
- MV stage version 12: a stub tree adds the true street length of each extension to its depth; before, an extension whose OSM-discounted routing cost was already within the stub limit counted with that cost, so trees along OSM MV lines could grow past `stub_max_length_m`
- MV stage version 11: `MV_NOMINAL_VOLTAGE_KV: auto` decides by municipality: a supply area is 10 kV when at least half of its installed capacity lies in municipalities with at least `mv_10kv_min_density_mva_km2` (now 5 MVA/km², over the municipality's area in the planning region), since a city utility runs one voltage in its parks and outskirts too
- MV stage version 10: the OSM HV/MV substation filter keeps substations on thermal and heating plant sites (`plant:source` of a plant or generator within 100 m, or a heating plant name), where city utilities run their HV/MV substations, and counts a `substation=transmission` footprint without a voltage tag as HV
- OSM completeness report of the MV stage (`pylovo.mv_grid.osm_coverage`, after Babli et al. 2025): mapped MV/LV station sites and MV overhead lines per settlement and land-use class against the planned stations, in `mv_variant.summary['osm_coverage']` and the run summary
- Display geometry of the MV stage for maps (`pylovo.mv_grid.display`): where MV lines share a route, `mv_line.display_geom` shifts them into lanes 2 m apart (the band at most 12 m wide), each on the side where it leaves the bundle, and every line from a substation ends on its busbar circle (`mv_substation.display_geom`); `geom` and the lengths stay as they are. Stored at the end of every run and by `pylovo-mv display` for older variants. Migration `0010_mv_display_geometry` adds the two columns: run `pylovo-setup` once
- `MV_GRID_GENERATION` (default `False`, in no snapshot) and `pylovo-generate --mv`/`--no-mv`: run the MV stage after the LV grids of a call, for the configured MV variant
- `pylovo-mv` with the subcommands `generate`, `couple` (LV design-point check and merged MV+LV power flows), `list`, `delete`, `export`, `graph`, `display` and `boundary` (the HV boundary of a variant for eTraGo: per supplying HV/MV substation its eTraGo 110 kV bus, which is its eGon bus id, the load-case P and Q at the MV busbar and the HV/MV transformers; the stored net's HV bus carries the same `etrago_bus_id`, and OSM sites without an eGon match get the flag `no_egon_bus`). `--set KEY=VALUE` changes the `MV_*` keys per variant
- `MV_*` keys in `config_generation_mv.yaml`, stored per MV variant in `pylovo.mv_variant` with the resolved line and transformer types, not in the snapshot of `VERSION_ID`, so existing versions are not affected; `pylovo-mv generate --set` changes them per variant, the constants of the stage are changed in Python (`MvParameters.replace`)
- Tables `mv_variant`, `mv_substation`, `mv_grid`, `mv_node`, `mv_line`, `mv_ring`, the routing graph tables `mv_graph`, `mv_graph_vertex`, `mv_graph_edge`, `mv_graph_settlement` and the view `mv_variant_stats`
- Migration `0009_mv_grids`: creates the MV tables and `mv_customers` on schemas set up before. Run `pylovo-setup` once (it keeps all grids). Until then `pylovo-generate` stops with `PyLovo schema has pending migrations (0009_mv_grids)`
- `.env` `INFDB_OSM_SCHEMA` (default `osm`): the OSM layers of InfDB for the HV/MV substations, the MV overhead lines and the built-up land use
- `pylovo.plotting.mv_grid` (maps and statistics of MV grids) and the notebooks `notebook_tutorials/mv_grids`
- `pylovo.analysis.mv_validation`: research tools for MV grids outside the MV stage: published ranges of MV grid metrics with traffic lights, readers of Ding0 CSV grids and pandapower nets into the generic tables of `pylovo.mv_grid.metrics`, Wasserstein distances and ring-membership agreement between variants, and the capacitive earth-fault current
- `USE_EXTERNAL_BASEDATA` with the optional `INFDB_DBNAME`, `INFDB_USER`, `INFDB_HOST`, `INFDB_PORT` and `INFDB_PASSWORD`: read the InfDB source data (buildings, ways, postcodes) from another database or server than the pylovo schema; unset values fall back to the pylovo connection
- `ROUTE_ACROSS_POSTCODE_BORDERS`: a PLZ also routes over the InfDB streets of its neighboring PLZ within `MAX_BROWNFIELD_TRAFO_DISTANCE` / 2 + 100 m outside its polygon (buildings and stations stay per PLZ; only the ways touching the PLZ are saved); the shipped configuration routes across the borders, a configuration without the key keeps the PLZ graph, and file input (no InfDB) has no neighboring streets; the switch and the width are in the version snapshot (older versions need `False`)
- `pylovo.building_exclusion` (migration `0011_building_exclusion`, run `pylovo-setup`): every loaded building of a generated PLZ that is in no grid, with the step that left it out
- `pylovo-analyze --coverage` (every source building generated, excluded with a reason, or missing; exit 3 when buildings are missing) and `--border-diagnostics` (import-scope share and street context per PLZ, distances of every grid to generated, open and outer PLZ borders and to the scope edge); `--plz` takes several PLZ
- CI workflow `tests.yml` (unit tests with the `api` and `plots` extras); pytest collects `tests` and `api/tests` by default
- Unit tests of the classification, the CLI entry points, the packaging, the data directory and the MV, API and LV fixes of the release review
- User guide page on the classification (`user_guide/classification`)

### Changed
- `config/config_generation.yaml` is now `config/config_generation_lv.yaml` (the LV grids), and the MV stage has its own `config/config_generation_mv.yaml` in the same directory. A `config_generation.yaml` is still read in place of `config_generation_lv.yaml`, with its `MV_*` keys; an `MV_*` key in both files needs the same value. `pylovo-api` edits whichever of the two LV files exists
- The MV overview shows seven figures by default, with optional details, explicit planning scope and separate generation, electrical and LV-coupling status
- The shipped `MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW` is `200` kW, the eGon-data boundary between the MV/LV station level and the MV level (a5 used `100`): fewer commercial and public loads are left out of the LV grids; the key is in the version snapshot, so versions generated with 100 keep it and generating with 200 needs a new `VERSION_ID`
- The shipped `config_generation.yaml` uses the settings of the paper's vanilla candidate a5: station sizes `1: [250, 400]`, `2: [400, 630]`, `3: [400, 630]` kVA, `FEEDER_SPLIT_MAX_CURRENT_KA` `0.4`, `GREENFIELD_TRAFO_POSITION_TOLERANCE` `0.3`, `MERGE_GREENFIELD_CLUSTERS` with `[250, 400, 630]` kVA and at most `4` buildings, and buildings of unknown use without load (`Unknown` at 0 W/m²); versions generated with the former defaults need a new `VERSION_ID`
- `GridGenerator.generate_grid_for_single_plz` returns the outcome of the PLZ (`generated`, `exists`, `empty`, `failed`) and `generate_grid_for_multiple_plz` returns the outcomes of all PLZ (a failing PLZ is still logged and skipped; the log lines are unchanged)
- The OSM transformer download also fetches pole-mounted stations (`power=pole` with a `transformer` tag)
- `InfdbClient` sends no startup `options` (PgBouncer rejects them) and schema-qualifies its tables; buildings and `classify_building_use` are read from `INFDB_SOURCE_SCHEMA` (default now `basedata`) instead of a fixed `basedata`; every query runs in its own read-only transaction; the way id offset (largest `way_id` of `ways_result`) is taken from the pylovo database instead of the InfDB connection
- Feeder cables: among the designs with the fewest parallel cables the cheapest cable is taken (then the smaller cross-section), as for the service cables; with the shipped catalogue, whose cost rises with the cross-section, the choices are the same
- The cable and transformer catalogue is read from the `equipment_data` rows of the current `VERSION_ID` only; before, the rows of all versions were read, so a cable's values could come from another version and every transformer rating was listed once per version
- `equipment_data` keeps both roles of a cable listed in both cable pools (`grid_role` `feeder,consumer_connection`; before, the service role overwrote the feeder role) and refuses a cable listed twice with different values
- Split points and the overlap helpers recognise feeder lines by their `feeder_section_id` instead of the configured feeder cable names, so a service cable of a feeder cable type no longer counts as an outgoing feeder
- `lines_result_cache` keeps one row per line: the feeder section merge, which matched no line once the feeder cables were also listed in `CONSUMER_CONNECTION_CABLES` (September 2026), is removed
- The last remaining connection node of a grid is planned like any other branch (it joins an existing feeder beyond `MIN_SHARED_PREFIX_LENGTH_M`) instead of always getting its own feeder from the transformer; this changes generated grids, so regenerate with a new `VERSION_ID`
- With `HOUSE_CONNECTION: cabinet`, buildings planned at one connection point (e.g. merged by `AGGREGATE_NEARBY_CONNECTION_POINTS`) share one house-connection cable from their cabinet to a joint and get their own cable from there
- House cables end at the house connection on the facade (where the building's connection line crosses the footprint outline) instead of the building centroid, about 6.5 m shorter on average; the consumer bus is drawn there. This changes generated grids, so regenerate with a new `VERSION_ID`
- With `MERGE_GREENFIELD_CLUSTERS` and existing stations, a greenfield cluster that is still at most `GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS` buildings after the merge joins the nearest existing station within `MAX_BROWNFIELD_TRAFO_DISTANCE`
- The brownfield step assigns a station only to buildings of its own k-means cluster: buildings of another cluster at a shared connection point (e.g. merged by `AGGREGATE_NEARBY_CONNECTION_POINTS`) were moved into a grid that was never built and went missing
- A single building left over after the brownfield step gets its own grid instead of being dropped; a k-means cluster with existing stations but no consumers no longer fails the brownfield cost query
- `pylovo-setup` creates or migrates the schema in place (migrations recorded in `pylovo.schema_migrations`) and imports every empty reference table after checking its input data; `pylovo-setup reset --database NAME` rebuilds the schema: it refuses while other schemas depend on it, keeps the previous schema as `pylovo_backup_<time>` until the rebuild is complete and restores it if the rebuild fails or is cancelled. Extensions are created in `public`
- `DatabaseClient` is composed of dedicated mixins (`ResultsMixin`, `TransformerUiMixin` added); SQL values are parameterised
- `utils.simultaneousPeakLoad` / `oneSimultaneousLoad` renamed to `simultaneous_peak_load` / `category_simultaneous_load`
- Documentation rewritten (furo, autoapi, reproducible figures); CI builds the docs and publishes them to GitHub Pages
- Tutorial notebooks rewired to the current package and executed on an OSM demo region
- `pylovo-generate` requires exactly one of `--plz`/`--ags`; the Flask transformer map binds `127.0.0.1` and kills port owners only with `--auto-cleanup`
- Analysis counts transformer stations with their full rating: `plz_parameters` keys, `clustering_parameters.transformer_mva` and the comparison metric use `sn_mva x parallel` (an 800 kVA station of two 400 kVA units was counted as 400)
- The version snapshot records `MERGE_GREENFIELD_CLUSTERS`, `GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA` and `USE_MANUAL_TRANSFORMER_POSITIONS`; older snapshots without them stay usable
- A transformer rating only sizes a station when the candidate's source is enabled; among several candidates the cluster's own station wins
- `grid_result.transformer_equipment_name` is filled during generation, and `pylovo-setup` backfills it for existing versions where the station rating maps to one catalog unit (standard 100 to 1260 kVA stations). The equipment columns of `transformer_positions_with_grid` (`s_max_kva`, `cost_eur`, ...) describe one unit; the new column `transformer_units` gives the units per station and `transformer_rated_power` stays the station rating (an 800 kVA station shows `s_max_kva` 400 and `transformer_units` 2)
- The transformer's street vertex is the station busbar `LVbus 1`: cables starting there leave the busbar directly, without the 1 m link line (one bus and one line fewer per grid; voltage drops change by less than 0.03 percentage points)
- Grid generation is about three times faster with the same results (PLZ 91301: 159 s -> 53 s, peak memory 0.94 -> 0.42 GB): the `ways_tem` source/target indexes are usable during generation, coincident loads come from precomputed load components (`utils.CoincidentLoads`), buses, loads and lines are created with pandapower's batch functions (`PandapowerBackend.batch`), empty pandapower networks are copied from a cached template, the kcid distance matrix is computed with scipy instead of transferring the `pgr_dijkstraCostMatrix` rows, the per-transformer analysis deserializes only the tables it reads, row inserts are sent in pages, and the connection-point aggregation (`AGGREGATE_NEARBY_CONNECTION_POINTS`) no longer re-runs a subquery per building (minutes -> seconds per PLZ). The result tables are unchanged; in the stored network JSON pylovo's extra line and load columns now have one dtype in every grid (before, the first line of a grid decided it)
- `N_JOBS_PERCENT` defaults to 50 (was 20): a worker needs about 0.4 GB of memory
- Design voltage-drop limits follow the band split of the station voltage: `MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT` 5 (was 8) and `MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT` 1 (was 3) share the 6 % between the LV busbar at `LV_REFERENCE_VOLTAGE_PU` (0.96 p.u.) and `MIN_VM_PU` (0.90 p.u.); generating with them needs a new `VERSION_ID`
- The LV stage stores the buildings with an MV-direct non-residential component in `pylovo.mv_customers`, the input of the MV stage. The LV results are unchanged. Versions generated before have no rows, and the MV stage then uses only the mixed MV-direct buildings of `buildings_result`
- `pylovo-delete networks --plz PLZ --version VERSION` also deletes the MV variants of the version that contain the postcode, because an MV grid spans several postcodes
- `version_snapshot.compare_snapshots` takes an optional `added_later` mapping. The MV variant snapshots use `MV_ADDED_LATER`
- `PYLOVO_DATA_DIR` applies to all input data (transformer data, municipal register, postcode CSV); `requires-python` is `>=3.12,<3.14`; shapely, networkx and tqdm (and starlette, pydantic in `api`) are declared, `xlrd` is removed, `contextily` needs 1.7.0; the SQL files of the ways preprocessing are part of the wheel and sdist
- `SWF_BUS_TYPE_CONFIG` is now `DSO_BUS_TYPE_CONFIG` (bus naming of a DSO grid export)
- `VERSION_ID` is read as text and must be 1 to 10 characters of `[A-Za-z0-9_.-]`; the generation config is checked at load (`sim_factor` in 0..1, cable ampacities above 0, `TRANSFORMER_MAPPING` ratings in `TRANSFORMERS`); a lone former `config_generation.yaml`, or one that hides the current files, gives a warning
- `pylovo-analyze` exits 1 for postcodes without grids and reports already analysed ones as skipped; `pylovo-export` reports postcodes without grids and exits 1 when nothing was exported; both warn about options the chosen mode ignores
- `pylovo-classify` asks for two cluster numbers (k-means, GMM; `N_CLUSTERS_KMEDOID` is removed), accepts y/yes/n/no, validates parameter names, checks the classification version before the questions and stops with a hint when the `plots` extra is missing; the version is registered only after the sample is drawn
- `pylovo-delete classification --version` takes the integer classification id, refreshes `transformer_classified_with_grid` and rejects unknown ids
- DSO CSV import: ids are read as text (leading zeros kept); blank or duplicate ids and unusable ratings are refused with their row numbers; `--replace-source` matches the exact prefix; the API preview uses the same parser
- OSM transformer download: an Overpass answer with a `remark` (for example a timeout) is an error and is not cached; HTTP 429 and 504 are retried
- MV stage: the staleness fingerprint counts the active (not undone) load edits, so variants built before show stale once; an interrupted or failed run is stored as `failed`; deletes are refused while a variant runs; every `pylovo-mv` subcommand that reads the MV tables checks for pending migrations; `pylovo-generate --mv` checks the MV parameters before the LV grids; `pylovo-mv display` reports lanes and busbar circles
- `pylovo-api`: generate and regenerate jobs pass `--no-mv` (the API has no MV stage); the on-demand power flow answers 503 when no slot is free within 30 s and streams the stored net; load edits accept only whole numbers of households and answer 409 on a lock timeout; config files that are not a YAML mapping answer 422
- Only the pandas connection warning is hidden; pylovo's own warnings (e.g. K-means clustering issues) are shown again
- Migration `0012_indexes`: indexes for the `way_id` lookup, the per-grid bus foreign keys of the pandapower tables and `postcode_result_plz`; redundant indexes and the leftover v0.8.0 function `generate_building_way_connection_candidates_infdb()` are dropped. Run `pylovo-setup` once
- `pylovo-setup` refuses PostgreSQL servers older than 15 before changing anything; a migration that meets violating rows stops with a message naming the migration
- `pylovo-generate` exits with 4 when the run finished but postcodes failed or got no grids (before: 0) and names them in one line; with `--mv` the MV stage runs only for the postcodes with LV grids; `pylovo-api` counts such a generate job as succeeded

### Deprecated
- `pylovo-import transformers-ui` (Flask transformer map): use the transformer editor of the GridPlanner UI (on `pylovo-api`); Flask moved to the extra `legacy-ui`

### Removed
- `pylovo-validate` (`compare-grids`), `pylovo.analysis.synthetic_metric_export` and `validations/grid_comparison`, superseded by `validations_new`
- The double-transformer comparison in the greenfield clustering (it could never win), `pylovo.analysis.powerflow_calculation` and `ParameterCalculator.count_consumers` (no callers)
- Unused configuration constants (`COLORS`, `PLOT_DEFAULTS`, `REGIOSTAR7_DICT`, `TUMPalette2`, `TUMPalette3`), `GRACEFUL_SHUTDOWN_TIMEOUT`, `utils.reset_log_directory`, `drop_orphaned_plz_temp_tables`, the `double_trans` parameter and the leftovers of the removed MV options
- The orphan module `pylovo.analysis.validation_helpers` (Excel export) and with it the direct dependencies `tqdm` and `xlsxwriter`; `MvInputMixin.lost_station_overlap_customers` and `TransformerUiMixin.delete_transformer_position_trafo_ui` (no callers); unused module constants and parameters of internal functions
- The OpenDSS backend (`ELECTRICAL_BACKEND: opendss`) and the equipment classes only it used: `altdss` was never installable through an extra, bus names with blanks gave an all-zero power flow, parallel cables were ignored and its output could not be read by the analysis, the API or the MV stage. `ELECTRICAL_BACKEND` keeps the value `pandapower`; `register_backend` stays the extension point

### Fixed
- MV circuit inventory counts each installed parallel circuit; route, ring and feeder distances remain geometric lengths
- MV/LV coupling includes station-busbar customer loads in its design-point checks and reports incomplete checks as partial
- MV maps accept string node identifiers, and ring labels remain valid after ring identifiers develop gaps
- pandapower LV lines get a minimum length of 1 m: buses at one point gave lines of a few micrometres whose power flow did not converge (2 of 4,589 grids of a test version)
- Overpass and OSM tile requests send an identifying User-Agent (Overpass answered HTTP 406)
- `pylovo-import transformers-osm` (called a non-existent method; failed for small areas), `pylovo-delete transformers` (TRUNCATE rejected by a foreign key), `pylovo-export` with kcid/bcid 0 or negative ids, `pylovo-validate --help`, `pylovo-classify --help`
- int16 overflow of vertex ids in brownfield assignment; aborted transactions in several database helpers
- On-map plots (pandapower lon/lat check), several network plots, radar plot for 7 clusters
- `pylovo-classify` used the clustering settings of the previous run
- Transformer candidates with an empty `type` (e.g. manual positions without a comment) were never used as positions
- `pylovo-generate --parallel` logged a postcode that its worker returned as failed as completed
- Every postcode start ran `ALTER TABLE pylovo.version` (exclusive lock that serialised parallel workers; failed for roles that do not own the table)
- A failing `pylovo-generate` or `pylovo-analyze` deleted postcodes from the `sample_set` of the configured classification version
- Classification: a GMM representative grid could lie in another cluster and the renumbering then lost a cluster id; the clustering depended on the database row order; `get_network_info_for_plotting` passed numpy integers to psycopg2 (notebooks 2_0 and 3_0)
- The per-postcode analysis aggregated the whole pandapower tables
- MV stage: a failed extension of a variant could be resumed as current with its old region; a failed coupling kept the old coupled values
- `pylovo-api`: a job whose command could not start stayed running and kept the writer lock; a job cancelled before its process existed still ran; an SSE `Last-Event-ID` with non-ASCII digits gave 500; `##` section comments leaked into the config form help
- Docs: PDF build (missing LaTeX characters), schema reference and diagram show the migrated constraints, CLI reference, installation requirements (PostgreSQL 15, pgRouting 3.8, Python 3.12/3.13), CONTRIBUTING for GitHub
- Coverage accounting reports exclusion rows without a source building as `not_in_source`; the border diagnostics count only the ring part of neighbouring streets, write `null` instead of `NaN` and stop with a message for a PLZ without polygon
- `register_cable_types` of the pandapower backend accepts cable names without a trailing cross-section; the LV plot functions return their figures without `plt.show()`, and `plot_load_and_voltage_distribution(ax=)` takes two axes
- MV notebooks default to the configured database and use the `python3` kernel; docstrings no longer cite unpublished design notes
- `pylovo-api` with `USE_EXTERNAL_BASEDATA`: the region input check, `/api/status` and the LoD2 view read the InfDB through the `INFDB_*` connection; before, every postcode was reported as blocked because the InfDB schemas were looked up in the pylovo database
- The average house distance of the grid analysis (`house_distance_km`, a default clustering parameter) passed (lon, lat) to the haversine distance, which expects (lat, lon): distances along a parallel were 1/cos(latitude) too long (about 1.5 at 48° N). Analyse grids again to update `clustering_parameters`
- MV repair loop (MV stage version 13): a ring split copied parallel cables and larger conductors onto unrelated lines that shared a trench piece or a busbar end with a repaired line; the split capped its rings by the ring members only and left their stubs without reserve; open points were chosen with the base conductor instead of the upgraded one; a station rating outside pylovo's lists was split into 630 kVA units in `couple`. Only variants with repairs change; build older variants again with `--replace` or under a new id
- LV generation is reproducible: the existing transformers of a k-means cluster, their connection lines and the brownfield fill are processed in vertex order, so brownfield grids keep their `bcid` and `way_id` from run to run (before, the negative `bcid` were assigned in the physical row order); ties of routed distances and of the connection point of a building are broken by vertex id; the `plz_parameters` lists follow the grid order and `cable_length` is summed in a fixed order. With the default configuration the grids are unchanged; brownfield grids can differ from earlier versions in tie cases, so use a new `VERSION_ID` for comparisons
- Two transformers at one point gave two brownfield grids and a failed postcode (`UNIQUE(osm_id, version_id)`); a station loaded exactly to the largest rating raised `IndexError`; street segments that collapse in the 1 µm snap are deleted before the routing topology; a k-means cluster whose dimensioning is skipped no longer blocks the following clusters



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

