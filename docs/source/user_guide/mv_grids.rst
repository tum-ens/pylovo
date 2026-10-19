MV grids
========

The MV stage adds medium-voltage (MV) grids to a generated LV version: open rings from the HV/MV
substations of the region to the stations of the LV grids and the MV customers
(:doc:`../concepts/mv_grids`). This page shows how to prepare its inputs, run it, compare MV
variants and read the results. The stage runs on the command line and from Python. The HTTP API and
the GridPlanner UI do not offer it yet.

The default workflow plans for peak load and N-1, like the LV-first workflow. It uses 240 mm² cable
and 122-AL1 overhead conductors. Generation is optional: ``MV_INCLUDE_GENERATION: true`` adds
generator inputs, connections and feed-in checks in a separate variant. Load-only results do not
claim feed-in acceptance.

.. _mv-prerequisites:

Prerequisites
-------------

.. list-table::
   :header-rows: 1
   :widths: 27 33 40
   :class: fixed-table

   * - Input
     - Used for
     - How to provide it
   * - Generated LV version
     - Stations, loads, MV customers, stored LV nets
     - ``pylovo-generate`` with the pandapower backend (:doc:`generating_grids`)
   * - Migration ``0009_mv_grids``
     - The MV tables
     - ``pylovo-setup`` (:ref:`mv-schema`)
   * - MaStR units (generation-enabled variants only)
     - MV generators and LV-side generation
     - ``pylovo-import mastr`` (:ref:`mv-mastr-import`)
   * - OSM power data in InfDB
     - HV/MV substations, built-up land use
     - Schema ``INFDB_OSM_SCHEMA`` (:ref:`mv-osm-data`)
   * - Regional street graph of InfDB
     - Routing graph and settlements
     - ``<INFDB_SOURCE_SCHEMA>.ways_per_junction`` and ``.buildings``
   * - eGon-data tables (optional)
     - ``MV_SUBSTATIONS: egon``, ``MV_SUPPLY_AREAS: egon``, cross-check of the substations
     - ``egon_hvmv_substation`` and ``egon_mv_grid_district`` (:ref:`mv-egon-data`)
   * - Municipal register
     - Population of the demand anchor in the run summary
     - Loaded by ``pylovo-setup``

The stage needs ``USE_INFDB=True``: it reads the InfDB schemas in the pylovo database
(:doc:`input_data`).

LV version
~~~~~~~~~~

The MV stage works on any generated LV version of the pandapower backend, with greenfield or
brownfield stations. It reads the LV parameters it needs from the parameter snapshot of the version
(sim factors, power factor, specific peaks per m², ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``,
``LV_REFERENCE_VOLTAGE_PU``, ``TRANSFORMER_PLANNING_UTILIZATION`` and ``TRANSFORMER_MAPPING``), never
from the current configuration. Three properties of the version matter:

* The LV stage stores the MV customers in ``pylovo.mv_customers`` since the MV stage exists. A
  version generated before has no rows. The stage then takes only the mixed buildings with an
  MV-direct component from ``buildings_result`` (flag ``not_in_mv_customers``), and the pure
  non-residential MV customers are missing. Generate such versions again under a new
  ``VERSION_ID``.
* ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` decides which loads leave the LV grids
  (:ref:`mv-customers`). The shipped value is 200 kW. Versions generated with the former default of
  100 kW keep it in their snapshot.
* With ``LV_REFERENCE_VOLTAGE_PU: null`` the stations have no stored voltage requirement, and the
  stage computes no voltage budgets and MV/LV taps (flag ``no_lv_reference``).

.. _mv-schema:

Database schema
~~~~~~~~~~~~~~~

The MV tables are created by migration ``0009_mv_grids`` (:doc:`../concepts/database_tables`). A new
schema gets them with ``pylovo-setup``. A schema set up before needs one run of the
non-destructive setup, which keeps all grids:

.. code-block:: bash

   uv run pylovo-setup

Until the migration is recorded, ``pylovo-generate`` and ``pylovo-mv`` stop with
``PyLovo schema has pending migrations (0009_mv_grids); run pylovo-setup first``. Run the setup when
no generation is running, because the new foreign keys to ``grid_result`` wait for running
transactions.

.. _mv-mastr-import:

MaStR import (optional)
~~~~~~~~~~~~~~~~~~~~~~~

Load-only planning does not query or require MaStR data. With ``MV_INCLUDE_GENERATION: true``,
the generators come from the Marktstammdatenregister (MaStR) of the Bundesnetzagentur. Its bulk
export is downloaded and parsed with `open-mastr <https://open-mastr.readthedocs.io>`_, a separate
tool that writes an SQLite file and is not a dependency of pylovo:

.. code-block:: bash

   uv run --no-project --with open-mastr python -c "from open_mastr import Mastr; \
       Mastr().download(data=['wind', 'solar', 'biomass', 'hydro', 'gsgk', 'combustion', 'storage', 'grid', 'location'])"
   uv run pylovo-import mastr --sqlite ~/.open-MaStR/data/sqlite/open-mastr.db

``pylovo-import mastr [--sqlite SQLITE] [--margin-m MARGIN_M]`` reads the SQLite file (default
``~/.open-MaStR/data/sqlite/open-mastr.db``) and replaces two tables:

* ``pylovo.mastr_units``: the units in operation with coordinates inside the extent of
  ``pylovo.postcode`` plus ``--margin-m`` (default 5,000 m), with technology, net power and the
  connection voltage level (``Spannungsebene``) of their grid connection point,
* ``pylovo.mastr_postcode_aggregates``: the units in operation without coordinates (mostly small
  rooftop PV), summed per postcode and technology.

The tables are not versioned. A new import changes the inputs of MV variants built afterwards but
not the stored ones. Without an import the tables are empty, and the stage plans the MV grids
without generators.

.. note::

   open-mastr 1.x stops on the solar and combustion units of the MaStR bulk export in version
   26.2, because some catalogue fields of these units hold several values. Until open-mastr reads
   this export, these units have to be written into the tables ``solar_extended`` and
   ``combustion_extended`` of the SQLite file by other means, for example with a separate XML
   parser. ``pylovo-import mastr`` only reads the SQLite file and skips a technology whose table is
   missing.

The MaStR data are published under the licence dl-de/by-2-0 (attribution: Bundesnetzagentur,
Marktstammdatenregister).

.. _mv-osm-data:

OSM power data
~~~~~~~~~~~~~~

The HV/MV substations come from the OpenStreetMap layers that the InfDB OSM importer writes into the
schema ``INFDB_OSM_SCHEMA`` of ``.env`` (default ``osm``, :ref:`env-variables`):

.. list-table::
   :header-rows: 1
   :widths: 36 64
   :class: fixed-table

   * - Table
     - Used for
   * - ``infrastructure_point``, ``infrastructure_polygon``
     - Features with ``osm_type = 'power'``: substations (``osm_subtype = 'substation'``), plants
       and generators (excluded substations) and transformers.
   * - ``tags``
     - The OSM tags (``voltage``, ``substation``, ``frequency``, ``operator``, ``name``,
       ``railway``) as ``jsonb``, joined by ``geom_type`` and ``osm_id``.
   * - ``landuse_polygon`` (optional)
     - Built-up land use (residential, commercial, industrial, retail) of the settlement flags.
       Without it the settlements come from address buildings only (warning).

Without OSM substations near the region, the stage stops. ``MV_SUBSTATIONS: egon`` uses the
eGon-data substations instead.

.. _mv-egon-data:

eGon-data (optional)
~~~~~~~~~~~~~~~~~~~~

The eGon-data HV/MV substations (``egon_hvmv_substation``: ``bus_id``, ``voltage``, ``osm_id``,
``geom``) and MV grid districts (``egon_mv_grid_district``: ``bus_id``, ``geom``) are read from the
schema ``pylovo`` if they exist there and hold rows, else from ``INFDB_OPENDATA_SCHEMA``. They are
needed for ``MV_SUBSTATIONS: egon`` and ``MV_SUPPLY_AREAS: egon``. Without them, the default variant
runs and only the eGon cross-check of the OSM substations is skipped (warning). pylovo has no
importer for these tables yet. Copy them from the eGon-data database or the Open Energy Platform.

The eGon substations are processed OSM-derived records. Keep the source release and the typed OSM
identifiers when comparing them with another grid dataset. The stored ``egon_bus_id`` of an OSM
site is a spatial cross-check; it does not establish an electrical connection to an external HV
network. Its distance is measured to the station footprint where available, otherwise to its point.

Quick start
-----------

.. code-block:: bash

   uv run pylovo-setup                                   # once: migration 0009_mv_grids
   uv run pylovo-generate --plz 80803 80802 --mv         # LV grids, then the MV grids
   uv run pylovo-mv list --version 1                     # the MV variants of version 1

``pylovo-generate --mv`` runs the MV stage for the MV variant of the configuration
(``MV_VARIANT_ID``, default ``1``) of ``VERSION_ID``. Further variants of the same LV version are
built with ``pylovo-mv generate`` (:ref:`mv-variants`). To opt into generation, prepare MaStR data
and create another variant with ``--set MV_INCLUDE_GENERATION=true``. Existing snapshots without
this flag retain their original generation-enabled behavior.

Running the MV stage
--------------------

After the LV grids
~~~~~~~~~~~~~~~~~~

With ``MV_GRID_GENERATION: True`` in ``config_generation.yaml``, or with ``--mv``,
``pylovo-generate`` runs the MV stage once after all postcodes of the call, for the configured MV
variant and the postcodes of the call. ``--no-mv`` skips it. The LV grids of every postcode are
committed first, so a failing MV stage never undoes them. If the MV stage does not end ``done``,
``pylovo-generate`` exits with code 1. A second call with ``--mv`` for generated postcodes skips the
LV generation and runs only the MV stage.

On a stored LV version
~~~~~~~~~~~~~~~~~~~~~~

``pylovo-mv generate`` runs the MV stage for any stored LV version and any MV variant:

.. code-block:: text

   pylovo-mv generate --version VERSION (--plz PLZ [PLZ ...] | --ags AGS [AGS ...] | --all)
                      [--variant VARIANT] [--comment COMMENT] [--set KEY=VALUE [KEY=VALUE ...]]
                      [--parallel | --no-parallel] [--jobs JOBS] [--replace]

``--version VERSION``
   The LV version (required). ``pylovo-mv`` never uses ``VERSION_ID`` implicitly.
``--plz``, ``--ags``, ``--all``
   The postcodes with LV grids of the version, the postcodes of municipalities (AGS) or every
   postcode with LV grids of the version. Requested postcodes without LV grids are reported as
   missing.
``--variant VARIANT``
   MV variant id of 1 to 10 letters, digits, ``_``, ``.`` or ``-`` (default ``MV_VARIANT_ID``).
``--comment COMMENT``
   Comment stored with a new variant (default ``MV_VARIANT_COMMENT``).
``--set KEY=VALUE [KEY=VALUE ...]``
   Parameters of this variant that differ from the configuration (:ref:`mv-variants`).
``--parallel`` / ``--no-parallel``
   Plan the supply areas in worker processes or one after the other in the main process (default
   ``PARALLEL``).
``--jobs JOBS``
   Number of workers (default the smaller of 4 and ``N_JOBS``). The stage lowers it so that about
   0.6 GB of free memory per worker and 2 GB for the rest of the machine remain.
``--replace``
   Delete the variant first and build it again.

.. code-block:: bash

   uv run pylovo-mv generate --version 1 --all --variant d0 --comment "cable rings" \
       --set MV_STUBS=none MV_OVERHEAD_LINES=none MV_INTERCONNECTION=none
   uv run pylovo-mv generate --version 1 --ags 09162000 --jobs 2

Every output line starts with ✓ or ✗. The first line names the database, the version and the
variant. The summary at the end has this form (``<...>`` stands for the values of the run):

.. code-block:: text

   ✓ MV stage on <DBNAME>@<HOST>:<PORT>, version 1, variant d0
   ✓ MV variant d0 of version 1: <n> grids (<n> done), <n> stations, <n> customers, <n> generators, <km> km (cable <share> %), <n> grids with violations, <n> band conflicts, <s> s
   ✓ Nominal voltage auto (10 kV from 8 MVA/km² installed): <n> at 20 kV
   ✓ Ties: none (no rings of different districts of the same voltage within 2 km)
   ✓ Links: <n> normally-open links between rings, <km> km, <n> of <n> rings, <share> % of the supply points with a third cable, <n> ring pairs joined twice
   ✓ Demand anchor: <kW> kW per inhabitant (<MW> MW coincident, <n> inhabitants of <n> PLZ; MV customers ...)
   ✓ OSM coverage: <n> mapped MV/LV station sites for <n> planned stations (<share> %; town ..., village ..., open field ...), OSM MV lines <km> km (<km/km²> km/km²)

The OSM coverage line compares the station sites and MV lines mapped in OpenStreetMap with the planned
stations (:ref:`mv-osm-coverage`); the full tables per settlement and land-use class are in the log and
in ``mv_variant.summary['osm_coverage']``. Without an OSM power layer the line is missing.

The exit code is 0 when the variant is ``done`` and 1 otherwise (2 for a usage error). A variant is
``partial`` when some supply areas failed. Every supply area is one database transaction, and a
failed one keeps its error in ``mv_grid.error`` while the others continue. If the worker pool breaks
(for example when the system runs out of memory), the remaining supply areas stay ``pending``. The
same command with fewer ``--jobs`` then plans only the pending and failed supply areas.

The stage takes a lock per variant, so a second run of the same variant stops at once. It also stops
before any write when a requested postcode is being generated by ``pylovo-generate`` at that moment.
The main process logs to ``log/log.txt`` in the working directory, each worker to
``log/log_mv_<version>_<variant>_<mv_grid_id>.txt``.

From Python
~~~~~~~~~~~

The command-line tools call :func:`pylovo.mv_grid.stage.run_mv_stage`:

.. code-block:: python

   from pylovo.mv_grid.parameters import MvParameters
   from pylovo.mv_grid.stage import run_mv_stage

   params = MvParameters.from_config().replace(variant_id="r15", comment="15 ring members")
   params = params.replace_raw({"MV_MAX_STATIONS_PER_RING": 15})
   summary = run_mv_stage("1", [80803, 80802], params, parallel=True, jobs=2)
   print(summary.describe())

``plz_list=None`` takes every postcode with LV grids of the version. :class:`~pylovo.mv_grid.parameters.MvParameters`
checks every value on construction. ``replace`` takes field names, ``replace_raw`` the names of
``--set``.

.. _mv-variants:

MV variants
-----------

An MV variant is one set of MV parameters for one LV version, identified by
``(version_id, mv_variant_id)``. The first run stores the parameters once in
``mv_variant.mv_parameters``: every ``MV_*`` key, every constant, the resolved line and HV/MV
transformer types and the version of the stage code (``mv_stage_version``). The ``MV_*`` keys are
not part of the snapshot of the LV version, so MV variants never require a new ``VERSION_ID``.

``--set KEY=VALUE`` changes a parameter for one variant. KEY is an ``MV_*`` key of
``config_generation.yaml`` (except ``MV_GRID_GENERATION``, ``MV_VARIANT_ID`` and
``MV_VARIANT_COMMENT``) or a constant of :ref:`mv-parameters`. VALUE is read as YAML (``null``,
numbers, lists like ``[25, 40, 63]``, mappings like ``{lv: 100, mvlv: 200, mv: 5500, hvmv: 20000}``)
and checked against the rules of the parameter. A value with spaces needs quotes in the shell:

.. list-table:: Examples of variants
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Question
     - ``--set``
   * - Cable rings only
     - ``MV_STUBS=none MV_OVERHEAD_LINES=none MV_INTERCONNECTION=none``
   * - Straight lines × 1.3, as in Ding0
     - ``MV_ROUTING=crowfly``
   * - Sweep instead of savings
     - ``MV_RING_SOLVER=sweep``
   * - Value of the local search
     - ``local_search_rounds=0``
   * - eGon-data substations and districts
     - ``MV_SUBSTATIONS=egon MV_SUPPLY_AREAS=egon``
   * - Heavier conductors
     - ``"MV_STANDARD_CABLE=NA2XS2Y 1x240 RM/25 12/20 kV" "MV_OVERHEAD_LINE=122-AL1/20-ST1A 20.0"``
   * - Smaller rings
     - ``MV_MAX_STATIONS_PER_RING=15``
   * - SimBench line utilisation
     - ``MV_LINE_PLANNING_UTILIZATION=0.5``
   * - One voltage for all supply areas
     - ``MV_NOMINAL_VOLTAGE_KV=20``
   * - LV-side PV with cos φ 0.95
     - ``lv_pv_cos_phi=0.95``
   * - Every MV customer with a node of its own, no hall rule
     - ``customer_hall_min_footprint_m2=null customer_min_mv_kw=0``

A later run of the same variant compares its parameters with the snapshot. Different parameters
stop the run with ``MV parameters differ from the stored snapshot of MV variant <id> of version
<version> (...)``. Use another ``--variant`` or delete the variant in this case. With the same
parameters, the run depends on the postcodes and the routing graph:

* The same postcodes, or a subset of them, resume the variant. Only ``pending`` and ``failed``
  supply areas are planned, and nothing happens when all are done.
* New postcodes extend the variant to the union of the old and the new postcodes. Every supply
  area is planned again, because the supply areas change.
* A changed routing graph (changed InfDB rows or graph build) also plans every supply area again.
* ``--replace`` deletes the variant and builds it from scratch.

A variant stored by an older version of the stage code can still be read, coupled and exported,
but not resumed: its ``mv_stage_version`` differs. Build it again with ``--replace`` or under a new
variant id.

A variant becomes stale when the LV results of its postcodes change after it was built, for example
by load edits, deleted or regenerated grids. ``pylovo-mv list`` compares a fingerprint of the LV
results (number of grids, grid ids, newest load edit, number of MV customers) with the one stored
at creation and marks such variants ``stale``. Rebuild them with ``--replace``.

``pylovo-mv delete`` deletes variants with all their results. ``pylovo-delete networks --plz PLZ
--version VERSION`` also deletes the MV variants of that version that contain the postcode, because
an MV grid spans several postcodes. ``pylovo-delete --version VERSION`` deletes everything of the
version, the MV variants included. The routing graphs are kept, because other variants may use
them.

Other commands
--------------

pylovo-mv couple
~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv couple --version VERSION --variant VARIANT [--grid GRIDS [GRIDS ...]] [--jobs JOBS] [--no-merged]

Checks the voltage budget of the done supply areas with the stored LV nets and solves the merged MV
and LV operating point (:ref:`mv-coupling`). ``--grid`` selects MV grids (default: all done ones),
``--jobs`` sets the workers (default 2, about 1 GB of memory each) and ``--no-merged`` runs only the
design-point check. The results are the ``coupled_*`` columns of ``mv_node``,
``mv_grid.coupled_status`` (``done``, ``partial`` or ``failed``) and ``mv_grid.metrics['coupling']``.
``couple`` takes the same lock as ``generate`` and can be run again at any time. For load-only
variants, it checks the LV design point and the merged load case; feed-in checks are not requested
and their empty results are not counted as failures.

pylovo-mv list
~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv list [--version VERSION]

One line per variant: generation status, number of postcodes, grids done and failed, electrical
pass/violation/unchecked counts, stored line length, the stale flag and the comment. The check mark
describes generation completion; it does not certify electrical compliance. Non-converging power flows
count among electrical failures. Electrical acceptance refers to the cases requested by that variant.
Circuit lengths count parallel circuits individually; route lengths count each modeled line once.

pylovo-mv delete
~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv delete --version VERSION [--variant VARIANTS [VARIANTS ...]]

Deletes the given variants of a version with all their results. Without ``--variant`` it deletes
every MV variant of the version.

pylovo-mv export
~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv export --version VERSION --variant VARIANT --grid GRID --output OUTPUT
                    [--case {load,n1a,n1b,feed_in_wind,feed_in_pv}] [--coupled]

Writes the stored pandapower net of one MV grid to a JSON file. ``load`` (default) is the stored
net with the HV side, the fixed OLTC tap and the results of the load case. Any other case is
applied to a copy and solved before writing. ``--coupled`` writes the merged MV and LV net of the
supply area instead.

The coupled JSON is a single pandapower network and can be solved with the standard API:

.. code-block:: bash

   uv run pylovo-mv export --version VERSION --variant VARIANT --grid GRID_ID \
       --coupled --output mv_lv.json

.. code-block:: python

   import pandapower as pp

   net = pp.from_json("mv_lv.json")
   pp.runpp(net)
   print(net.converged)

The merge removes the aggregate MV station loads and the LV external grids, then connects the
LV transformers at their assigned MV stations. It preserves the original stored LV networks.
Check coupling diagnostics and voltage/loading results as well as convergence: an exported model
can contain LV grids whose design-point check failed.

The coupled export uses an ideal source at the MV busbar and omits the upstream HV side and
HV/MV transformers. Attaching an HV network therefore needs an explicit transformer boundary,
consistent source controls and reconciliation of the HV aggregate demand with the MV/LV demand.
Use original physical bus identities before simplification or clustering. For example, the
`PyPSA-Eur prebuilt OSM v0.7 network <https://zenodo.org/records/18619025>`_ covers 220–750 kV;
its German buses do not provide a 110 kV attachment. The
`eGon HV model <https://egon-data.readthedocs.io/en/latest/data.html>`_ includes 110 kV, but its
release, scenario and bus identities must still be checked against the selected substations.

pylovo-mv boundary
~~~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv boundary --version VERSION --variant VARIANT --output OUTPUT

Writes the HV boundary of a variant to a CSV file, one row per supplying HV/MV substation, for a
coupling with the eTraGo grid model of eGon-data. ``etrago_bus_id`` is the ``egon_bus_id`` of the
substation. In eGon-data the bus id of an HV/MV substation is the id of its 110 kV bus in eTraGo.
In the test regions, all 37 matched sites had an eTraGo 110 kV bus with the same id within 55 m
(scenario eGon2035). A site without an eGon match has no bus (flag ``no_egon_bus``). ``p_mw``
and ``q_mvar`` are the load case at the MV busbar, the coincident district peaks plus the MV
losses, so they can replace eTraGo's load at that bus. The file also lists the HV/MV
transformers, their loading in normal operation and with one unit out, and the position (WGS84).
In the stored net of every grid, the HV bus carries the same id in the column
``etrago_bus_id``.

pylovo-mv display
~~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv display --version VERSION --variant VARIANT [VARIANT ...]

Stores the display geometry of variants again (see `Display geometry`_), for example for
variants built before it existed. ``generate`` stores it at the end of every run.

pylovo-mv graph
~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv graph --version VERSION (--plz PLZ [PLZ ...] | --ags AGS [AGS ...] | --all) [--qa]

Builds the routing graph of the planning region of the given postcodes, or reuses a stored one, and
prints its id. ``--qa`` prints the quality tables: a summary (edges, vertices, components, share of
the largest component, settlement share), the length per street class, the components, the vertex
degrees and the settlements. ``generate`` builds the graph by itself, so this command is only
needed to inspect a graph.

Results
-------

Tables
~~~~~~

All MV tables are in the schema ``pylovo`` (:doc:`../concepts/database_tables` lists every column).
Every result table carries ``version_id`` and ``mv_variant_id`` and is deleted with its
``mv_variant`` row.

.. list-table::
   :header-rows: 1
   :widths: 26 74
   :class: fixed-table

   * - Table
     - Content
   * - ``mv_variant``
     - One row per MV variant: parameter snapshot, postcodes, routing graph, status (``running``,
       ``done``, ``partial``, ``failed``), LV fingerprint and the run summary (``summary``, with the
       ties and the demand anchor).
   * - ``mv_substation``
     - HV/MV substations with busbar point, footprint, OSM tags, eGon match, sizing (number and
       rating of the transformers), peak load and feed-in, loading, flags and the busbar circle
       of the display geometry (``display_geom``).
   * - ``mv_grid``
     - One MV grid per supply area: status, nominal voltage, polygon and area, ``complete``,
       HV/MV transformers and OLTC tap, counts, circuit, cable, overhead and trench length, voltage
       and loading extremes per case, band conflicts, ``metrics``, ``repairs`` and the stored
       pandapower net (``net``).
   * - ``mv_node``
     - Busbars, stations, MV customers, MV generators and joints with their loads, connection,
       ring, feeder and position, MV voltages per case, voltage budget, MV/LV tap, coupling results
       and flags.
   * - ``mv_line``
     - Lines with role (``ring``, ``stub``, ``dedicated``, ``generator``, ``tie``, ``link``), kind
       (cable or overhead), standard type, parallel cables, lengths, normally-open flag, loadings per
       case and the display geometry (``display_geom``).
   * - ``mv_ring``
     - One row per ring, dedicated feeder, generator feeder or busbar radial: members, lengths of
       the ring, the halves and the stubs, coincident loads, open point, drops and loadings.
   * - ``mv_variant_stats``
     - View with the sums per variant (grids by status, stations, rings, lengths, cable share, km per
       supply point, peak load, band conflicts).
   * - ``mv_graph``, ``mv_graph_vertex``, ``mv_graph_edge``, ``mv_graph_settlement``
     - Routing graphs with settlement flags, shared by all variants with the same inputs.
   * - ``mv_customers``
     - MV customers written by the LV stage (per LV version).
   * - ``mastr_units``, ``mastr_postcode_aggregates``
     - MaStR import (not versioned).

The column ``connection`` of ``mv_node`` gives the result of the planning: ``busbar``, ``ring``,
``stub``, ``dedicated``, ``tee`` (T-off generator), ``loop_in`` (looped-in generator), ``joint`` or
``excluded`` (HV customers and nodes that could not be connected). ``connection_hint`` keeps the
class decided before the planning. ``mv_grid.circuit_km``, ``cable_km`` and ``overhead_km`` count
installed parallel circuits individually, including normally-open sections and excluding interdistrict
ties. ``mv_grid.metrics['route_km']`` counts each modeled line once; it is not the union of shared
trenches. ``mv_ring`` lengths and feeder/path distances remain unweighted geometric lengths.
Regional inventory includes the installed ties and clips each circuit to the region.

Display geometry
~~~~~~~~~~~~~~~~

Lines that run along the same streets have the same geometry, so a map draws them on top of each
other. At the end of every run the stage stores a display geometry for maps
(:mod:`pylovo.mv_grid.display`); ``geom`` and every length stay as they are:

* ``mv_line.display_geom`` shifts the lines sideways into lanes where they share a route: 2 m
  apart, the band at most 12 m wide (more lines share it with a smaller spacing). A line lies on
  the side where it leaves the bundle, and consecutive lines of one ring, stub or feeder keep
  their lane through their stations. A line changes lane over 10 m.
* ``mv_substation.display_geom`` is the busbar circle: half the widest band at the busbar plus
  2 m, at least 6 m. Every line from the substation ends on it, whatever its direction.
* Other line ends bend into their node point. Lines that need no change keep ``display_geom``
  NULL, so maps draw ``COALESCE(display_geom, geom)``.

Flags
~~~~~

``mv_node.flags`` and ``mv_substation.flags`` mark the special cases of the method. The most
important ones:

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Flag
     - Meaning
   * - ``hall_one_storey``
     - MV customer whose load counts one storey (hall rule).
   * - ``busbar_customers``
     - Station with station-busbar customers (``load_stats['busbar_customers']``).
   * - ``station_busbar_overload``
     - Station upsized in the MV model for its busbar customers.
   * - ``remote_small_customer``
     - MV customer below 200 kW that keeps an MV node, because no station lies within 1 km.
   * - ``hv_customer``
     - Customer above 20 MW coincident, not modelled.
   * - ``transfer_point``
     - MV customer moved to its street transfer point.
   * - ``mapped_station``
     - MV customer placed at a transformer candidate inside its building.
   * - ``lv_not_converged``, ``lv_voltage_violation``
     - Station whose LV grid did not converge or violates the voltage band.
   * - ``location_split``
     - Generator node of a MaStR location whose units lie more than 1 km apart.
   * - ``not_n1_secure``
     - Station or customer without an (n-1) reserve (stub, T-off, busbar radial).
   * - ``band_conflict``, ``tap_limit``
     - No MV/LV tap keeps the LV busbar inside both bounds, or not the lower one
       (:ref:`mv-voltage-budget`).
   * - ``no_lv_reference``, ``no_lv_requirement``
     - No voltage budget: the LV version has no ``LV_REFERENCE_VOLTAGE_PU``, or the LV net has no
       stored requirement.
   * - ``dedicated_overload``
     - Customer or generator whose dedicated feeder would need more than ``max_parallel_cables``
       parallel cables.
   * - ``loop_in_infeasible``, ``tee_fallback``, ``no_line_within_tee_limit``
     - Generator that could not be looped in, or got a dedicated feeder because no line was within
       reach.
   * - ``unsupplied``, ``unconnected``
     - Node without a voltage in the load case, or without any line.
   * - ``ehv_uncertain``, ``long_attach``, ``over_capacity``, ``no_nodes``
     - Substation flags: extra-high-voltage site without an HV/MV partner, busbar too far from the
       streets, load above the (n-1) capacity (more transformer units), no node assigned.

SQL examples
~~~~~~~~~~~~

Variants of an LV version:

.. code-block:: sql

   SELECT mv_variant_id, n_grids, n_done, n_converged, n_stations, n_customers, n_rings,
          round(circuit_km::numeric, 1) AS circuit_km, round(cable_share::numeric, 2) AS cable_share,
          n_band_conflicts
   FROM pylovo.mv_variant_stats
   WHERE version_id = '1'
   ORDER BY mv_variant_id;

Supply areas of one variant:

.. code-block:: sql

   SELECT mv_grid_id, vn_kv, status, power_flow_status, complete, round(area_km2::numeric, 1) AS km2,
          n_stations, n_customers, n_rings, round(circuit_km::numeric, 1) AS circuit_km,
          hv_mv_transformers, hv_mv_transformer_mva,
          round(max_voltage_drop_percent::numeric, 2) AS max_drop_pct,
          round(max_line_loading_n1_percent::numeric, 1) AS max_loading_n1_pct
   FROM pylovo.mv_grid
   WHERE version_id = '1' AND mv_variant_id = 'd0'
   ORDER BY mv_grid_id;

Stations with a band conflict and their voltage budget:

.. code-block:: sql

   SELECT mv_node_id, grid_result_id, area_class, mv_lv_tap_pos,
          round(vm_load_pu::numeric, 3) AS mv_load, round(vm_feed_in_pu::numeric, 3) AS mv_feed_in,
          round(lv_busbar_vm_pu::numeric, 3) AS lv_busbar,
          round(lv_busbar_vm_feed_in_pu::numeric, 3) AS lv_busbar_feed_in
   FROM pylovo.mv_node
   WHERE version_id = '1' AND mv_variant_id = 'd0' AND kind = 'station' AND band_conflict
   ORDER BY mv_grid_id, mv_node_id;

Python readers and plots
~~~~~~~~~~~~~~~~~~~~~~~~

:class:`~pylovo.database.mv_result_mixin.MvResultMixin` reads the results without parsing the
stored nets:

.. code-block:: python

   from pylovo.database.database_client import DatabaseClient

   with DatabaseClient() as dbc:
       layers = dbc.read_mv_layers("1", "d0")      # GeoDataFrames: nodes, lines, substations, grids, settlements
       rings = dbc.read_mv_rings("1", "d0")
       stats = dbc.read_variant_stats("1")         # the view mv_variant_stats
       net = dbc.read_mv_grid_net(12)              # stored pandapower net of MV grid 12

:mod:`pylovo.plotting.mv_grid` draws these layers (extra ``plots``): maps of a variant, one grid,
one ring, LV busbar voltages and variants side by side, and statistics such as metric distributions
with literature ranges, voltage profiles, line loadings, voltage budgets and MV/LV taps:

.. code-block:: python

   from pylovo.plotting.mv_grid import plot_mv_grid_map, plot_mv_overview

   fig = plot_mv_overview(layers)
   fig = plot_mv_grid_map(layers, 12, color_by="voltage")   # ring, kind, loading, voltage or tap

Notebooks
~~~~~~~~~

``notebook_tutorials/mv_grids/`` contains five notebooks that read the MV variants of an LV version
(extra ``notebooks``): how to run the stage, an overview of the results, the comparison of
variants, the plausibility against literature ranges and the results of ``pylovo-mv couple``.
:doc:`../getting_started/tutorials` lists them, and ``notebook_tutorials/mv_grids/README.md``
explains their parameters and prerequisites.

.. _mv-parameters:

Parameter reference
-------------------

``MV_*`` keys
~~~~~~~~~~~~~

The ``MV_*`` keys are the last section of ``config_generation.yaml``. A missing key takes its shipped
value, so older configuration files keep working.

.. list-table::
   :header-rows: 1
   :widths: 30 12 58
   :class: fixed-table

   * - Key
     - Shipped value
     - Meaning and source
   * - ``MV_GRID_GENERATION``
     - ``False``
     - Run the MV stage after the LV grids in ``pylovo-generate`` (``--mv`` and ``--no-mv``
       override it). An execution flag, stored in no snapshot.
   * - ``MV_VARIANT_ID``
     - ``"1"``
     - MV variant of the LV version (1 to 10 characters). A changed MV parameter needs a new id.
   * - ``MV_VARIANT_COMMENT``
     - ``default``
     - Comment stored with a new MV variant.
   * - ``MV_INCLUDE_GENERATION``
     - ``False``
     - Opt into MaStR generator connections, LV generation and feed-in cases. The default plans
       peak load and N-1 only; no MaStR query or generator-driven feeder is required.
   * - ``MV_NOMINAL_VOLTAGE_KV``
     - ``auto``
     - ``auto``: 10 kV for supply areas with at least ``mv_10kv_min_density_mva_km2`` of installed
       MV/LV capacity per km², else 20 kV (§ 23c EnWG structural data; Harnisch et al. 2016).
       ``20`` or ``10``: one voltage for all supply areas.
   * - ``MV_SUBSTATIONS``
     - ``osm``
     - HV/MV substations from OpenStreetMap (Hülk et al. 2017), or ``egon`` from eGon-data.
   * - ``MV_SUPPLY_AREAS``
     - ``routed``
     - The nearest substation along the streets; ``municipal``: whole municipalities on their own
       substation, split among several, else on the substation nearest to most of their load (Hülk
       et al. 2017, as Ding0); ``egon``: the eGon-data MV grid districts (needs
       ``MV_SUBSTATIONS: egon``).
   * - ``MV_ROUTING``
     - ``street``
     - Lines along streets and field paths, or ``crowfly``: straight lines × ``crowfly_detour``
       (Ding0).
   * - ``MV_RING_SOLVER``
     - ``savings``
     - Clarke-Wright savings with local search (Clarke and Wright 1964), or ``sweep``.
   * - ``MV_MAX_STATIONS_PER_RING``
     - ``20``
     - Most ring members (stations, MV customers, looped-in generators) of one ring, 1 to 100 (Ding0;
       5 to 30 in a large-city grid, Kreutmayr 2023).
   * - ``MV_STUBS``
     - ``rural``
     - Isolated rural stations (satellites) and small rural loads off the ring hang on a T-off (at
       most 1 MVA; 4 km up to 0.5 MVA, 2 km at 1 MVA), or ``none``: every node is looped in (Amme et
       al. 2018; Harnisch et al. 2016; Tran et al. 2019).
   * - ``MV_OVERHEAD_LINES``
     - ``osm``
     - Overhead lines only along the MV overhead lines mapped in OpenStreetMap, the rest cable;
       ``rural``: links between rural settlements are straight overhead spans (Meinecke et al.
       2019); ``none``: cables only.
   * - ``MV_INTERCONNECTION``
     - ``ties``
     - Normally-open ties to rings of neighbouring substations within ``tie_max_length_m`` and
       normally-open links inside the supply area of one substation within ``link_max_length_m``, or
       ``none`` (Weisenstein and Wellßow 2021; Dubielzig 2022).
   * - ``MV_STANDARD_CABLE``
     - ``null``
     - A pandapower cable type. ``null`` selects NA2XS2Y 1x240 RM/25 for the voltage.
       Explicit types and frozen historical equipment remain authoritative.
   * - ``MV_OVERHEAD_LINE``
     - ``null``
     - A pandapower overhead line type. ``null`` selects 122-AL1/20-ST1A for the voltage.
   * - ``MV_LINE_PLANNING_UTILIZATION``
     - ``0.6``
     - Planned current per ampacity of MV lines in normal operation, 0.1 to 1.0 (dena 2012;
       Harnisch et al. 2016; Wintzek et al. 2021; SimBench uses 0.5).
   * - ``MV_MAX_VOLTAGE_DROP_PERCENT``
     - ``5``
     - MV share of the voltage band in normal operation, from the busbar to every node, 0.5 to 20
       (Harnisch et al. 2016; SimBench; Ding0).

Constants
~~~~~~~~~

The constants are no YAML keys. They are stored in the snapshot of every variant and can be changed
per variant with ``--set name=value`` or :meth:`~pylovo.mv_grid.parameters.MvParameters.replace_raw`.
:data:`pylovo.mv_grid.options.CONSTANTS` defines them with their bounds. The constants of the
routing graph and the settlements are part of the graph id, so a change builds a new graph.

.. list-table:: Voltage and loading
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``busbar_vm_pu``
     - 1.02
     - OLTC setpoint of the MV busbar in p.u. (Harnisch et al. 2016: 102.5 ± 1.5 %; Wintzek et al.
       2021: 102 ± 2 %).
   * - ``busbar_band_pu``
     - 0.015
     - Control band of the OLTC. The voltage budget puts the busbar at its edges (Harnisch et al.
       2016).
   * - ``max_drop_n1_percent``
     - 10.0
     - Voltage drop limit in (n-1) operation (Ding0; SimBench).
   * - ``max_rise_percent``
     - 2.0
     - Voltage rise limit in the feed-in cases (VDE-AR-N 4110, dena 2012).
   * - ``mv_10kv_min_density_mva_km2``
     - 8.0
     - Installed MV/LV capacity per km² from which ``auto`` gives 10 kV (§ 23c EnWG structural data,
       not calibrated on supply areas).
   * - ``n1_utilization``
     - 1.0
     - Line loading limit in the (n-1) and feed-in cases (dena 2012).
   * - ``hv_mv_n1_utilization``
     - 1.0
     - Loading of the HV/MV transformers with one unit out, for their sizing and the capacity of the
       supply areas (dena 2012).

.. list-table:: Topology and solver
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``max_half_ring_length_m``
     - 28000
     - Longest half-ring in m (Ding0 configuration; ring length up to 60 km after FGH).
   * - ``rural_ring_factor``
     - 2
     - Rural ring members count 1 / factor against ``MV_MAX_STATIONS_PER_RING``, so rural rings hold
       40 members and urban rings 20 (rural feeders carry 11 to 28 supply points: SimBench;
       Almomani and Witzmann 2024). 1 counts every member 1.
   * - ``stub_max_kva``
     - 1000
     - Largest coincident load of a stub tree in kVA: the size of a typical mobile emergency
       generator (Amme et al. 2018, after LEW and FGH 2008).
   * - ``stub_max_length_m``
     - 4000
     - Longest stub tree in m: the upper end of the 2 to 4 km of load stubs in real grids (Tran et
       al. 2019).
   * - ``stub_max_kva_km``
     - 2000
     - Largest product of stub tree load (kVA) and length (km): 2 km at 1 MVA (Consentec 2006 after
       Amme et al. 2018), 4 km up to 0.5 MVA. A derived rule: the energy not supplied after a stub
       fault grows with both.
   * - ``stub_min_offset_m``
     - 100
     - Only nodes more than this off the ring become stubs (Harnisch et al. 2016; Amme et al. 2018).
   * - ``loop_in_max_offset_m``
     - 100
     - Urban generators within this distance of a ring are looped in (Amme et al. 2018).
   * - ``generator_max_tee_m``
     - 5000
     - Longest generator T-off before a dedicated feeder (assumption).
   * - ``tie_max_length_m``
     - 2000
     - Longest tie to a neighbouring supply area (assumption).
   * - ``link_max_length_m``
     - ``{rural: 1000, semi_urban: 750, urban: 500}``
     - Reach of the normally-open links inside a supply area by the settlement type of the PLZ,
       about three times the distance between neighbouring stations (assumption); a link takes the
       shorter reach of its two ends, and one number applies to every type. Each node takes at most
       one link, two rings at most two (:ref:`mv-links`).
   * - ``link_stub_ends``
     - True
     - Each stub end may also take one link within its reach to a station of another feeder of its
       supply area, a second infeed (:ref:`mv-links`).
   * - ``local_search_rounds``
     - 50
     - Pass limit of the local search, 0 switches it off (Ding0 used 10 fixed rounds).
   * - ``neighbours``
     - 15
     - Neighbour list length of the local search.
   * - ``ls_time_guard_s``
     - 120.0
     - Wall-clock guard of the local search in s (flagged when hit).
   * - ``repair_rounds``
     - 8
     - Rounds of the repair loop (an overhead line needs up to three rounds to reach the largest
       conductor).

.. list-table:: Loads
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``dedicated_feeder_kw``
     - 5500
     - MV customers and generators above this coincident load or power get a dedicated feeder
       (eGon-data MV level up to 5.5 MW).
   * - ``hv_customer_kw``
     - 20000
     - Customers above this coincident load are HV customers and not modelled (eGon-data HV/MV level
       up to 20 MW).
   * - ``customer_hall_min_footprint_m2``
     - 2000
     - Footprint from which an MV customer building counts one storey (hall rule, assumption).
       ``null`` switches the rule off.
   * - ``customer_min_mv_kw``
     - 200
     - Own peak below which an MV customer becomes a station-busbar customer (eGon-data: MV level
       above 200 kW).
   * - ``busbar_customer_max_m``
     - 1000
     - Largest straight-line distance of a station-busbar customer to its station (pylovo's default
       ``MAX_GREENFIELD_TRAFO_DISTANCE``). ``null`` means no limit.

.. list-table:: Generation and cases
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``generator_lv_side_kw``
     - 200
     - MaStR points at the MV/LV level above this power are MV nodes, the others LV side
       (eGon-data).
   * - ``generator_fallback_kw``
     - mapping
     - ``{lv: 100, mvlv: 200, mv: 5500, hvmv: 20000}``: upper power (kW) of each class for MaStR
       points without a voltage level (eGon-data).
   * - ``mv_gen_cos_phi``
     - 0.95
     - Power factor of MV generators, under-excited (assumption after VDE-AR-N 4110 practice).
   * - ``lv_pv_cos_phi``
     - 1.0
     - Power factor of the LV-side generation. 0.95 is a sensitivity for cos φ(P) control after
       VDE-AR-N 4105.
   * - ``feed_in_cases``
     - mapping
     - ``feed_in_wind``: loads 0.1 / 0.123, wind 1.0, PV 0.8, other 1.0. ``feed_in_pv``: loads
       0.1 / 0.123, wind 0.85, PV 0.95, other 1.0 (SimBench study cases lW and lPV).

.. list-table:: Equipment
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``hv_mv_sizes_mva``
     - ``[25, 31.5, 40, 63]``
     - Ratings of the HV/MV transformers in MVA (Meinecke et al. 2019; 31.5 MVA interpolated).
   * - ``hv_mv_units``
     - 2
     - HV/MV transformers per substation (Harnisch et al. 2016).
   * - ``max_parallel_cables``
     - 2
     - Most parallel cables per circuit (dedicated feeders and repairs only).

.. list-table:: Routing graph and settlements
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``crowfly_detour``
     - 1.3
     - Detour factor of straight lines with ``MV_ROUTING: crowfly`` (Ding0, after detour factors of
       1.1 to 1.5).
   * - ``overhead_detour``
     - 1.1
     - Detour factor of straight overhead spans with angle poles (assumption).
   * - ``bridge_detour``
     - 1.3
     - Detour factor of bridges between graph islands and of long connectors.
   * - ``long_connector_m``
     - 300
     - Connectors from a node to the street above this length count ``bridge_detour`` times and are
       logged.
   * - ``osm_line_snap_m``
     - 100
     - ``MV_OVERHEAD_LINES: osm``: a loose end of an OSM MV line joins the nearest street point
       within this distance by a straight cable connector (assumption).
   * - ``osm_line_cost_factor``
     - 0.7
     - ``MV_OVERHEAD_LINES: osm``: routing cost of an OSM MV line per m of its length, so that routes
       and rings follow the existing corridors (assumption; 1: no preference). Lengths and checks use
       the true lengths.
   * - ``busbar_exit_radius_m``
     - 200
     - The busbar gets straight cable connectors to every street vertex within this distance, so the
       feeders leave the substation in several directions (assumption). 0: only the attachment of
       the substation.
   * - ``graph_excluded_klasse``
     - ``[Bundesautobahn]``
     - Street classes (``klasse``) left out of the graph: no MV lines along motorways.
   * - ``graph_snap_tolerance_m``
     - 0.25
     - Edge ends within this distance are one vertex.
   * - ``subgraph_buffer_m``
     - 2000
     - Buffer of the district graph around the nodes of a supply area.
   * - ``region_buffer_m``
     - 5000
     - Search buffer for substations and for the municipalities of the routing graph.
   * - ``settlement_buffer_m``
     - 50
     - Address buildings within this distance count for a street edge.
   * - ``settlement_spacing_m``
     - 100
     - Street length per address building below which an edge is in a settlement.
   * - ``settlement_min_buildings``
     - 2
     - Fewest address buildings of a settlement edge.
   * - ``urban_min_buildings``
     - 1000
     - Settlements with at least this many address buildings are urban (about a small town).
   * - ``settlement_polygon_buffer_m``
     - 75
     - Buffer of the settlement polygons around their edges (maps).

.. list-table:: Substations and supply areas
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``substation_merge_m``
     - 75
     - Substation candidates closer than this form one site (Hülk et al. 2017).
   * - ``substation_drop_m``
     - 150
     - Of two sites closer than this, the smaller one is dropped.
   * - ``ehv_partner_m``
     - 1000
     - An extra-high-voltage site without MV tag is dropped when an HV/MV site lies within this
       distance.
   * - ``substation_max_attach_m``
     - 2000
     - Longest connection of a substation busbar to the street graph.
   * - ``egon_match_m``
     - 300
     - Distance of the eGon-data cross-check of the OSM sites.
   * - ``rebalance_step_m``
     - 500
     - Weight step of the capacity rebalancing of routed supply areas.
   * - ``rebalance_max_iter``
     - 0
     - Most weight raises of the rebalancing of routed supply areas; 0: none, a substation above
       its (n-1) capacity keeps its load and gets more transformer units.
   * - ``municipal_site_max_offset_m``
     - 250
     - A substation belongs to the municipality of its street edge within this distance (municipal
       supply areas), else to none.

.. list-table:: MV/LV taps
   :header-rows: 1
   :widths: 32 12 56
   :class: fixed-table

   * - Constant
     - Default
     - Meaning and source
   * - ``mvlv_tap_step_percent``
     - 2.5
     - Off-load tap step of the MV/LV transformers (pandapower and SimBench types).
   * - ``mvlv_tap_min``, ``mvlv_tap_max``
     - -2, 2
     - Lowest and highest MV/LV tap position.
   * - ``lv_design_tolerance_pu``
     - 0.002
     - Tolerance of the LV busbar below ``LV_REFERENCE_VOLTAGE_PU`` of the LV version.
   * - ``lv_feed_in_max_pu``
     - 1.07
     - Highest LV busbar voltage in feed-in: 1.10 minus the 3 % LV share of VDE-AR-N 4105.

.. _mv-troubleshooting:

Troubleshooting
---------------

``PyLovo schema has pending migrations (0009_mv_grids); run pylovo-setup first``
   Run the non-destructive ``pylovo-setup`` once (:ref:`mv-schema`).
``none of the requested PLZ has LV grids in version <version>``
   Generate the LV grids first, or check ``--version``. ``pylovo-mv`` never uses ``VERSION_ID``.
``PLZ ... of version <version> are being generated right now``
   Wait until ``pylovo-generate`` has finished these postcodes.
``another MV run of variant <id> of version <version> is active``
   A ``generate`` or ``couple`` of the same variant is running.
``MV parameters differ from the stored snapshot of MV variant <id> ...``
   The variant exists with other parameters: use another ``--variant``, or delete it
   (``pylovo-mv delete`` or ``--replace``).
``MV_SUPPLY_AREAS egon needs MV_SUBSTATIONS egon ...``
   The eGon-data districts belong to the eGon-data substations, so set both.
``no HV/MV substations within 5 km of the region; check INFDB_OSM_SCHEMA or use MV_SUBSTATIONS=egon``
   The OSM schema is missing or empty, or the region has no substation nearby (:ref:`mv-osm-data`).
``the MV stage needs LV grids of the pandapower backend``
   The LV version was generated with another electrical backend.
``MV_STANDARD_CABLE must name a pandapower cable line type for 20 kV, got ...``
   The configured type does not exist or does not fit the kind or the voltage. For an unknown name
   the message lists the closest pandapower types.
``the MV worker pool broke (...; out of memory?)``
   Run the same command again with fewer ``--jobs``. Done supply areas are kept.
A variant ends ``partial``
   Some supply areas failed. Their ``mv_grid.error`` and the worker logs
   ``log/log_mv_<version>_<variant>_<mv_grid_id>.txt`` give the reason. Running the same command
   again plans only the failed and pending supply areas.
