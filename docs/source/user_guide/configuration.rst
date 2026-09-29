Configuration
=============

pylovo is configured with two kinds of files:

* ``.env`` in the repository root -- database connection, input mode and coordinate system;
* ``config/*.yaml`` -- all grid generation parameters (``config_generation.yaml``) and the
  post-processing settings (``config_analysis.yaml``).

Both are read once, when :mod:`pylovo.config_loader` is imported. A running process does not
see later edits; every ``pylovo-*`` command reads the files anew when it starts.

.. _env-variables:

Environment variables (``.env``)
--------------------------------

``.env`` is loaded with ``load_dotenv(find_dotenv(), override=True)`` before the YAML files: the
first ``.env`` in the directories above the installed ``pylovo`` package (for a source checkout:
the repository root) is used -- in an interactive session or a notebook the search starts in the
current working directory instead -- and its values **override** environment variables with the
same name.

.. list-table::
   :header-rows: 1
   :widths: 26 14 60
   :class: fixed-table

   * - Variable
     - Default
     - Meaning
   * - ``DBNAME``
     - required
     - Database that holds the schema ``pylovo`` (with InfDB: the InfDB database).
   * - ``DBUSER``
     - required
     - Database user.
   * - ``HOST``
     - required
     - Database host.
   * - ``PORT``
     - required
     - Database port.
   * - ``PASSWORD``
     - required
     - Password of ``DBUSER``.
   * - ``USE_INFDB``
     - ``True``
     - ``True``/``1``/``yes``: read buildings, streets and postcodes from the InfDB schemas in the
       same database. Anything else: file-based mode (:ref:`file-based-input`).
   * - ``INFDB_SOURCE_SCHEMA``
     - ``pylovo_input``
     - InfDB schema with the street tables ``ways_per_connection`` and ``connection_lines``.
       Set it to ``basedata`` for current InfDB releases; the building query always reads
       ``basedata.buildings``.
   * - ``INFDB_OPENDATA_SCHEMA``
     - ``opendata``
     - InfDB schema with ``postcodes_germany``.
   * - ``TARGET_EPSG``
     - ``25832``
     - Projected CRS (metres) of all geometries stored by pylovo. Choose it before the first
       ``pylovo-setup``; the table definitions use it.
   * - ``PYLOVO_ROOT``
     - --
     - Optional project root for installations outside a checkout: ``$PYLOVO_ROOT/config`` is
       searched for configuration files and ``$PYLOVO_ROOT/data`` is used as data directory.
   * - ``PYLOVO_DATA_DIR``
     - ``./data``
     - Optional data directory for building shapefiles and the street SQL dump.
   * - ``PYLOVO_CONFIG_DIR``
     - ``~/.config/pylovo``
     - Optional directory with the YAML files (see :ref:`config-file-lookup`).

.. _config-file-lookup:

Where the YAML files are found
------------------------------

Each YAML file is taken from the first of these locations that contains it:

#. ``./config/`` in the current working directory,
#. ``$PYLOVO_ROOT/config/``,
#. ``$PYLOVO_CONFIG_DIR`` if set; otherwise ``~/.config/pylovo/`` (Linux/macOS) and then
   ``./.pylovo/``,
#. ``config/`` of the source checkout that contains the package.

All four files ``config_generation.yaml``, ``config_analysis.yaml``,
``config_classification.yaml`` and ``config_clustering.yaml`` must exist. The last two belong to
the classification module and do not influence grid generation.

.. _versions:

Versions and the parameter snapshot
-----------------------------------

Every generated grid is stored under ``VERSION_ID``. When a version is used for the first time,
pylovo stores a snapshot of all generation parameters in ``pylovo.version.generation_parameters``.
Every later run with the same ``VERSION_ID`` compares the current configuration with that
snapshot and stops with

.. code-block:: text

   ValueError: Generation parameters differ from the stored snapshot for version 1.
   Increment VERSION_ID before generating grids with the changed configuration.

if anything differs. The column **Snapshot** in the tables below marks the keys that are part of
the snapshot. ``VERSION_ID`` is stored as ``varchar(10)``, so use at most 10 characters.

A postcode that already has grids in the active version is skipped
(``Grid for the postcode area ... has already been generated``). Delete the old grids first
(:doc:`cli`, ``pylovo-delete``) or use a new version.

``config_generation.yaml``
--------------------------

Execution
~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 33 12 41 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``VERSION_ID``
     - ``"1"``
     - Version of all grids written by this configuration.
     - key
   * - ``VERSION_COMMENT``
     - ``default``
     - Description stored with a new version.
     -
   * - ``PARALLEL``
     - ``True``
     - Generate several postcodes in parallel worker processes (``N_JOBS_PERCENT``); the default of
       ``pylovo-generate``, overridden by ``--parallel`` or ``--no-parallel``. Does not change results.
     -
   * - ``N_JOBS_PERCENT``
     - ``50``
     - Share of CPU cores used as worker processes for multi-postcode runs
       (at least one worker; about 0.4 GB of memory per worker).
     -
   * - ``ANALYZE_GRIDS``
     - ``True``
     - Compute the postcode key figures (``plz_parameters``) right after each postcode.
     -
   * - ``LOG_LEVEL``
     - ``INFO``
     - Level of all pylovo loggers (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
     -
   * - ``SAVE_GRID_FOLDER``
     - ``False``
     - Also write each pandapower net as JSON file to
       ``<RESULT_DIR>/grids/version_<VERSION_ID>/<plz>/kcid<k>bcid<b>.json``.
     -
   * - ``RESULT_DIR``
     - ``results``
     - Output directory, relative to the working directory.
     -
   * - ``GRACEFUL_SHUTDOWN_TIMEOUT``
     - not set (5)
     - Optional: seconds to wait for running workers after :kbd:`Ctrl+C` in a parallel run.
     -

Loads and general grid settings
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 33 12 41 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``ELECTRICAL_BACKEND``
     - ``pandapower``
     - Electrical model used to build and solve the grids: ``pandapower`` or ``opendss``
       (work in progress, :doc:`../concepts/electrical_backends`).
     - ✓
   * - ``RESIDENTIAL_ONLY_GENERATION``
     - ``False``
     - Keep only residential buildings and components; non-residential loads are ignored.
     - ✓
   * - ``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS``
     - ``True``
     - InfDB mode: skip buildings without street and house number (sheds, garages, barns).
     - ✓
   * - ``PEAK_LOAD_HOUSEHOLD``
     - ``16.825``
     - Installed peak load per household in kW. The residential category refers to it with
       ``peak_load: PEAK_LOAD_HOUSEHOLD``.
     - ✓
   * - ``DEFAULT_POWER_FACTOR``
     - ``0.95``
     - cos φ of all loads: design currents, reactive power of the snapshot loads and voltage
       drops.
     - ✓
   * - ``CONSUMER_CATEGORIES``
     - 3 rows
     - Electrical load categories, see below.
     - ✓
   * - ``VN``
     - ``400``
     - Nominal LV voltage in V.
     - ✓
   * - ``MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT``
     - ``5``
     - Planning limit of the approximate voltage drop from the transformer LV bus to any
       service connection point; feeder conductors are upsized to meet it. With the service
       limit it shares the 6 % between the LV busbar at ``LV_REFERENCE_VOLTAGE_PU`` (0.96 p.u.)
       and ``MIN_VM_PU`` (0.90 p.u.).
     - ✓
   * - ``MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT``
     - ``1``
     - Limit of the voltage drop along a service cable at its building-local design load (the
       rest of the 6 %).
     - ✓
   * - ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``
     - ``100``
     - Commercial or public load components above this peak (kW) are assumed to have their own
       MV connection and are left out of the LV grid.
     - ✓
   * - ``FEEDER_SPLIT_MAX_CURRENT_KA``
     - ``0.850``
     - Topology parameter: maximum coincident current (kA) while connection points are grouped
       into one planned branch. Not a cable limit.
     - ✓
   * - ``MIN_SHARED_PREFIX_LENGTH_M``
     - ``50``
     - Minimum routed distance (m) from the transformer at which a later branch may attach to an
       existing branch instead of starting a new feeder at the transformer. A large value
       switches branch sharing off.
     - ✓
   * - ``AGGREGATE_NEARBY_CONNECTION_POINTS``
     - ``False``
     - Merge nearby street-side connection points of buildings on the same street into one
       (a proxy for shared house connections).
     - ✓
   * - ``CONNECTION_POINT_AGGREGATION_RADIUS_M``
     - ``15``
     - Maximum distance and cluster diameter (m) for the aggregation.
     - ✓
   * - ``CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS``
     - ``4``
     - Maximum number of buildings per aggregated connection point.
     - ✓

``CONSUMER_CATEGORIES`` rows have these fields:

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Field
     - Meaning
   * - ``consumer_category_id``
     - Integer key of the category.
   * - ``definition``
     - Category name. The load model uses ``Residential``, ``Commercial`` and ``Public``.
   * - ``peak_load``
     - Installed peak per load unit in kW (residential: per household).
   * - ``peak_load_per_m2``
     - Installed peak per m² of non-residential floor area in W/m² (Commercial, Public).
   * - ``yearly_consumption``, ``yearly_consumption_per_m2``
     - Stored in ``pylovo.consumer_categories``; not used by the grid generation.
   * - ``sim_factor``
     - Coincidence (simultaneity) factor *f* of the category, see
       :doc:`../concepts/grid_dimensioning`.

Transformer placement and settlement type
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 33 12 41 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``RURAL_MAX_HOUSEHOLDS``
     - ``1.0``
     - Average households per residential building at or below which the household score is
       fully rural.
     - ✓
   * - ``URBAN_MIN_HOUSEHOLDS``
     - ``2.85``
     - Average households per building at or above which the household score is fully urban.
     - ✓
   * - ``RURAL_MIN_BUILDING_DISTANCE``
     - ``44``
     - Mean distance (m) to the four nearest buildings at or above which the distance score is
       fully rural.
     - ✓
   * - ``URBAN_MAX_BUILDING_DISTANCE``
     - ``29``
     - Mean distance (m) at or below which the distance score is fully urban.
     - ✓
   * - ``TRANSFORMER_MAPPING``
     - ``1: [100, 160, 250, 400]``
       ``2: [250, 400, 630]``
       ``3: [400, 630, 800]``
     - Transformer ratings (kVA) allowed per settlement type (1 rural, 2 semi-urban, 3 urban).
       Every rating must exist in ``TRANSFORMERS``.
     - ✓

Clustering
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 33 12 41 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``MAX_BUILDINGS_PER_KCID``
     - ``1000``
     - Street components with more connected buildings are split by k-means into
       ⌈n / 1000⌉ clusters (kcid) before the building clustering.
     - ✓
   * - ``K_MEANS_SEED``
     - ``3329829316``
     - Seed of the k-means split and of all random draws (greenfield distance limits,
       station positions), which makes runs reproducible.
     - ✓
   * - ``MAX_GREENFIELD_TRAFO_DISTANCE``
     - ``1000``
     - Maximum routed distance (m) from a greenfield transformer to any connection point of its
       cluster (mean value if ``MAX_GREENFIELD_TRAFO_DISTANCE_STD`` > 0).
     - ✓
   * - ``MAX_GREENFIELD_TRAFO_DISTANCE_STD``
     - ``250``
     - Standard deviation (m) of a per-cluster distance limit drawn around the mean and clipped
       to ±2 standard deviations; ``0`` uses one fixed limit.
     - ✓
   * - ``GREENFIELD_TRAFO_POSITION_TOLERANCE``
     - ``1.0``
     - The greenfield station is drawn at random among the feasible positions whose
       load-weighted distance is at most (1 + tolerance) times the optimum; ``0`` takes the
       optimum.
     - ✓
   * - ``TRANSFORMER_PLANNING_UTILIZATION``
     - ``0.8``
     - Planned coincident load per transformer rating: a cluster load *P* needs a rating
       above *P* / 0.8. Used for sizing and for splitting clusters.
     - ✓
   * - ``MERGE_GREENFIELD_CLUSTERS``
     - ``False``
     - After splitting, merge neighbouring greenfield clusters if the merged cluster still fits
       one transformer of the ratings below and the distance limit.
     - ✓
   * - ``GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA``
     - ``[400]``
     - Ratings (kVA) allowed for merged clusters.
     - ✓
   * - ``MAX_BROWNFIELD_TRAFO_DISTANCE``
     - ``1000``
     - Maximum routed distance (m) between an existing transformer and a building assigned to it.
     - ✓
   * - ``USE_OPEN_TRANSFORMER_POSITIONS``
     - ``False``
     - Use open transformer positions (OSM, LoD2 stations, manual edits) from
       ``pylovo.transformers`` as brownfield grid roots.
     - ✓
   * - ``USE_DSO_TRANSFORMER_POSITIONS``
     - ``False``
     - Use imported DSO transformer positions as brownfield grid roots.
     - ✓
   * - ``USE_MANUAL_TRANSFORMER_POSITIONS``
     - ``False``
     - Use the positions placed in the transformer editor (``manual/…``) also when
       ``USE_OPEN_TRANSFORMER_POSITIONS`` is off, i.e. only a few hand-placed stations without
       the OSM and LoD2 candidates.
     - ✓

Ratings of candidates count only for the sources that are switched on: a rated candidate of a
disabled source never sizes a station.

.. note::

   ``MERGE_GREENFIELD_CLUSTERS``, ``GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA`` and
   ``USE_MANUAL_TRANSFORMER_POSITIONS`` were added to the version snapshot later. Snapshots of
   older versions do not contain them: pylovo then accepts the current values of the first two
   with a warning (the values used for the existing grids are unknown) and requires
   ``USE_MANUAL_TRANSFORMER_POSITIONS: False``, the behaviour before the switch existed.

Station voltage
~~~~~~~~~~~~~~~

The ±10 % voltage band of DIN EN 50160 is shared between the MV and the LV grid. pylovo follows
the split of Niederle et al. (2026, :doc:`../further_reading`): the LV busbar of the station is at
0.96 p.u. at the validation operating point, and the LV grid may drop to ``MIN_VM_PU`` of
``POWER_FLOW_VOLTAGE_LIMITS`` (0.90 p.u.). The transformer tap stays neutral: the reference already
stands for a station whose tap is set for its place in the MV grid (:ref:`validation-snapshot`).

.. list-table::
   :header-rows: 1
   :widths: 28 16 42 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``LV_REFERENCE_VOLTAGE_PU``
     - ``0.96``
     - LV busbar voltage at the operating point; pylovo sets the MV-side voltage of the external
       grid to reach it. ``null`` keeps the MV side at 1.0 p.u.
     - ✓

It affects only the validation power flow and its status, not the topology or the cable sizing.
Versions whose snapshot predates it were generated with the MV side at 1.0 p.u.; adding postcodes
to such a version requires ``LV_REFERENCE_VOLTAGE_PU: null``.

Equipment
~~~~~~~~~

The equipment catalogue is copied into ``pylovo.equipment_data`` for every version (✓ Snapshot).

``TRANSFORMERS``
   Transformer types with ``name``, ``s_max_kva`` (rating), ``cost_eur`` (per unit) and
   ``typ: Transformer``. The ratings allowed in a postcode area are selected with
   ``TRANSFORMER_MAPPING``.

``FEEDER_CABLES``
   Cables that may be used for feeders (street-side network).

``CONSUMER_CONNECTION_CABLES``
   Cables that may be used for service connections to buildings. A service connection that
   starts directly at the transformer may also use the feeder cables.

Cable rows have the fields ``name``, ``max_i_a`` (ampacity in A), ``r_mohm_per_km``,
``x_mohm_per_km``, ``z_mohm_per_km`` (mΩ/km), ``cost_eur`` (material cost in EUR/m) and
``typ: Cable``. Rules:

* The last ``_``-separated part of the name is the cross-section in mm² (``NAYY_4_150``); it is
  used to order cables.
* Numeric values are stored as integers; use whole numbers.
* A cable may be listed in both pools (for example ``NAYY_4_150``). The database keeps one row
  per name, so both entries must have identical values.

``config_analysis.yaml``
------------------------

.. list-table::
   :header-rows: 1
   :widths: 28 20 38 14
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning
     - Snapshot
   * - ``POWER_FLOW_VOLTAGE_LIMITS``
     - ``MIN_VM_PU: 0.9``
       ``MAX_VM_PU: 1.1``
     - Voltage band used to classify a converged power flow as ``converged`` or
       ``voltage_violation``. It does not affect convergence, topology or cable sizing.
     - ✓
   * - ``PLOT_COLOR_DICT``
     - rating → colour
     - Colours per transformer rating (kVA) for statistics plots.
     -
   * - ``COLORS``, ``PALETTES``
     - TUM colours
     - Colour definitions; ``TUMPalette`` becomes the seaborn default palette if seaborn is
       installed.
     -
   * - ``NETWORK_COLORS``
     - three colours
     - Node colours of the graph plots (transformer, consumer, connection bus) in
       :mod:`pylovo.plotting.generation.networks`.
     -
   * - ``PLOT_DEFAULTS``
     - figure size, DPI, fonts
     - Loaded into ``DEFAULT_*`` constants of :mod:`pylovo.config_loader`; currently not used by
       the plotting functions.
     -
   * - ``MUNICIPAL_REGISTER``
     - column list
     - Column names of ``pylovo.municipal_register`` in table order; used to read the register.
       Do not change.
     -
