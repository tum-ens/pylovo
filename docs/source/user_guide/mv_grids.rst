MV grids
========

The MV stage adds medium-voltage (MV) grids to a generated LV version: open rings from the HV/MV
substations of the region to the stations of the LV grids and the MV customers
(:doc:`../concepts/mv_grids`). This page shows how to prepare its inputs, run it, compare MV
variants and read the results. :doc:`mv_parameters` lists every parameter. The stage runs on the
command line and from Python. The HTTP API and the GridPlanner UI do not offer it yet.

The stage plans for the peak load and the (n-1) cases, like the LV-first workflow. It uses
NA2XS2Y 1x240 cables and 122-AL1 overhead conductors. It plans no generation, so its results say
nothing about the feed-in that a grid can take.

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
   * - Migrations ``0009_mv_grids`` and ``0010_mv_display_geometry``
     - The MV tables and the display geometry of MV lines and substations
     - ``pylovo-setup`` (:ref:`mv-schema`)
   * - OSM power data in InfDB
     - HV/MV substations, MV overhead lines, built-up land use
     - Schema ``INFDB_OSM_SCHEMA`` (:ref:`mv-osm-data`)
   * - Regional street graph of InfDB
     - Routing graph and settlements
     - ``<INFDB_SOURCE_SCHEMA>.ways_per_junction`` and ``.buildings``
   * - eGon-data HV/MV substations (optional)
     - The eTraGo 110 kV bus of every substation (``pylovo-mv boundary``)
     - ``egon_hvmv_substation`` (:ref:`mv-egon-data`)
   * - Municipal register
     - Population of the demand anchor in the run summary
     - Loaded by ``pylovo-setup``

The stage needs ``USE_INFDB=True``: it reads the InfDB schemas in the pylovo database
(:doc:`input_data`). ``USE_EXTERNAL_BASEDATA`` with the InfDB in another database is not supported
yet: ``pylovo-generate --mv``, ``pylovo-mv generate`` and ``pylovo-mv graph`` stop before they
start. Run the MV stage on a database that holds both the InfDB schemas and the schema ``pylovo``.

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

The MV tables are created by the migrations ``0009_mv_grids`` and ``0010_mv_display_geometry``
(:doc:`../concepts/database_tables`). A new schema gets them with ``pylovo-setup``. A schema set up
before needs one run of the non-destructive setup, which keeps all grids:

.. code-block:: bash

   uv run pylovo-setup

Until every migration is recorded, ``pylovo-generate`` and the ``pylovo-mv`` commands that read the MV
tables stop with ``PyLovo schema has pending migrations (0009_mv_grids, 0010_mv_display_geometry, ...);
run pylovo-setup first``. The message lists all pending migrations, also those of the LV stage. Run the
setup when no generation is running, because the new foreign keys to ``grid_result`` wait for running
transactions.

.. _mv-osm-data:

OSM power data
~~~~~~~~~~~~~~

The HV/MV substations and the MV overhead lines come from the OpenStreetMap layers that the InfDB
OSM importer writes into the schema ``INFDB_OSM_SCHEMA`` of ``.env`` (default ``osm``,
:ref:`env-variables`):

.. list-table::
   :header-rows: 1
   :widths: 36 64
   :class: fixed-table

   * - Table
     - Used for
   * - ``infrastructure_point``, ``infrastructure_polygon``
     - Features with ``osm_type = 'power'``: substations (``osm_subtype = 'substation'``), plants
       and generators (excluded substations) and transformers.
   * - ``infrastructure_line`` (optional)
     - The MV overhead lines (``line`` and ``minor_line``) of the routing graph. Without it every
       MV line is a cable.
   * - ``tags``
     - The OSM tags (``voltage``, ``substation``, ``frequency``, ``operator``, ``name``,
       ``railway`` and the tags of the lines) as ``jsonb``, joined by ``geom_type`` and ``osm_id``.
   * - ``landuse_polygon`` (optional)
     - Built-up land use (residential, commercial, industrial, retail) of the settlement flags.
       Without it the settlements come from address buildings only (warning).

Without OSM substations near the region, the stage stops.

.. _mv-egon-data:

eGon-data (optional)
~~~~~~~~~~~~~~~~~~~~

The eGon-data HV/MV substations (``egon_hvmv_substation``: ``bus_id``, ``voltage``, ``osm_id``,
``geom``) are read from the schema ``pylovo`` if the table exists there and holds rows, else from
``INFDB_OPENDATA_SCHEMA``. Every OSM site gets the bus id of the nearest one within
``egon_match_m`` (300 m), which is the id of its 110 kV bus in the eTraGo grid model
(``pylovo-mv boundary``). Without the table the stage runs, the match is skipped (warning) and no
substation has a bus. pylovo has no importer for this table yet. Copy it from the eGon-data
database or the Open Energy Platform.

The eGon substations are processed OSM-derived records. Keep the source release and the typed OSM
identifiers when comparing them with another grid dataset. The stored ``egon_bus_id`` of an OSM
site is a spatial match; it does not establish an electrical connection to an external HV network.
Its distance is measured to the station footprint where available, otherwise to its point.

Quick start
-----------

.. code-block:: bash

   uv run pylovo-setup                                   # once: the migrations of the MV stage
   uv run pylovo-generate --plz 80803 80802 --mv         # LV grids, then the MV grids
   uv run pylovo-mv list --version 1                     # the MV variants of version 1

``pylovo-generate --mv`` runs the MV stage for the MV variant of the configuration
(``MV_VARIANT_ID``, default ``1``) of ``VERSION_ID``. Further variants of the same LV version are
built with ``pylovo-mv generate`` (:ref:`mv-variants`).

Running the MV stage
--------------------

After the LV grids
~~~~~~~~~~~~~~~~~~

With ``MV_GRID_GENERATION: True`` in ``config_generation_mv.yaml``, or with ``--mv``,
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
   ``MV_*`` keys of this variant that differ from the configuration (:ref:`mv-variants`).
``--parallel`` / ``--no-parallel``
   Plan the supply areas in worker processes or one after the other in the main process (default
   ``PARALLEL``).
``--jobs JOBS``
   Number of workers (default the smaller of 4 and ``N_JOBS``). The stage lowers it so that about
   0.6 GB of free memory per worker and 2 GB for the rest of the machine remain.
``--replace``
   Delete the variant first and build it again.

.. code-block:: bash

   uv run pylovo-mv generate --version 1 --all --variant r15 --comment "15 ring members" \
       --set MV_MAX_STATIONS_PER_RING=15
   uv run pylovo-mv generate --version 1 --ags 09162000 --jobs 2

Every output line starts with ✓ or ✗. The first line names the database, the version and the
variant. The summary at the end has this form (``<...>`` stands for the values of the run):

.. code-block:: text

   ✓ MV stage on <DBNAME>@<HOST>:<PORT>, version 1, variant r15
   ✓ MV variant r15 of version 1: <n> grids (<n> done), <n> stations, <n> customers, <km> km (cable <share> %), <n> grids with violations, <n> band conflicts, <s> s
   ✓ Nominal voltage auto (10 kV from 5 MVA/km² installed): <n> at 20 kV
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

``plz_list=None`` takes every postcode with LV grids of the version.
:class:`~pylovo.mv_grid.parameters.MvParameters` checks every value on construction. ``replace``
takes field names, ``replace_raw`` the ``MV_*`` keys and the constant names. The constants of the
stage are changed this way only (:doc:`mv_parameters`).

.. _mv-variants:

MV variants
-----------

An MV variant is one set of MV parameters for one LV version, identified by
``(version_id, mv_variant_id)``. The first run stores the parameters once in
``mv_variant.mv_parameters``: every ``MV_*`` key, every constant, the resolved line and HV/MV
transformer types and the version of the stage code (``mv_stage_version``). The ``MV_*`` keys are
not part of the snapshot of the LV version, so MV variants never require a new ``VERSION_ID``.

``--set KEY=VALUE`` changes a parameter for one variant. KEY is an ``MV_*`` key of
``config_generation_mv.yaml`` (except ``MV_GRID_GENERATION``, ``MV_VARIANT_ID`` and
``MV_VARIANT_COMMENT``). VALUE is read as YAML (``null``, numbers, text) and checked against the
rules of the key. A value with spaces needs quotes in the shell. The constants of
:doc:`mv_parameters` are changed in Python with ``MvParameters.replace``.

.. list-table:: Examples of variants
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Question
     - ``--set``
   * - Smaller rings
     - ``MV_MAX_STATIONS_PER_RING=15``
   * - SimBench line utilisation
     - ``MV_LINE_PLANNING_UTILIZATION=0.5``
   * - One voltage for all supply areas
     - ``MV_NOMINAL_VOLTAGE_KV=20``
   * - A narrower standard cable
     - ``"MV_STANDARD_CABLE=NA2XS2Y 1x150 RM/25 12/20 kV"``
   * - A smaller MV share of the voltage band
     - ``MV_MAX_VOLTAGE_DROP_PERCENT=4``

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

A variant planned by an earlier version of the stage with an option that the stage no longer has
(for example another ring solver, other supply areas or generators with feed-in cases) is
*retired*. ``pylovo-mv list`` names these options. Its results stay readable, and ``pylovo-mv
export`` writes its stored nets, but it cannot be resumed, coupled or exported coupled.

A variant becomes stale when the LV results of its postcodes change after it was built, for example
by load edits, deleted or regenerated grids. ``pylovo-mv list`` compares a fingerprint of the LV
results of its postcodes (number of grids, grid ids, active load edits, number of MV customers) with
the one stored at creation and marks such variants ``stale``. Undoing a load edit changes the active
load edits, so it marks a variant that was built on the edited loads as stale. Rebuild them with
``--replace``.

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
``couple`` takes the same lock as ``generate`` and can be run again at any time. It refuses a
retired variant.

pylovo-mv list
~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv list [--version VERSION]

One line per variant: generation status, number of postcodes, grids done and failed, the retired
options if any, electrical pass/violation/unchecked counts, stored line length, the stale flag and
the comment. The check mark (✓) means that the generation is done and the variant is not stale (an
unknown stale flag keeps it); it does not certify electrical compliance. Non-converging power flows
count among electrical failures. Circuit lengths count
parallel circuits individually; route lengths count each modeled line once.

pylovo-mv delete
~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv delete --version VERSION [--variant VARIANTS [VARIANTS ...]]

Deletes the given variants of a version with all their results. Without ``--variant`` it deletes
every MV variant of the version. It refuses while a ``generate`` or ``couple`` of one of them runs.

pylovo-mv export
~~~~~~~~~~~~~~~~

.. code-block:: text

   pylovo-mv export --version VERSION --variant VARIANT --grid GRID --output OUTPUT
                    [--case {load,n1a,n1b}] [--coupled]

Writes the stored pandapower net of one MV grid to a JSON file. ``load`` (default) is the stored
net with the HV side, the fixed OLTC tap and the results of the load case. ``n1a`` and ``n1b`` are
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
       ties, the links and the demand anchor).
   * - ``mv_substation``
     - HV/MV substations with busbar point, footprint, OSM tags, eGon match, sizing (number and
       rating of the transformers), peak load, loading, flags and the busbar circle of the display
       geometry (``display_geom``).
   * - ``mv_grid``
     - One MV grid per supply area: status, nominal voltage, polygon and area, ``complete``,
       HV/MV transformers and OLTC tap, counts, circuit, cable, overhead and trench length, voltage
       and loading extremes per case, band conflicts, ``metrics``, ``repairs`` and the stored
       pandapower net (``net``).
   * - ``mv_node``
     - Busbars, stations, MV customers and joints with their loads, connection, ring, feeder and
       position, MV voltages per case, voltage budget, MV/LV tap, coupling results and flags.
   * - ``mv_line``
     - Lines with role (``ring``, ``stub``, ``dedicated``, ``tie``, ``link``), kind (cable, overhead
       or mixed), standard type, parallel cables, lengths, normally-open flag, loadings per case and
       the display geometry (``display_geom``).
   * - ``mv_ring``
     - One row per ring, dedicated feeder or busbar radial: members, lengths of the ring, the halves
       and the stubs, coincident loads, open point, drops and loadings.
   * - ``mv_variant_stats``
     - View with the sums per variant (grids by status, stations, rings, lengths, cable share, km per
       supply point, peak load, band conflicts).
   * - ``mv_graph``, ``mv_graph_vertex``, ``mv_graph_edge``, ``mv_graph_settlement``
     - Routing graphs with settlement flags, shared by all variants with the same inputs.
   * - ``mv_customers``
     - MV customers written by the LV stage (per LV version).

The columns about generation (``gen_*_kw``, the ``*feed_in*`` columns, ``n_generators``, the node
kind and line role ``generator``) hold values only in retired variants planned with generators. The
stage writes 0 or NULL there.

The column ``connection`` of ``mv_node`` gives the result of the planning: ``busbar``, ``ring``,
``stub`` (also the node of a busbar radial), ``dedicated``, ``joint`` or ``excluded`` (HV customers
and nodes that could not be connected). ``connection_hint`` keeps the class decided before the
planning. ``mv_grid.circuit_km``, ``cable_km`` and ``overhead_km`` count installed parallel circuits
individually, including normally-open sections and excluding interdistrict ties.
``mv_grid.metrics['route_km']`` counts each modeled line once; it is not the union of shared
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
   * - ``satellite_in_ring``, ``satellite_own_ring``
     - Satellite that fits no stub and went into a ring, or into a ring of its own.
   * - ``not_n1_secure``
     - Station or customer without an (n-1) reserve (stub, busbar radial).
   * - ``band_conflict``, ``tap_limit``
     - No MV/LV tap brings the LV busbar up to its reference (:ref:`mv-voltage-budget`).
   * - ``no_lv_reference``, ``no_lv_requirement``
     - No voltage budget: the LV version has no ``LV_REFERENCE_VOLTAGE_PU``, or the LV net has no
       stored requirement.
   * - ``dedicated_overload``
     - Customer whose dedicated feeder would need more than ``max_parallel_cables`` parallel cables.
   * - ``unsupplied``, ``unconnected``
     - Node without a voltage in the load case, or without any line.
   * - ``ehv_uncertain``, ``long_attach``, ``over_capacity``, ``no_nodes``, ``no_egon_bus``
     - Substation flags: extra-high-voltage site without an HV/MV partner, busbar too far from the
       streets, load above the (n-1) capacity (more transformer units), no node assigned, no
       eGon-data substation (eTraGo bus) within reach.

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
   WHERE version_id = '1' AND mv_variant_id = 'r15'
   ORDER BY mv_grid_id;

Stations with a band conflict and their voltage budget:

.. code-block:: sql

   SELECT mv_node_id, grid_result_id, area_class, mv_lv_tap_pos,
          round(vm_load_pu::numeric, 3) AS mv_load, round(vm_n1_pu::numeric, 3) AS mv_n1,
          round(lv_busbar_vm_pu::numeric, 3) AS lv_busbar,
          round(lv_min_vm_pu::numeric, 3) AS lv_min
   FROM pylovo.mv_node
   WHERE version_id = '1' AND mv_variant_id = 'r15' AND kind = 'station' AND band_conflict
   ORDER BY mv_grid_id, mv_node_id;

Python readers and plots
~~~~~~~~~~~~~~~~~~~~~~~~

:class:`~pylovo.database.mv_result_mixin.MvResultMixin` reads the results without parsing the
stored nets:

.. code-block:: python

   from pylovo.database.database_client import DatabaseClient

   with DatabaseClient() as dbc:
       layers = dbc.read_mv_layers("1", "r15")     # GeoDataFrames: nodes, lines, substations, grids, settlements
       rings = dbc.read_mv_rings("1", "r15")
       stats = dbc.read_variant_stats("1")         # the view mv_variant_stats
       net = dbc.read_mv_grid_net(12)              # stored pandapower net of MV grid 12

:mod:`pylovo.plotting.mv_grid` draws these layers (extra ``plots``): maps of a variant, one grid,
one ring, LV busbar voltages and variants side by side, and statistics such as metric distributions
with literature ranges, voltage profiles, line loadings, voltage budgets and MV/LV taps:

.. code-block:: python

   from pylovo.plotting.mv_grid import plot_mv_grid_map, plot_mv_overview

   fig = plot_mv_overview(layers)
   fig = plot_mv_grid_map(layers, 12, color_by="voltage")   # ring, kind, loading, voltage or tap

:mod:`pylovo.mv_grid.metrics` computes the topology metrics of the stored grids
(``topology_metrics``, ``feeder_table``, ``ring_table``) in a generic table format.
:mod:`pylovo.analysis.mv_validation` holds the research tools on top of it, which the stage itself
does not use: published ranges of MV grid metrics with traffic lights, readers of Ding0 grids and
pandapower nets into the generic tables, metrics of a whole region, Wasserstein distances and the
adjusted Rand index of the ring membership between variants, and the capacitive earth-fault
current.

Notebooks
~~~~~~~~~

``notebook_tutorials/mv_grids/`` contains five notebooks that read the MV variants of an LV version
(extra ``notebooks``): how to run the stage, an overview of the results, the comparison of
variants, the plausibility against literature ranges and the results of ``pylovo-mv couple``.
:doc:`../getting_started/tutorials` lists them, and ``notebook_tutorials/mv_grids/README.md``
explains their parameters and prerequisites.

Parameters
----------

The ``MV_*`` keys of ``config_generation_mv.yaml`` and the constants of the stage, with their
shipped values, meanings and sources, are listed in :doc:`mv_parameters`.

.. _mv-troubleshooting:

Troubleshooting
---------------

``PyLovo schema has pending migrations (0009_mv_grids, 0010_mv_display_geometry, ...); run pylovo-setup first``
   Run the non-destructive ``pylovo-setup`` once (:ref:`mv-schema`).
``MV variant(s) <version>/<variant> are being generated or coupled right now; delete them when the run has finished``
   ``pylovo-mv delete`` and the ``pylovo-delete`` commands refuse to delete a variant, a postcode or a
   version while a ``generate`` or ``couple`` of an affected variant runs. Wait for the run.
``the MV stage reads the InfDB source data on the pylovo connection, but USE_EXTERNAL_BASEDATA points to ...``
   The InfDB schemas are in another database (:ref:`mv-prerequisites`).
``none of the requested PLZ has LV grids in version <version>``
   Generate the LV grids first, or check ``--version``. ``pylovo-mv`` never uses ``VERSION_ID``.
``PLZ ... of version <version> are being generated right now``
   Wait until ``pylovo-generate`` has finished these postcodes.
``another MV run of variant <id> of version <version> is active``
   A ``generate`` or ``couple`` of the same variant is running.
``MV parameters differ from the stored snapshot of MV variant <id> ...``
   The variant exists with other parameters, an older stage version or a retired option: use
   another ``--variant``, or delete it (``pylovo-mv delete`` or ``--replace``).
``MV variant <id> of version <version> was planned with options this code no longer has (...)``
   The variant is retired (:ref:`mv-variants`). Its stored results stay readable, but it cannot be
   coupled. Plan it again under a new variant id.
``<KEY> ... is no longer supported: the MV stage always uses ...``
   The configuration or ``--set`` names an option that the stage no longer has. Remove the key.
``... is a constant of the MV stage, not a key of config_generation_mv.yaml``
   ``--set`` takes only ``MV_*`` keys. Change a constant in Python (:doc:`mv_parameters`).
``no HV/MV substations within 5 km of the region; check INFDB_OSM_SCHEMA``
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
