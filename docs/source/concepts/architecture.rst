Architecture
============

pylovo is organised in layers: command-line tools and the HTTP API call a few workflow classes,
which use the database access layer and the electrical backends. Configuration is read once by
:mod:`pylovo.config_loader` and shared by all layers.

.. figure:: /images/diagrams/architecture.*
   :alt: Layered architecture of pylovo: interfaces, workflows, engines, data access and storage
   :width: 100%

   Modules of the ``pylovo`` package and how they depend on each other (figure made with
   ``docs/scripts/plot_diagrams.py``).

Modules
-------

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Module
     - Responsibility
   * - :mod:`pylovo.cli`
     - Entry points of the ``pylovo-*`` commands; parse arguments and call the workflows.
   * - :mod:`pylovo.config_loader`
     - Reads ``.env`` and ``config/*.yaml`` and exposes all settings as module constants
       (``VERSION_ID``, ``TRANSFORMERS``, ``MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT``, ...).
   * - :mod:`pylovo.grid_generator`
     - :class:`~pylovo.grid_generator.GridGenerator`: the generation pipeline for one postcode,
       sequential and parallel runs for many postcodes, building clustering, transformer placement
       and persistence of each grid.
   * - :mod:`pylovo.feeder_planning`
     - Feeder topology and feeder cable sizing of one grid:
       :func:`~pylovo.feeder_planning.plan_feeder_branches` and
       :func:`~pylovo.feeder_planning.size_feeder_tree` (pure functions, no database writes).
   * - :mod:`pylovo.cable_installer`
     - :class:`~pylovo.cable_installer.CableInstaller`: creates buses, transformer, loads and lines
       of one grid in the electrical backend and selects feeder and service cables.
   * - :mod:`pylovo.utils`
     - Load model (:func:`~pylovo.utils.build_load_components`,
       :func:`~pylovo.utils.simultaneous_peak_load` and :class:`~pylovo.utils.CoincidentLoads`,
       :func:`~pylovo.utils.category_simultaneous_load`, :func:`~pylovo.utils.design_current_ka`),
       logging and small helpers.
   * - :mod:`pylovo.electrical_backend`
     - Backend interface :class:`~pylovo.electrical_backend.core.backend_base.IElectricalBackend`,
       component specifications (``BusSpec``, ``LineSpec``, ...) and the pandapower and OpenDSS
       implementations (:doc:`electrical_backends`).
   * - :mod:`pylovo.database`
     - :class:`~pylovo.database.database_client.DatabaseClient` (all SQL of the pipeline, composed of
       mixins), :class:`~pylovo.database.database_constructor.DatabaseConstructor` (setup) and the
       table definitions in :mod:`pylovo.database.config_table_structure`.
   * - :mod:`pylovo.infdb`
     - :class:`~pylovo.infdb.infdb_client.InfdbClient`: reads buildings, streets and postcodes from
       the InfDB schemas.
   * - :mod:`pylovo.data_import`
     - Transformer download and processing (OSM), DSO CSV import, municipal register, building
       shapefiles (file-based mode) and region resolution.
   * - :mod:`pylovo.mv_grid`
     - The MV stage (:doc:`mv_grids`): parameters (:mod:`~pylovo.mv_grid.options`,
       :mod:`~pylovo.mv_grid.parameters`), the stage driver :func:`~pylovo.mv_grid.stage.run_mv_stage`
       and the pure planning modules (substations, supply areas, routing graph, rings, solvers,
       topology, electrical checks, ties, coupling, metrics). The planner
       :func:`~pylovo.mv_grid.planner.plan_district` runs without a database.
   * - :mod:`pylovo.analysis`
     - :class:`~pylovo.analysis.parameter_calculation.ParameterCalculator` for postcode and
       per-grid key figures, power-flow helpers.
   * - :mod:`pylovo.plotting`
     - Grid plots (:mod:`pylovo.plotting.generation`), GIS export helpers
       (:mod:`pylovo.plotting.gis_preparation`) and MV grid plots (:mod:`pylovo.plotting.mv_grid`).
   * - ``ways_preprocessing_functions/``
     - SQL functions installed by the setup: segmenting intersecting streets and creating
       building and transformer connection lines.

Database access
---------------

:class:`~pylovo.database.database_client.DatabaseClient` holds one psycopg2 connection and one
SQLAlchemy engine with the search path ``pylovo, public``. Its methods are grouped in mixins:

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Mixin
     - Methods for
   * - :class:`~pylovo.database.preprocessing_mixin.PreprocessingMixin`
     - Version snapshot, equipment and consumer categories, postcode, buildings and loads,
       transformer candidates, street preprocessing, pgRouting topology and connection points.
   * - :class:`~pylovo.database.clustering_mixin.ClusteringMixin`
     - Connected components, k-means, routed distance matrices, load-constrained hierarchical
       clustering, brownfield assignment and greenfield transformer positions.
   * - :class:`~pylovo.database.grid_mixin.GridMixin`
     - Cable catalogue, node coordinates, routing paths, line records and the GIS helper geometries
       (split points, offset lines).
   * - :class:`~pylovo.database.analysis_mixin.AnalysisMixin`
     - Persisting grids (JSON and pandapower tables), reading nets, key-figure tables and
       GeoDataFrame queries.
   * - :class:`~pylovo.database.results_mixin.ResultsMixin`
     - Copying buildings and streets to the result tables (``save_tables``) and deleting postcodes,
       versions and transformers.
   * - :class:`~pylovo.database.transformer_ui_mixin.TransformerUiMixin`
     - The ``*_trafo_ui`` methods of the transformer editors (view, add, delete, set ratings).
   * - :class:`~pylovo.database.utils_mixin.UtilsMixin`
     - Temporary tables, materialised views, consumer categories and the municipal register.
   * - :class:`~pylovo.database.mv_graph_mixin.MvGraphMixin`
     - MV stage: build of the routing graph with settlement flags, attachment of points, district
       subgraphs and quality tables.
   * - :class:`~pylovo.database.mv_source_mixin.MvSourceMixin`
     - MV stage: OSM substations, eGon-data tables and MaStR units.
   * - :class:`~pylovo.database.mv_input_mixin.MvInputMixin`
     - MV stage: LV results of a version (stations, load statistics, MV customers, stored LV nets).
   * - :class:`~pylovo.database.mv_result_mixin.MvResultMixin`
     - MV stage: variants, locks, region and district results, readers, listing and deletion.

Control flow of ``pylovo-generate``
-----------------------------------

#. :mod:`pylovo.cli.generate` resolves the regions (:doc:`../user_guide/region_selection`) and
   creates a :class:`~pylovo.grid_generator.GridGenerator`, which opens a ``DatabaseClient``
   (and an ``InfdbClient`` in InfDB mode) and stores or checks the version snapshot.
#. For several postcodes, ``generate_grid_for_multiple_plz`` starts worker processes; each creates
   its own ``GridGenerator`` and database connections.
#. ``generate_grid_for_single_plz`` creates the temporary tables of the postcode and runs
   ``generate_grid``: ``prepare_*`` steps, ``apply_kmeans_clustering``,
   ``position_all_transformers`` and ``install_cables`` (:doc:`pipeline`).
#. ``install_cables`` builds every grid in a fresh electrical backend: the ``CableInstaller``
   creates buses, transformer and loads, :mod:`pylovo.feeder_planning` plans and sizes the feeder
   tree, the ``CableInstaller`` adds feeder and service lines, and ``save_net`` runs the
   power-flow check and writes the grid rows.
#. ``save_tables`` copies buildings and streets to the result tables, the transaction is committed,
   the optional analysis runs and the temporary tables are dropped.
#. With ``--mv`` (or ``MV_GRID_GENERATION``), :mod:`pylovo.cli.generate` finally calls
   :func:`~pylovo.mv_grid.stage.run_mv_stage` for the postcodes of the call. The stage plans every
   supply area in its own worker process with its own database connection (:doc:`mv_grids`).

Conventions
-----------

* **Configuration is frozen at import.** Settings are module constants; a changed YAML file takes
  effect in a new process.
* **Working directory.** ``config/``, ``data/``, ``log/`` and ``results/`` are resolved relative to
  the working directory; run the commands from the repository root.
* **Coordinates.** Stored geometries use ``TARGET_EPSG`` (default EPSG:25832, metres); bus and line
  coordinates inside the pandapower nets are WGS84 (EPSG:4326).
* **Identifiers.** A grid is ``(version_id, plz, kcid, bcid)`` and has a surrogate key
  ``grid_result_id``. Buses are named ``LVbus 1``, ``MVbus 1``, ``Connection Nodebus <vertex>`` and
  ``Consumer Nodebus <vertex>``, where ``<vertex>`` is the pgRouting vertex id; the transformer's
  own vertex has no connection node, it is ``LVbus 1``.
* **Logging.** All modules log through :func:`pylovo.utils.create_logger` to the console and to
  ``log/log.txt`` (``log/log_<plz>.txt`` for parallel workers).
