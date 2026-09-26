Analysing grids
===============

Generated grids can be analysed on three levels: the diagnostics stored with every grid, the
key figures computed by ``pylovo-analyze``, and your own analyses on the pandapower nets.

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
     - Rating of one transformer unit. For stations with parallel units, this is not the sum of
       their ratings.
   * - ``graph_resistance`` (Ω)
     - Aggregate routed-line resistance proxy over the feeder backbone. It sums the equivalent
       resistance of segments, accounting for parallel conductors; it is not an end-to-end
       path resistance or a load-flow loss.

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
     - Per rating: list with the number of consumers of each grid.
   * - ``bus_count_per_trafo``
     - Per rating: list with the number of buses of each grid.
   * - ``sim_peak_load_per_trafo``
     - Per rating: list with the coincident peak load (kW) of each grid.
   * - ``max_distance_per_trafo``, ``avg_distance_per_trafo``
     - Per rating: lists with the maximum and mean path length (m) from the LV bus to the loads.

.. note::

   The rating key is the rating of a single transformer unit (``sn_mva`` of the pandapower
   transformer). Grids with two parallel units, such as 800 kVA built from 2 × 400 kVA, are
   therefore counted under 400.

Per-grid key figures (``clustering_parameters``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

One row per grid (``grid_result_id``), computed by
:meth:`~pylovo.analysis.parameter_calculation.ParameterCalculator.analyze_grid_parameters_for_plz`:

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Column
     - Content
   * - ``no_house_connections``, ``no_connection_buses``
     - Number of consumer buses and of street-side connection buses.
   * - ``no_branches``
     - Number of feeders leaving the transformer (topological count).
   * - ``no_households``
     - Number of load elements (one per consumer and load category).
   * - ``no_household_equ``
     - Installed power divided by ``PEAK_LOAD_HOUSEHOLD``.
   * - ``no_house_connections_per_branch``, ``no_households_per_branch``,
       ``max_no_of_households_of_a_branch``
     - The counts above per feeder, and the largest feeder.
   * - ``house_distance_km``
     - Median of the mean distance of each consumer to its four nearest neighbours.
   * - ``transformer_mva``, ``osm_trafo``
     - Rating of one transformer unit; ``osm_trafo`` is true for brownfield grids (``bcid`` < 0).
   * - ``max_trafo_dis``, ``avg_trafo_dis``
     - Maximum and mean path length (km) from the transformer to the consumers.
   * - ``cable_length_km``, ``cable_len_per_house``
     - Total cable length and length per consumer.
   * - ``max_power_mw``, ``simultaneous_peak_load_mw``
     - Installed power and coincident peak load of the grid.
   * - ``resistance``, ``reactance``, ``ratio``, ``vsw_per_branch``, ``max_vsw_of_a_branch``
     - Path-impedance proxies weighted with household equivalents
       (:meth:`~pylovo.analysis.parameter_calculation.ParameterCalculator.calculate_impedance_metrics`).

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
