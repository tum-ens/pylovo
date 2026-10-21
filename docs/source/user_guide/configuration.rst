Configuration
=============

pylovo is configured with two kinds of files:

* ``.env`` in the repository root -- database connection, input mode and coordinate system;
* ``config/*.yaml`` -- all grid generation parameters (``config_generation_lv.yaml``) and the
  post-processing settings (``config_analysis.yaml``).

Both are read once, when :mod:`pylovo.config_loader` is imported. A running process does not
see later edits; every ``pylovo-*`` command reads the files anew when it starts.

.. _env-variables:

Environment variables (``.env``)
--------------------------------

``.env`` is loaded with ``load_dotenv(find_dotenv() or find_dotenv(usecwd=True), override=True)`` before
the YAML files. The first ``.env`` in the directories above the installed ``pylovo`` package (for a
source checkout: the repository root) is used -- in an interactive session or a notebook the search starts
in the current working directory instead. If that search finds none, as for a wheel installed in a virtual
environment outside the project (:ref:`install-wheel`), the ``.env`` of the current working directory or its
parents is used. A ``.env`` above the installed package therefore wins over the one in the working directory.
The values of the ``.env`` **override** environment variables with the same name.

.. list-table::
   :header-rows: 1
   :widths: 26 14 60
   :class: fixed-table

   * - Variable
     - Default
     - Meaning
   * - ``DBNAME``
     - required
     - Database that holds the schema ``pylovo`` (with InfDB: the InfDB database, unless
       ``USE_EXTERNAL_BASEDATA``).
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
       same database. Anything else: file-based mode (:ref:`file-based-input`). Part of the version
       snapshot.
   * - ``USE_EXTERNAL_BASEDATA``
     - ``False``
     - ``True``/``1``/``yes``: read the InfDB schemas from the database given by ``INFDB_DBNAME``,
       ``INFDB_USER``, ``INFDB_HOST``, ``INFDB_PORT`` and ``INFDB_PASSWORD``, e.g. a central InfDB
       on another server, while the schema ``pylovo`` and the results stay in ``DBNAME``.
       PgBouncer in front of InfDB is supported. LV grids only: the MV stage needs the InfDB
       schemas in ``DBNAME`` (:ref:`mv-prerequisites`).
   * - ``INFDB_DBNAME``, ``INFDB_USER``, ``INFDB_HOST``, ``INFDB_PORT``, ``INFDB_PASSWORD``
     - pylovo connection
     - Connection of the external InfDB (``USE_EXTERNAL_BASEDATA`` only). Unset values take the
       value of ``DBNAME``, ``DBUSER``, ``HOST``, ``PORT`` and ``PASSWORD``.
   * - ``INFDB_SOURCE_SCHEMA``
     - ``basedata``
     - InfDB schema with ``buildings``, the function ``classify_building_use`` and the street
       tables ``ways_per_connection`` and ``connection_lines``. Part of the version snapshot.
   * - ``INFDB_OPENDATA_SCHEMA``
     - ``opendata``
     - InfDB schema with ``postcodes_germany``.
   * - ``INFDB_OSM_SCHEMA``
     - ``osm``
     - Schema of the InfDB OSM layers (``infrastructure_point``, ``infrastructure_polygon``,
       ``infrastructure_line``, ``tags``, ``landuse_polygon``), read by the MV stage for the HV/MV
       substations, the MV overhead lines and the built-up land use (:doc:`mv_grids`).
   * - ``TARGET_EPSG``
     - ``25832``
     - Projected CRS (metres) of all geometries stored by pylovo. Choose it before the first
       ``pylovo-setup``; the table definitions use it. Part of the version snapshot.
   * - ``PYLOVO_ROOT``
     - --
     - Optional project root for installations outside a checkout: ``$PYLOVO_ROOT/config`` is
       searched for configuration files and ``$PYLOVO_ROOT/data`` is used as data directory.
   * - ``PYLOVO_DATA_DIR``
     - ``$PYLOVO_ROOT/data``, else ``./data``
     - Optional data directory with all input data: ``transformer_data/``, ``municipal_register/``
       and, in the file-based mode, ``postcode.csv``, ``ways/`` and ``buildings/``. The municipal
       register also finds the ``data/`` folder of the checkout. Reference data that is missing here
       (municipal register, Overpass queries, processed Bavarian transformers) is read from the copy
       in the wheel (``pylovo/_defaults/data``); pylovo writes only into this directory.
   * - ``PYLOVO_CONFIG_DIR``
     - ``~/.config/pylovo``
     - Optional directory with the YAML files, searched after ``./config`` and
       ``$PYLOVO_ROOT/config``. If it is set, ``~/.config/pylovo`` and ``./.pylovo`` are not
       searched (see :ref:`config-file-lookup`).
   * - ``ACCESS_TOKEN_PLOTLY``
     - --
     - Optional Mapbox access token for the map background of some plotly figures (plotting extra).

.. _config-file-lookup:

Where the YAML files are found
------------------------------

Each YAML file is taken from the first of these locations that contains it:

#. ``./config/`` in the current working directory,
#. ``$PYLOVO_ROOT/config/``,
#. ``$PYLOVO_CONFIG_DIR`` if set; otherwise ``~/.config/pylovo/`` (Linux/macOS) and then
   ``./.pylovo/``,
#. ``config/`` of the source checkout that contains the package,
#. the default configuration in the wheel (``pylovo/_defaults/config``, only in a wheel installation,
   :ref:`install-wheel`).

The files ``config_generation_lv.yaml`` (the LV grids), ``config_analysis.yaml``,
``config_classification.yaml`` and ``config_clustering.yaml`` must exist. The last two belong to
the classification module (:doc:`classification`) and do not influence grid generation. ``config_generation_mv.yaml`` (the
MV stage) is read from the directory of ``config_generation_lv.yaml``; without it, the MV stage
takes its shipped values. The former single file ``config_generation.yaml`` is still read in place
of ``config_generation_lv.yaml``, with its ``MV_*`` keys. pylovo warns about it, and also when
the former file hides ``config_generation_lv.yaml`` or ``config_generation_mv.yaml`` of a folder
that is searched later.

pylovo also warns, once per file, about a top-level key that nothing reads, for example a
misspelled ``HOUSE_CONECTION`` or a key of an older release, and suggests the closest key it does
read. Such a key has no effect: the setting keeps its default, which differs from the shipped value
for some keys. The warning covers ``config_generation_lv.yaml``, ``config_analysis.yaml``,
``config_classification.yaml`` and ``config_clustering.yaml``; ``config_generation_mv.yaml`` refuses
an unknown key with an error.

.. _versions:

Versions and the parameter snapshot
-----------------------------------

Every generated grid is stored under ``VERSION_ID``. When a version is used for the first time,
pylovo stores a snapshot of all generation parameters in ``pylovo.version.generation_parameters``.
Every later run with the same ``VERSION_ID`` compares the current configuration with that
snapshot and stops with

.. code-block:: text

   ValueError: Generation parameters differ from the stored snapshot for version 1
   (max_end_to_end_feeder_voltage_drop_percent, ...). Increment VERSION_ID before generating
   grids with the changed configuration.

if anything differs. The column **Snapshot** in the tables below marks the keys that are part of
the snapshot. ``VERSION_ID`` is stored as ``varchar(10)``: use 1 to 10 letters, digits, ``_``,
``.`` or ``-``. pylovo reads it as text (``VERSION_ID: 2`` is the version ``"2"``) and stops at the
start for any other value. Quote an id that YAML would read as another type: ``1.0`` is refused,
``"1.0"`` is the text.

The snapshot also records which data the grids are built from: ``USE_INFDB``, ``TARGET_EPSG`` and
``INFDB_SOURCE_SCHEMA`` of the ``.env`` (``input_data`` in the snapshot). Changing one of them needs a
new ``VERSION_ID`` as well. A version stored before these entries existed is not blocked: pylovo logs
that the values used for its existing grids are not recorded.

Versions that pylovo 0.x stored have a snapshot with other keys, so adding postcodes to such a
version stops with the message above: generate those postcodes under a new ``VERSION_ID``.

A postcode that already has grids in the active version is skipped
(``Grid for the postcode area ... has already been generated``). Delete the old grids first
(:doc:`cli`, ``pylovo-delete``) or use a new version.

The ``MV_*`` keys of the MV stage are not part of this snapshot. The MV stage stores them, with its
constants, once per MV variant of a version in ``pylovo.mv_variant.mv_parameters``, and a changed
MV parameter needs a new ``MV_VARIANT_ID`` instead of a new ``VERSION_ID`` (:ref:`mv-variants`).

``config_generation_lv.yaml``
-----------------------------

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
     - Electrical model used to build and solve the grids. Only ``pandapower`` ships; another backend
       can be registered (:doc:`../concepts/electrical_backends`).
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
     - 4 rows
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
     - ``200``
     - Commercial or public load components above this peak (kW) are assumed to have their own
       MV connection and are left out of the LV grid (eGon-data: 200 kW boundary between the
       MV/LV station level and the MV level). The buildings are stored in ``pylovo.mv_customers``
       for the MV stage. The default was 100 kW before the MV stage.
     - ✓
   * - ``CABLE_PLANNING_UTILIZATION``
     - ``0.8``
     - Planned coincident current per cable ampacity, for feeder and service cables: a design
       current *I* needs a cable design rated above *I* / 0.8, the cable counterpart of
       ``TRANSFORMER_PLANNING_UTILIZATION``. It keeps the capacity reserve that LV planning
       leaves on cables (a maximum planned line utilisation of 80 %); voltage drops are still
       computed at *I*. ``1.0`` sizes at the full ampacity. The former name
       ``FEEDER_PLANNING_UTILIZATION`` (feeders only) is still read.
     - ✓
   * - ``FEEDER_STANDARD_CABLE``
     - ``null``
     - Standard feeder cable (Standardkabel, Regelquerschnitt), a name from ``FEEDER_CABLES``
       such as ``NAYY_4_150``. Every feeder section uses it, laid in parallel (Parallelkabel)
       when one is not enough, for the load and for the voltage drop; larger cables from
       ``FEEDER_CABLES`` only beyond ``MAX_PARALLEL_FEEDER_CABLES``. Many DSOs plan their LV
       networks this way. ``null`` picks the cheapest sufficient cable from ``FEEDER_CABLES``
       (larger cross-sections before parallel cables).
     - ✓
   * - ``MAX_PARALLEL_FEEDER_CABLES``
     - ``4``
     - Most parallel standard cables per feeder section, only used with
       ``FEEDER_STANDARD_CABLE``.
     - ✓
   * - ``FEEDER_SPLIT_MAX_CURRENT_KA``
     - ``0.4``
     - Topology parameter: maximum coincident current (kA) while connection points are grouped
       into one planned branch. Not a cable limit.
     - ✓
   * - ``MIN_SHARED_PREFIX_LENGTH_M``
     - ``50``
     - Minimum routed distance (m) from the transformer at which a later branch may attach to an
       existing branch instead of starting a new feeder at the transformer. A large value
       switches branch sharing off.
     - ✓
   * - ``HOUSE_CONNECTION``
     - ``joint``
     - How houses are connected to the feeders (Hausanschlusstechnik). ``joint``
       (Muffentechnik): each house is connected with a tee joint (Abzweigmuffe) on the street
       cable, and cable distribution cabinets (Kabelverteilerschränke, KVS) only sit at the
       feeder split points. ``cabinet`` (Kabelverteilertechnik): houses are connected from
       cabinets placed along the feeders, each with its own house-connection cable from the
       nearest cabinet, and the feeder cable runs from cabinet to cabinet. Which technique
       applies is a DSO practice that varies by network ("Kabelverteiler- oder
       Muffenanschluss"); cabinet connections are common in rural Bavaria. See
       :ref:`house-connection-technique`.
     - ✓
   * - ``CABINET_SPACING_M``
     - ``100``
     - Only with ``HOUSE_CONNECTION: cabinet``: longest feeder cable (m) without a cabinet
       (Abstand der Kabelverteiler). The default comes from one rural reference network (first
       cabinet a median 105 m from the station, cabinets 74 m apart); check it against the
       practice of the DSO.
     - ✓
   * - ``AGGREGATE_NEARBY_CONNECTION_POINTS``
     - ``False``
     - Merge nearby street-side connection points of buildings on the same street into one
       (a proxy for shared house connections). With ``HOUSE_CONNECTION: cabinet`` the merged
       buildings share one house-connection cable from their cabinet.
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
     - Category name. The load model uses ``Residential``, ``Commercial``, ``Public`` and ``Unknown``
       (buildings of unknown use; the shipped 0 W/m² gives them no load, so they stay out of the grids).
   * - ``peak_load``
     - Installed peak per load unit in kW (residential: per household).
   * - ``peak_load_per_m2``
     - Installed peak per m² of non-residential floor area in W/m² (Commercial, Public).
   * - ``yearly_consumption``, ``yearly_consumption_per_m2``
     - Stored in ``pylovo.consumer_categories``; not used by the grid generation.
   * - ``sim_factor``
     - Coincidence (simultaneity) factor *f* of the category, a number from 0 to 1, see
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
     - ``1: [250, 400]``
       ``2: [400, 630]``
       ``3: [400, 630]``
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
     - ``0.3``
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
     - ``True``
     - After splitting, merge neighbouring greenfield clusters if the merged cluster still fits
       one transformer of the ratings below and the distance limit. Where existing
       (brownfield) stations are used, a greenfield cluster that still has at most
       ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS`` buildings afterwards joins the nearest
       existing station within ``MAX_BROWNFIELD_TRAFO_DISTANCE``, without checking that
       station's rating (a few buildings at the edge of its area). Needs a building limit.
     - ✓
   * - ``GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA``
     - ``[250, 400, 630]``
     - Ratings (kVA) allowed for merged clusters.
     - ✓
   * - ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS``
     - ``4``
     - Merge a pair only if its smaller cluster has at most this many loaded buildings: small
       fragments of the split join their nearest feasible neighbour, larger clusters stay as
       split. ``null`` merges clusters of any size.
     - ✓
   * - ``MAX_BROWNFIELD_TRAFO_DISTANCE``
     - ``1000``
     - Maximum routed distance (m) between an existing transformer and a building assigned to it.
     - ✓
   * - ``BROWNFIELD_NEAREST_ONLY``
     - ``False``
     - A building joins only its nearest existing station. When that station is full, the
       building gets a greenfield station instead of joining a farther existing station that still
       has capacity, so existing stations do not reach far beyond their own area.
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
     - Use the positions placed with the transformer map (``pylovo-import transformers-ui``) or
       the HTTP API (``manual/…``) also when ``USE_OPEN_TRANSFORMER_POSITIONS`` is off, i.e. only a
       few hand-placed stations without the OSM and LoD2 candidates.
     - ✓
   * - ``ROUTE_ACROSS_POSTCODE_BORDERS``
     - ``True``
     - A PLZ also routes over the InfDB streets of its neighboring PLZ within
       ``MAX_BROWNFIELD_TRAFO_DISTANCE`` / 2 + 100 m outside its polygon, so a route may pass
       through the neighboring PLZ. Buildings and existing stations stay per PLZ; only the ways
       touching the PLZ are saved to ``ways_result``. A building whose InfDB connection line ends on
       a neighboring street (such lines are often filed under the neighboring postcode) is connected
       there; without the switch it is usually left out as ``isolated_component``. The strip holds
       every brownfield route up to ``MAX_BROWNFIELD_TRAFO_DISTANCE`` whose ends lie in the PLZ
       (any route of length L lies within L/2 of one of its ends); longer greenfield routes are not
       guaranteed. A configuration
       without the key keeps the PLZ graph (``False``); with file input (``USE_INFDB=False``) the
       switch has no effect.
     - ✓

Ratings of candidates count only for the sources that are switched on: a rated candidate of a
disabled source never sizes a station.

``ROUTE_ACROSS_POSTCODE_BORDERS`` is recorded in the version snapshot with its effective values (width
of the neighboring streets and a rule revision); versions stored before it require ``False``.

.. note::

   ``MERGE_GREENFIELD_CLUSTERS``, ``GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA``,
   ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS``, ``USE_MANUAL_TRANSFORMER_POSITIONS`` and
   ``BROWNFIELD_NEAREST_ONLY`` were added to the version snapshot later. Snapshots of older versions do not contain them: pylovo then
   accepts the current values of the first two with a warning (the values used for the existing
   grids are unknown) and requires ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS: null``,
   ``USE_MANUAL_TRANSFORMER_POSITIONS: False`` and ``BROWNFIELD_NEAREST_ONLY: False``, the behaviour
   before these keys existed.

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

* The last ``_``-separated part of the name is the cross-section in mm² (``NAYY_4_150``); it
  breaks cost ties when cables are selected.
* Numeric values are stored as integers; use whole numbers.
* A cable may be listed in both pools (for example ``NAYY_4_150``). The database keeps one row
  per name with both roles (``grid_role`` ``feeder,consumer_connection``), so both entries must
  have identical values. Different values stop the generation.

pylovo checks the equipment and the load factors when it loads the configuration: every cable needs a
``max_i_a`` above 0, every rating of ``TRANSFORMER_MAPPING`` must be defined in ``TRANSFORMERS``, and
every ``sim_factor`` must be a number from 0 to 1. Otherwise the command stops at the start.

.. _mv-configuration:

``config_generation_mv.yaml``
-----------------------------

``config_generation_mv.yaml`` configures the MV stage (:doc:`mv_grids`). Its keys
are stored per MV variant, not in the snapshot of ``VERSION_ID``, and ``pylovo-mv generate --set``
changes them for one variant. A missing key takes its shipped value. ``MV_*`` keys in the LV file
(as in the former ``config_generation.yaml``) are still read; a key in both files needs the same
value. :doc:`mv_parameters` lists the keys with their shipped values, meanings and sources,
together with the constants of the stage, which are changed in Python only.

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
   * - ``PALETTES``
     - ``TUMPalette``, ``TUMPalette1``
     - TUM colour palettes; ``TUMPalette`` becomes the seaborn default palette if seaborn is
       installed.
     -
   * - ``NETWORK_COLORS``
     - three colours
     - Node colours of the graph plots (transformer, consumer, connection bus) in
       :mod:`pylovo.plotting.generation.networks`.
     -
   * - ``MUNICIPAL_REGISTER``
     - column list
     - Column names of ``pylovo.municipal_register`` in table order; used to read the register.
       Do not change.
     -
