.. _mv-parameters:

MV parameters
=============

The parameters of an MV variant are the ``MV_*`` keys of ``config_generation_mv.yaml`` and the
constants of the MV stage (:doc:`mv_grids`, :doc:`../concepts/mv_grids`). Both are stored once per
variant in ``mv_variant.mv_parameters``, not in the snapshot of ``VERSION_ID``, so a changed
parameter needs a new ``MV_VARIANT_ID`` (:ref:`mv-variants`).
:data:`pylovo.mv_grid.options.YAML_KEYS` and :data:`pylovo.mv_grid.options.CONSTANTS` define them
with their bounds. A value that does not fit stops the run with a message that names the key or
the constant.

``MV_*`` keys
-------------

The ``MV_*`` keys are in ``config_generation_mv.yaml``, which pylovo reads from the directory of
``config_generation_lv.yaml`` (:ref:`mv-configuration`). A missing key takes its shipped value, so
older configuration files keep working. A former ``config_generation.yaml`` with ``MV_*`` keys is
still read, and a key set in both files needs the same value. ``config_generation_mv.yaml`` holds
only the keys below; another key stops pylovo when it reads the configuration.

``pylovo-mv generate --set KEY=VALUE`` changes a key for one variant, except
``MV_GRID_GENERATION``, ``MV_VARIANT_ID`` and ``MV_VARIANT_COMMENT``.

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
     - MV variant of the LV version (1 to 10 letters, digits, ``_``, ``.`` or ``-``). A changed MV
       parameter needs a new id.
   * - ``MV_VARIANT_COMMENT``
     - ``default``
     - Comment stored with a new MV variant.
   * - ``MV_NOMINAL_VOLTAGE_KV``
     - ``auto``
     - ``auto``: 10 kV for supply areas with at least half of their installed capacity in
       municipalities that have at least ``mv_10kv_min_density_mva_km2`` of installed MV/LV capacity
       per km², else 20 kV (§ 23c EnWG structural data; Harnisch et al. 2016). ``20`` or ``10``: one
       voltage for all supply areas.
   * - ``MV_MAX_STATIONS_PER_RING``
     - ``20``
     - Most ring members (stations and MV customers) of one ring, 1 to 100. A rural member counts
       1 / ``rural_ring_factor``, so a rural ring holds up to twice as many (Ding0; 5 to 30 in a
       large-city grid, Kreutmayr 2023).
   * - ``MV_STANDARD_CABLE``
     - ``null``
     - A pandapower cable type. ``null`` selects NA2XS2Y 1x240 RM/25 for the voltage.
   * - ``MV_OVERHEAD_LINE``
     - ``null``
     - A pandapower overhead line type, used where a line follows an MV overhead line mapped in
       OpenStreetMap. ``null`` selects 122-AL1/20-ST1A for the voltage.
   * - ``MV_LINE_PLANNING_UTILIZATION``
     - ``0.6``
     - Planned current per ampacity of MV lines in normal operation, 0.1 to 1.0 (dena 2012;
       Harnisch et al. 2016; Wintzek et al. 2021; SimBench uses 0.5).
   * - ``MV_MAX_VOLTAGE_DROP_PERCENT``
     - ``5``
     - MV share of the voltage band in normal operation, from the busbar to every node, 0.5 to 20
       (Harnisch et al. 2016; SimBench; Ding0).

Constants
---------

The constants are no YAML keys, and ``--set`` does not take them. They are changed in Python only,
with :meth:`MvParameters.replace <pylovo.mv_grid.parameters.MvParameters.replace>` (field names,
which are the constant names) or :meth:`~pylovo.mv_grid.parameters.MvParameters.replace_raw` (key
and constant names). Both check the values like the configuration:

.. code-block:: python

   from pylovo.mv_grid.parameters import MvParameters
   from pylovo.mv_grid.stage import run_mv_stage

   params = MvParameters.from_config().replace(variant_id="ls0", comment="no local search",
                                               local_search_rounds=0)
   summary = run_mv_stage("1", None, params)          # every postcode with LV grids of version 1

The constants are stored in the snapshot of every variant like the keys. The constants of the
routing graph and the settlements are also part of the graph id, so a change builds a new graph.

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
     - Control band of the OLTC. The voltage budget and the coupling put the busbar at its lower
       edge (Harnisch et al. 2016).
   * - ``max_drop_n1_percent``
     - 10.0
     - Voltage drop limit in (n-1) operation (Ding0; SimBench).
   * - ``mv_10kv_min_density_mva_km2``
     - 5.0
     - Installed MV/LV capacity per km² of a municipality (its area inside the planning region) from
       which ``auto`` gives 10 kV to the supply areas that have at least half of their capacity there (§ 23c
       EnWG structural data, not calibrated on supply areas; :ref:`mv-voltage-level`).
   * - ``n1_utilization``
     - 1.0
     - Line loading limit in the (n-1) cases (dena 2012).
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
     - MV customers above this coincident load get a dedicated feeder (eGon-data MV level up to
       5.5 MW).
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
   * - ``bridge_detour``
     - 1.3
     - Detour factor of bridges between graph islands and of long connectors (Ding0).
   * - ``long_connector_m``
     - 300
     - Connectors from a node to the street above this length count ``bridge_detour`` times and are
       logged.
   * - ``osm_line_snap_m``
     - 100
     - A loose end of an OSM MV line joins the nearest street point within this distance by a
       straight cable connector (assumption).
   * - ``osm_line_cost_factor``
     - 0.7
     - Routing cost of an OSM MV line per m of its length, so that routes and rings follow the
       existing corridors (assumption; 1: no preference). Lengths and checks use the true lengths.
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

.. list-table:: Substations
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
     - Largest distance of the eGon-data substation (eTraGo bus) matched to an OSM site.

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
