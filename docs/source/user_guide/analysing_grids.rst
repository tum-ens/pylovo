Analysing grids
===============

Generated grids can be analysed on three levels: the diagnostics stored with every grid, the
key figures computed by ``pylovo-analyze``, and your own analyses on the pandapower nets.

.. _structural-metrics:

Six structural metrics for synthetic grids
------------------------------------------

The comparison metric set in :func:`pylovo.analysis.grid_analysis.compute_comparison_parameters`
summarises feeder topology, extent and electrical equipment. These values describe a grid's
structure; they are not a power-flow result or a claim that the synthetic grid matches an
operator's grid. The default calculation excludes terminal building service connections from
length, distance and resistance and considers the active, switch-aware network. It counts
terminal backbone branches after pruning service stubs.

.. list-table::
   :header-rows: 1
   :widths: 29 71
   :class: fixed-table

   * - Metric
     - Interpretation
   * - ``feeder_lines`` (count)
     - Terminal feeder/backbone branches, including splits beyond the transformer. A larger
       value suggests a more branched supply structure, not necessarily more first-hop cables.
   * - ``graph_length`` (km)
     - Total unique routed backbone length. Parallel line rows between the same buses are
       collapsed so physical routes are not counted twice.
   * - ``avg_trafo_distance`` (km)
     - Mean shortest-path distance from the LV transformer bus to terminal backbone nodes.
   * - ``max_trafo_distance`` (km)
     - Longest such path; useful for identifying grids with a distant feeder endpoint.
   * - ``transformer_mva`` (MVA)
     - Rating of the transformer station: the unit rating times the number of parallel units
       (0.8 MVA for two 400 kVA units).
   * - ``graph_resistance`` (Ω)
     - Aggregate routed-line resistance proxy over the feeder backbone. It sums the equivalent
       resistance of segments, accounting for parallel conductors; it is not an end-to-end
       path resistance or a load-flow loss.

The metrics also work for grids without the pylovo bus names, for example exports of real grids:
their load buses count as house connections and every other bus as backbone. To mark service
buses, cable distribution cabinets and service cables by the names of an export, pass
``bus_type_config`` with its name patterns
(:func:`~pylovo.analysis.parameter_calculation.classify_bus_types`).

.. figure:: /images/analysis/synthetic_comparison_metrics.png
   :alt: Six histograms of feeder count, feeder length, mean and maximum transformer distance, transformer capacity and resistance for synthetic grids
   :width: 100%

   Distributions for **synthetic grids only**, version 1, PLZ 91301. Of 113 stored grids,
   106 have at least five buses and enter the figure; seven smaller grids are excluded. The
   orange lines mark medians. The read-only source script is
   ``docs/scripts/plot_synthetic_metrics.py``; run it with
   ``uv run --extra plots python docs/scripts/plot_synthetic_metrics.py`` after generating
   grids for this version and postcode.

In this synthetic set, the median grid has four terminal feeder branches, 0.82 km of backbone
and a 0.63 MVA transformer station. Its mean and maximum transformer-to-terminal distances are
0.24 and 0.35 km, and the median resistance proxy is 0.15 Ω. The 10th–90th percentile range of
backbone length is 0.15–2.31 km, showing substantial variation between local networks. These
summaries describe this one generated postcode and version; use a broader regional sample for
population-level conclusions.

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
station of two parallel units counts with its total rating (800 for 2 × 400 kVA):

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

A **branch** is an outgoing circuit of the LV busbar: every cable that leaves the busbar of the station. A building
connected directly at the busbar is a branch of its own, and a grid with one building has one branch. All per-branch
figures use these branches. The structural metrics count feeders differently
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
     - Branches with at least one building.
     - count
   * - ``no_households``
     - Households: the sum of ``load_units`` of the residential loads. A non-residential load is no household.
     - count
   * - ``no_household_equ``
     - Household equivalents of the grid.
     - –
   * - ``no_house_connections_per_branch``
     - ``no_house_connections`` divided by ``no_branches``.
     - count
   * - ``no_household_equ_per_branch``
     - ``no_household_equ`` divided by ``no_branches``.
     - –
   * - ``max_household_equ_of_a_branch``
     - Household equivalents of the branch with the most.
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
     - ``resistance`` divided by ``no_branches``.
     - Ω
   * - ``max_vsw_of_a_branch``
     - ``resistance`` of the branch with the largest one: an upper bound of the voltage drop at the end of that
       branch, exact for an unbranched feeder.
     - Ω
   * - ``key_figures_version``
     - Version of these definitions (``KEY_FIGURES_VERSION``). The per-grid analysis computes rows of an older
       version again, and a classification reads only key figures of the version it was clustered with.
     - –

The Ω of the impedance figures are weighted with household equivalents. Multiplied by ``PEAK_LOAD_HOUSEHOLD`` and
divided by the square of the nominal voltage, ``resistance`` becomes the summed resistive voltage drop of the grid in
per unit. Version 1 of the key figures (pylovo 1.0.0) counted only the street feeders as branches, but kept the
buildings at the busbar in the per-branch figures, and weighted only R with the coincidence factor.

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
