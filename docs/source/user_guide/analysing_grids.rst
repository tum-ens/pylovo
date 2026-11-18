Analysing grids
===============

Generated grids can be analysed on three levels: the diagnostics stored with every grid, the
key figures computed by ``pylovo-analyze``, and your own analyses on the pandapower nets.

.. _structural-metrics:

Eight structural metrics
------------------------

:mod:`pylovo.analysis.comparison` describes every grid by eight metrics, the definitions of the structural validation
of pylovo against grids of distribution system operators. They form a matrix of two categories (network topology,
electrical sizing) and four parts of the grid (station, feeders, per building, route).
:func:`~pylovo.analysis.comparison.grid_metrics` computes them from the cables and the station of each grid. The
topology is rebuilt from the cable geometries, so cables and stations exported from the GIS of a grid operator work
the same way as pylovo grids. The values describe a grid's structure; they are not a power-flow result.

.. list-table::
   :header-rows: 1
   :widths: 30 14 56
   :class: fixed-table

   * - Metric
     - Unit
     - Meaning
   * - ``building_count``
     - buildings
     - Size of a transformer supply area.
   * - ``feeder_branching_endpoints``
     - count
     - Ends of the feeder network without house connection cables; more means more branched.
   * - ``cable_length_per_building_m``
     - m
     - Installed cable to the houses, parallel cables counted.
   * - ``max_supply_path_m``
     - m
     - Cable distance from the station to the farthest house.
   * - ``transformer_bank_capacity_kva``
     - kVA
     - Rated capacity of the station.
   * - ``feeder_capacity_a``
     - A per feeder
     - Current rating of a cable leaving the station; parallel cables add up.
   * - ``transformer_capacity_per_building_kva``
     - kVA
     - Station capacity divided by its buildings.
   * - ``max_series_resistance_ohm``
     - mΩ
     - Cable resistance from the station to the worst-supplied house.

:func:`~pylovo.analysis.comparison.compare` scores two datasets of grids against each other, metric by metric; the
notebook ``notebook_tutorials/analysis/basic_comparison.ipynb`` shows the workflow.

.. figure:: /images/analysis/synthetic_comparison_metrics.png
   :alt: Histograms of the eight structural metrics for the synthetic grids of one example area
   :width: 100%

   The eight metrics for **synthetic grids only**: version 1 of the semi-urban example area of the tutorials (PLZ
   85256), the 25 of its 26 grids with at least five buildings. The script ``docs/scripts/plot_synthetic_metrics.py``
   reads the example data, no database is needed.

In this example area the median grid supplies 57 buildings from a 400 kVA station over four feeder branch ends. It
has 40 m of cable per building, its longest route is 0.4 km and the highest route resistance is 85 mΩ. These
summaries describe this one postcode area and version; use a broader regional sample for conclusions about a region.

Stored diagnostics
------------------

Every row of ``pylovo.grid_result`` already contains the transformer rating, the power-flow status
of the validation snapshot and the planning and solved voltage drops
(:ref:`stored-diagnostics`). The pandapower tables ``pandapower_bus``, ``pandapower_line``,
``pandapower_trafo`` and ``pandapower_load`` hold every element with its sizing provenance, so many
questions can be answered in SQL:

.. code-block:: sql

   -- installed cable length per type, feeders and service connections
   SELECT pl.std_type,
          pl.service_sizing_basis IS NOT NULL AS service,
          round(sum(pl.length_km * pl.parallel)::numeric, 2) AS km
   FROM pylovo.pandapower_line pl
   JOIN pylovo.grid_result gr USING (grid_result_id)
   WHERE gr.version_id = '1' AND gr.plz = 85653
   GROUP BY 1, 2 ORDER BY 1;

   -- grids whose power flow left the voltage band
   SELECT version_id, plz, kcid, bcid, max_total_lv_voltage_drop_pu
   FROM pylovo.grid_result WHERE power_flow_status <> 'converged';

.. figure:: /images/analysis/cable_length_by_type.png
   :alt: Horizontal bar chart of installed cable length per cable type, split into feeders and service connections
   :width: 85%

   Installed cable length by type for PLZ 85653, version 1 (from ``pylovo.pandapower_line``; figure
   made with ``docs/scripts/plot_analysis.py``).

Key figures with ``pylovo-analyze``
-----------------------------------

.. code-block:: bash

   uv run pylovo-analyze --plz 85653              # postcode key figures
   uv run pylovo-analyze --plz 85653 --per-grid   # key figures per grid (needs the first)
   uv run pylovo-analyze --plz 85653 --all        # both

The analysis uses the grids of the active ``VERSION_ID``. With ``ANALYZE_GRIDS: True`` (shipped
setting) the postcode key figures are computed right after generation. A postcode or grid that
already has key figures is skipped.

Postcode key figures (``plz_parameters``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

One row per version and postcode; the values are JSON objects keyed by station rating (kVA). A
station of two parallel units counts with its total rating (1260 for 2 × 630 kVA):

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Column
     - Content
   * - ``trafo_num``
     - Number of grids per transformer rating.
   * - ``cable_length``
     - Installed cable length in km per cable type (parallel cables counted separately).
   * - ``load_count_per_trafo``
     - Per rating: list with the number of consumers of each grid (all lists of a rating in the
       order of the grids, ``kcid`` then ``bcid``).
   * - ``bus_count_per_trafo``
     - Per rating: list with the number of buses of each grid.
   * - ``sim_peak_load_per_trafo``
     - Per rating: list with the coincident peak load (kW) of each grid.
   * - ``max_distance_per_trafo``, ``avg_distance_per_trafo``
     - Per rating: lists with the maximum and mean path length (m) from the LV bus to the loads.

Per-grid key figures (``clustering_parameters``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

One row per grid (``grid_result_id``), computed by
:meth:`~pylovo.analysis.parameter_calculation.ParameterCalculator.analyze_grid_parameters_for_plz`.

A **branch** is a feeder as the structural validation of the pylovo paper defines it, without line labels, so that
a cabinet star and a row of tee joints split alike. A bus belongs to the *shared network* if at least two buildings
lie at or beyond it. The cable from the last shared bus to a building is the building's *dedicated path*. The
*station* is the busbar with the buses it reaches over links of at most 2 m. A **feeder** is a cable that leaves the
station towards at least two buildings. A building at the busbar, or on its own cable from the station, belongs to
no feeder, and a grid without a feeder (one building, or one cable per building) has 0 branches. The per-branch
figures count only the buildings behind the feeders and are 0 without a feeder, as for the eight structural metrics
(:ref:`structural-metrics`).

A **household equivalent** is the installed power of a consumer divided by ``PEAK_LOAD_HOUSEHOLD`` (16.825 kW): a
load measure that makes residential, commercial and public buildings comparable. It is no household count and no
coincident load.

.. list-table::
   :header-rows: 1
   :widths: 34 50 16
   :class: fixed-table

   * - Column
     - Content
     - Unit
   * - ``no_house_connections``
     - Consumer buses: the buildings of the grid.
     - count
   * - ``no_connection_buses``
     - Street-side connection buses.
     - count
   * - ``no_branches``
     - Feeders: cables that leave the station towards at least two buildings.
     - count
   * - ``no_branch_ends``
     - Ends of the shared network: shared buses from which no shared bus continues, the busbar excluded. More ends
       mean more branching.
     - count
   * - ``no_households``
     - Households: the sum of ``load_units`` of the residential loads. A non-residential load is no household.
     - count
   * - ``no_household_equ``
     - Household equivalents of the grid.
     - –
   * - ``no_house_connections_per_branch``
     - Buildings behind the feeders divided by ``no_branches``.
     - count
   * - ``no_household_equ_per_branch``
     - Household equivalents behind the feeders divided by ``no_branches``.
     - –
   * - ``max_household_equ_of_a_branch``
     - Household equivalents of the feeder with the most.
     - –
   * - ``house_distance_km``
     - Median over the buildings of the mean distance to their (up to) four nearest buildings. Empty for a grid
       with one building.
     - km
   * - ``transformer_mva``, ``osm_trafo``
     - Rating of the station (``sn_mva`` × ``parallel``). ``osm_trafo`` is true for brownfield grids (``bcid`` < 0).
     - MVA
   * - ``max_trafo_dis``, ``avg_trafo_dis``
     - Maximum and mean path length along the cables from the busbar to the buildings, service cables included.
     - km
   * - ``cable_length_km``, ``cable_len_per_house``
     - Installed cable length (``length_km`` × ``parallel``) and length per building.
     - km
   * - ``max_power_mw``, ``simultaneous_peak_load_mw``
     - Installed power and coincident peak load of the grid, the latter from the grid's own loads with the formula
       of ``sim_peak_load_per_trafo``.
     - MW
   * - ``resistance``, ``reactance``
     - Consumer-weighted resistance and reactance (*Verbrauchersummenwiderstand*): the sum over the buildings of
       their household equivalents times the R or X of the lines on their path, each line weighted with the
       coincidence factor of the loads behind it and divided by its parallel cables.
     - Ω
   * - ``ratio``
     - ``resistance`` divided by ``reactance``: a load-weighted mean R/X of the lines, so it lies between the
       smallest and the largest R/X of the cable catalogue.
     - –
   * - ``vsw_per_branch``
     - ``resistance`` of the buildings behind the feeders divided by ``no_branches``.
     - Ω
   * - ``max_vsw_of_a_branch``
     - ``resistance`` of the feeder with the largest one: an upper bound of the voltage drop at the end of that
       feeder, exact for an unbranched one.
     - Ω
   * - ``max_route_resistance_ohm``
     - Highest series resistance of the cables from the busbar to a building (the worst-supplied building),
       parallel cables dividing the resistance of their line. Unlike ``resistance`` it is not weighted with loads.
     - Ω
   * - ``key_figures_version``
     - Version of these definitions (``KEY_FIGURES_VERSION``). The per-grid analysis computes rows of an older
       version again, and a classification reads only key figures of the version it was clustered with.
     - –

The Ω of the impedance figures are weighted with household equivalents. Multiplied by ``PEAK_LOAD_HOUSEHOLD`` and
divided by the square of the nominal voltage, ``resistance`` becomes the summed resistive voltage drop of the grid in
per unit. Version 1 of the key figures (pylovo 1.0.0) counted the street cables at the busbar as branches, at
least one, but kept the buildings at the busbar in the per-branch figures, and weighted only R with the coincidence
factor. It had no ``no_branch_ends`` and ``max_route_resistance_ohm``.

Service cables are straight lines from the street to the building. They make up about a tenth of ``avg_trafo_dis``
in a typical grid, but where streets are missing in the map data a service cable can be hundreds of metres long.
There, ``max_trafo_dis`` and ``avg_trafo_dis`` measure the missing streets rather than the length of the feeders.

Cable lengths are installed lengths: a line of ``parallel`` cables counts ``length_km`` ×
``parallel``, in ``plz_parameters.cable_length`` and in ``cable_length_km`` alike. Only the
``graph_length`` of the comparison metrics counts each route once.

Working with the pandapower nets
--------------------------------

Every grid can be loaded as pandapower network for your own studies, for example time-series
simulations with your own load profiles:

.. code-block:: python

   import pandapower as pp
   from pylovo.database.database_client import DatabaseClient

   with DatabaseClient() as dbc:
       net = dbc.read_net_db(85653, 1, 3, version_id="1")

   pp.runpp(net)
   print(net.res_bus.vm_pu.min(), net.res_line.loading_percent.max())

The loads of a stored net are the synthetic validation snapshot (:ref:`validation-snapshot`):
``p_mw`` is the transformer-coincident operating point, ``max_p_mw`` the installed peak and
``service_design_p_mw`` the building-local design load. Replace ``p_mw`` with your own profiles
for time-series studies.


Coverage and border diagnostics
-------------------------------

Two reports account for the buildings of generated PLZ and show the context of their borders; they
write CSV and JSON files to ``--output`` (default ``results/diagnostics``) and change nothing in the
database:

.. code-block:: bash

   pylovo-analyze --plz 80331 80333 80469 --coverage --border-diagnostics [--version-id <id>]

``--coverage``
    Every source building that generation loads (InfDB, same filters) is ``generated`` (in a grid,
    in any PLZ), ``excluded`` with the step that left it out (``pylovo.building_exclusion``: e.g.
    ``mv_direct``, ``no_connection_point``, ``isolated_component``, ``unloaded``,
    ``transformer_evidence``), ``postcode_not_generated`` (its PLZ has no results: it failed or was not run) or ``missing``.
    A PLZ that got no grid at all still stores the exclusions of its buildings.
    A ``buildings_result`` or ``building_exclusion`` row without a source building is reported as
    ``not_in_source``, a building that is both in a grid and excluded as ``generated_and_excluded``.
    If any building is ``missing`` or ``generated_and_excluded`` (inconsistent records of the
    version), the command prints their number and exits with code 3. ``not_in_source`` and
    ``postcode_not_generated`` do not fail the command: stored rows without a source building
    appear when the source data or its filters changed after the generation (for example for a
    version generated with an older pylovo), and failed or empty PLZ are reported by
    ``pylovo-generate`` (exit 4). The reports are written either way.

``--border-diagnostics``
    Per PLZ the share of its polygon inside the InfDB import scope (``scope_share``), the share of
    the ring of neighboring streets around it inside the scope (``context_share``) and the street length inside and in
    the ring: a PLZ can lie inside the scope while its surroundings were never imported. Per grid
    the number of buildings and the distance to the nearest PLZ border with a generated neighbour,
    with a neighbour without results (open) and without a neighbouring PLZ (outer), and to the edge
    of the import scope. Two PLZ whose polygons are at most 10 m apart are neighbours, so a small
    gap between the polygons still counts as a shared border. The street length of the ring counts
    only the parts of the streets of other PLZ that lie in the ring. Without a scope table in the InfDB, ``scope_share`` and
    ``context_share`` are empty in the CSV and ``null`` in ``diagnostics_<version>.json``; a PLZ
    without a polygon in ``pylovo.postcode`` stops the command. These are diagnostics: report
    flagged and unflagged grids side by side.
