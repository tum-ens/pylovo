Medium-voltage grids
====================

The MV stage connects the stations of a generated LV version to the HV/MV substations of its region
with medium-voltage (MV) grids. Every LV grid of the version becomes one MV node at its station.
The large non-residential consumers that the LV stage leaves to the MV grid (MV customers) and the
generators of the Marktstammdatenregister (MaStR) that feed in at MV level are added as further
nodes. The stage plans open rings along the streets, sizes the lines and the HV/MV transformers and
checks the result with pandapower power flows. It reads the stored LV results and never changes
the LV grids. Its parameters belong to an *MV variant* of the LV version, so several MV designs of
one LV version can be stored and compared.

This page describes the method. :doc:`../user_guide/mv_grids` explains how to run the stage, which
inputs it needs and where it stores the results, and lists every parameter.

The stage is implemented from scratch in :mod:`pylovo.mv_grid`. It follows published planning
methods and rules, in particular the open-ring model of Ding0 (Amme et al. 2018) and the substation
filter of Hülk et al. (2017). No code of Ding0 (AGPL-3.0) or of another grid generator is used. The
algorithms are reimplemented from the publications and textbooks listed in :ref:`mv-references`.

Method at a glance
------------------

For one MV variant of an LV version, :func:`~pylovo.mv_grid.stage.run_mv_stage` runs these steps:

#. Read the LV results of the requested postcodes: one node per LV grid with its load statistics,
   the MV customers and the stored LV voltage requirement of every station.
#. Build or reuse the routing graph of the region from the regional street graph of InfDB, with a
   settlement flag for every street edge (:ref:`mv-routing-graph`).
#. Select the HV/MV substations from OpenStreetMap or eGon-data (:ref:`mv-substations`) and add the
   MV generators of MaStR (:ref:`mv-generators`).
#. Attach all nodes and substations to the routing graph, split the region into supply areas, one
   per substation (:ref:`mv-supply-areas`), and give each supply area its nominal voltage
   (:ref:`mv-voltage-level`).
#. Plan every supply area on its own, in parallel worker processes: rings with normally-open
   points, rural stubs, feeders of large customers and generators, line types, five power-flow
   cases, a repair loop, the HV/MV transformers and the voltage budget of every station
   (:ref:`mv-rings` to :ref:`mv-voltage-budget`).
#. Add normally-open ties between the rings of neighbouring supply areas (:ref:`mv-ties`) and
   store the run summary.

The command ``pylovo-mv couple`` can afterwards solve the MV grids together with the stored LV
grids (:ref:`mv-coupling`).

Terms
-----

.. code-block:: text

   110 kV grid --- HV/MV transformers (OLTC) --- MV busbar

   MV busbar --- S --- S --- C --- J --- S --[NOP]-- S --- MV busbar
          exit A                   |                  exit B
                                   S   stub (T-off) from joint J

   S    MV/LV station (one LV grid)        C    MV customer
   J    joint (T-joint) of a stub          NOP  normally-open point

The sketch shows one open ring with five members and one stub. The terms used on this page are:

Supply area
   The part of the region that one HV/MV substation supplies, also called MV grid district. The
   stage stores one MV grid (``pylovo.mv_grid``) per supply area (:ref:`mv-supply-areas`).
Open ring
   A line loop that leaves the busbar at one feeder bay (exit A) and returns at another (exit B).
   One section is open in normal operation, so each half of the ring is a radial feeder. If a
   section fails, the open point is closed and the whole ring is supplied from the other exit
   ((n-1) operation).
Half-ring (feeder)
   The part of a ring between one exit and the normally-open point.
Normally-open point (NOP)
   The open switch of a ring. It sits in the ring panel of a station, an MV customer or a looped-in
   generator, never at a T-joint, which has no switchgear.
Ring member
   A station, MV customer or looped-in generator on the main path of a ring.
   ``MV_MAX_STATIONS_PER_RING`` limits the members. Joints and stub nodes do not count.
Stub (T-off)
   A radial branch from a joint on a ring to a rural node. A stub has no (n-1) reserve.
Busbar radial
   One radial line from the busbar to a single small rural node.
Dedicated feeder
   A feeder of its own from the busbar to one large MV customer (two legs) or one large generator.
Tie
   A normally-open line between the rings of two supply areas.
MV variant
   One set of MV parameters for one LV version (``pylovo.mv_variant``).

Nodes and loads
---------------

Stations
~~~~~~~~

Every LV grid of the version (one row of ``grid_result``) is one MV node at its station. The
position is the station of ``transformer_positions``, else the bus ``LVbus 1`` of the stored LV
net. A grid without either is left out with a warning. The node keeps the station rating, the
settlement type of its postcode, the source of its position (``station_source``: greenfield, OSM,
LoD2, DSO or manual) and the power-flow status of its LV grid. Since the stations come from the LV
stage, the MV stage works for greenfield versions and for versions with known (brownfield) station
positions alike.

The load of a station is not one number but its load statistics: the summed installed peak and the
number of load units per consumer category (Residential, Commercial, Public, Unknown) of all
building components of its LV grid (:doc:`grid_dimensioning`). Components that the LV stage left to
the MV grid are not part of them.

Coincident load
~~~~~~~~~~~~~~~

The coincident load of any set :math:`F` of nodes (the nodes behind a ring section, a half-ring, a
ring or a supply area) follows pylovo's coincidence law on the summed statistics of its nodes:

.. math::

   P(F) = \sum_c I_c(F)\left[g_c + (1-g_c)\,N_c(F)^{-3/4}\right] + \sum_{i \in F} P_{\mathrm{fixed},i},

where :math:`I_c(F)` and :math:`N_c(F)` are the installed peak and the load units of category
:math:`c` summed over the stations of :math:`F`, :math:`g_c` is the sim factor of the category in
the snapshot of the LV version and :math:`P_{\mathrm{fixed},i}` are the fixed loads of the MV
customers, including those at station busbars (:ref:`mv-customers`). For a single station this is the design load of its LV grid (``p_own_kw``). The station
peaks are never added, because their sum ignores the coincidence between the stations and
overstates the load of a ring or a substation. The law is not monotonic in the non-residential
terms (:doc:`grid_dimensioning`), so every section load is computed and none is inferred from a
neighbouring section.

In the power flows, the coincident load of a feeder is distributed over its stations in proportion
to their installed load per category, as in the validation snapshot of the LV stage
(:ref:`validation-snapshot`). The station loads of a feeder therefore add up to the coincident load
of the feeder. The normal case uses the half-rings as feeders, the (n-1) cases the whole ring.
Reactive power follows the power factor of the LV version.

.. _mv-customers:

MV customers
~~~~~~~~~~~~

pylovo derives the non-residential load of a building from its floor area and a specific peak per
m² of its category (:doc:`../user_guide/input_data`). Large consumers are supplied from the MV
grid. However, a simple threshold gives many MV customers: with a threshold of 200 kW, Dubielzig
(2022) found 1.2 to 1.9 times as many MV connection points (stations and MV customers) as published
for four cities, mainly because of the MV customers. Therefore, one threshold in the LV stage and
two rules in the MV stage decide which customers become MV nodes:

#. In the LV stage, a Commercial or Public load component above
   ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` (200 kW installed, the boundary between the MV/LV
   station level and the MV level in eGon-data) leaves the LV grid, and the building is stored in
   ``pylovo.mv_customers``. The residential part of a mixed building stays in its LV grid. The
   threshold is a parameter of the LV version, not of the MV variant.
#. The hall rule corrects the floor area. The building data derive the storeys from the building
   height, so a large single-storey hall counts several storeys and a large floor area. For the MV
   load, a building with a footprint of at least ``customer_hall_min_footprint_m2`` (2,000 m²)
   counts one storey: its non-residential floor area is the smaller of its footprint and its floor
   area (flag ``hall_one_storey`` where this lowers it). The installed load is this area times the
   specific peak of the category in the LV snapshot.
#. The MV threshold acts on the corrected own peak. A customer whose own peak is below
   ``customer_min_mv_kw`` (200 kW, as in eGon-data) gets no MV node. The own peak of one building
   is its installed load after the hall rule, because pylovo's law gives a single load unit the
   factor 1. Such a customer becomes a *station-busbar customer* of the nearest station
   (straight-line distance) if that station is at most ``busbar_customer_max_m`` (1 km, pylovo's
   default ``MAX_GREENFIELD_TRAFO_DISTANCE``) away. Its fixed load is added to the station (flag
   ``busbar_customers``). A small customer farther away keeps an MV node of its own (flag
   ``remote_small_customer``).

If the own peak of a station plus the fixed loads of its busbar customers exceeds the station
rating times ``TRANSFORMER_PLANNING_UTILIZATION`` of the LV version, the station gets pylovo's
rating for that load in the MV model (flag ``station_busbar_overload``). This is the smallest rating
of ``TRANSFORMER_MAPPING`` for the settlement type of its postcode above load / utilisation, and
n × 630 kVA above the catalogue. The LV grid itself is not changed, but ``pylovo-mv couple`` uses
the larger transformer.

The remaining customers are fixed loads :math:`P_\mathrm{fixed} = g_c \times` installed load, with
the sim factor of their category (the limit of pylovo's law for many units). This coincident load
decides how a customer is connected:

.. list-table::
   :header-rows: 1
   :widths: 34 66
   :class: fixed-table

   * - Coincident load
     - Connection
   * - above ``hv_customer_kw`` (20 MW)
     - HV customer: listed with the flag ``hv_customer`` (``connection_hint`` ``excluded``), but not
       modelled.
   * - above ``dedicated_feeder_kw`` (5.5 MW)
     - Dedicated two-leg feeder from the busbar (:ref:`mv-topology`).
   * - otherwise
     - Ring member, or stub if the customer is rural.

The thresholds of 5.5 MW and 20 MW are the upper limits of the MV and the HV/MV level in
eGon-data. A customer whose building contains an enabled transformer candidate of
``pylovo.transformers`` is placed at that station (flag ``mapped_station``). Every other customer is
moved to the nearest point of the street (flag ``transfer_point``), because its private cable to the
building is not part of the MV grid.

.. _mv-generators:

Generators from MaStR
~~~~~~~~~~~~~~~~~~~~~

The generators come from ``pylovo.mastr_units``, the units in operation with coordinates of the
MaStR import (:doc:`../user_guide/mv_grids`). Storage units take part in no case. The units are
grouped per grid connection point, i.e. per MaStR location (``LokationMastrNummer``), so one wind or
PV park is one connection point. Units of one location more than 1 km apart (single linkage) form
separate points (flag ``location_split``). A point gets the power-weighted position of its units and
their net power per group (solar, wind, other).

The connection voltage level (``Spannungsebene``) is entered in the MaStR by the grid operator for
each location. The highest level of the units of a point decides how it is modelled. Without a
known level, the installed power decides with the thresholds of eGon-data
(``generator_fallback_kw``):

.. list-table::
   :header-rows: 1
   :widths: 40 12 18 30
   :class: fixed-table

   * - ``Spannungsebene``
     - Class
     - Without a level: up to
     - Modelled as
   * - Höchstspannung, Hochspannung, Umspannebene Höchstspannung/Hochspannung
     - ``hv``
     - (above 20 MW)
     - not modelled (counted in the log)
   * - Umspannebene Hochspannung/Mittelspannung
     - ``hvmv``
     - 20 MW
     - MV node with a dedicated feeder
   * - Mittelspannung
     - ``mv``
     - 5.5 MW
     - MV node
   * - Umspannebene Mittelspannung/Niederspannung
     - ``mvlv``
     - 200 kW
     - MV node above ``generator_lv_side_kw`` (200 kW), else LV side
   * - Niederspannung ...
     - ``lv``
     - 100 kW
     - LV side

LV-side generation belongs to a station: to the LV grid of the nearest building of the version
within 100 m, else to the nearest station within 2 km. Otherwise it is dropped with a warning. Units
without coordinates (mostly small rooftop PV, ``pylovo.mastr_postcode_aggregates``) are split over
the stations of their postcode in proportion to the number of buildings per station, since rooftop
PV follows the buildings. Their power is scaled with the share of the postcode area inside the
imported postcode polygons. The MV model represents the LV-side generation of a station by static
generators at its node. How MV generator nodes are connected is described in :ref:`mv-topology`.

.. _mv-substations:

HV/MV substations
-----------------

With ``MV_SUBSTATIONS: osm`` (default), the substations come from the current OpenStreetMap data in
the InfDB (``INFDB_OSM_SCHEMA``): every ``power=substation`` node, way or relation within
``region_buffer_m`` (5 km) of the planning region, which is the union of the requested postcode
polygons. Substations outside the region are needed, because they supply its border areas. The
filter follows Hülk et al. (2017, Table 1), adapted. A candidate is kept if its ``voltage`` tag
holds 110 kV or 60 kV and none of these rules applies:

#. ``substation`` is ``traction``, ``transition``, ``converter``, ``generation``, ``industrial`` or
   ``minor_distribution``. A ``transmission`` tag with 110 kV is kept, because many HV/MV
   substations of distribution grids are tagged so.
#. ``frequency`` holds 16.7 Hz or 0 (railway or DC).
#. The operator is a railway operator (DB Energie, Deutsche Bahn, DB Netz, Bahnstrom), the name
   marks a railway substation (Unterwerk, Bahnstrom, Umformerwerk, Umrichter), or a ``railway`` tag
   is set.
#. The candidate lies inside a ``power=plant`` or ``power=generator`` polygon (the substation of a
   power plant or PV park).

Kept candidates within ``substation_merge_m`` (75 m) of each other form one site. Its busbar is a
point inside its largest footprint polygon. An extra-high-voltage site (220 kV or more) without a
tagged MV level is dropped if an HV/MV site lies within ``ehv_partner_m`` (1 km), else it is kept
with the flag ``ehv_uncertain``. Of two sites closer than ``substation_drop_m`` (150 m), the one
with the smaller footprint is dropped. The MV level of a site is rarely tagged, so the nominal
voltage comes from the rule of :ref:`mv-voltage-level`.

Each OSM site gets the nearest eGon-data HV/MV substation within ``egon_match_m`` (300 m) as a
cross-check (``mv_substation.egon_bus_id``). With ``MV_SUBSTATIONS: egon``, the eGon-data
substations themselves are the sites, since eGon-data has filtered and aggregated them already.

A substation whose busbar lies more than ``substation_max_attach_m`` (2 km) from the routing graph
supplies nothing (flag ``long_attach``). A substation that supplies no node gets no MV grid (flag
``no_nodes``) but stays in ``mv_substation``. Transformer ratings mapped in OSM are not read in the
current version, so every substation is sized by the rule of :ref:`mv-equipment`.

.. _mv-supply-areas:

Supply areas
------------

What a supply area is
~~~~~~~~~~~~~~~~~~~~~

In a German distribution grid, every HV/MV substation (Umspannwerk) feeds an MV grid of its own.
All MV/LV stations and MV customers of an area hang on the rings of that substation. The MV grids
of neighbouring substations meet only at normally-open points and are not connected in normal
operation. pylovo calls such an area a supply area and stores one MV grid (``pylovo.mv_grid``) per
supply area. No open data set gives the supply areas of the real grids, so the stage derives them.

Why the stage needs them
~~~~~~~~~~~~~~~~~~~~~~~~

First, a ring starts and ends at the busbar of one substation, so every node must be assigned to a
substation before rings can be formed. Second, the HV/MV transformers of a substation are sized for
the coincident load of its area, and a substation can supply only a limited load when one
transformer is out of service. Third, the supply areas split the region into independent planning
problems. Each one is planned in its own worker process, which keeps the stage fast for many
postcodes. Finally, the area of a supply area is needed for the density rule of the nominal voltage
and for metrics per km².

How they are formed
~~~~~~~~~~~~~~~~~~~

With ``MV_SUPPLY_AREAS: routed`` (default), each node goes to the substation that it reaches with
the smallest weighted street distance. This is the distance along the routing graph from the busbar
of the substation to the node plus a weight per substation:

#. All weights start at 0, so every node goes to the substation that it reaches first along the
   streets. This is a Voronoi partition on the street graph instead of the plane.
#. The coincident load of each substation (stations and MV customers of its area, divided by the
   power factor) is compared with its (n-1) capacity: (``hv_mv_units`` - 1) units of the largest of
   ``hv_mv_sizes_mva``, i.e. 63 MVA with the defaults.
#. Every overloaded substation gets ``rebalance_step_m`` (500 m) more weight, which hands the
   outermost nodes of its area to its neighbours. The assignment is repeated until no substation is
   overloaded or ``rebalance_max_iter`` (50) raises are spent. Remaining overloads get the flag
   ``over_capacity`` and more transformers (:ref:`mv-equipment`).

For example, a town between two substations is split at the streets that are equally far from
both. If one of the two cannot supply its part with one transformer out of service, its distances
are lengthened in steps of 500 m until the stations at the border of its area belong to the other
one. Since the weight is added to the distance from the substation, a node on the shortest path to
a node of a substation belongs to the same substation. The supply areas therefore stay connected
along the streets, unlike areas formed by moving single nodes. Generators and HV customers are
assigned by the same rule but do not count for the capacity. A node without any street path to a
substation goes to the nearest substation in straight line (column ``mv_node.bridged``).

With ``MV_SUPPLY_AREAS: egon``, which needs ``MV_SUBSTATIONS: egon``, a node goes to the eGon-data
MV grid district that contains it (else to the nearest one), and the depot is the eGon substation
with the same bus id. There is no rebalancing in this case.

Polygon and completeness
~~~~~~~~~~~~~~~~~~~~~~~~

The polygon of a supply area (``mv_grid.geom``) is the union of the Voronoi cells around its
stations and customers, clipped to the planning region. The polygons of all supply areas therefore
cover the region without overlap, and their areas add up to the region area. A supply area is
``complete`` if its substation lies in the region and less than 10 % of its boundary runs along the
region border. Metrics per supply area should only be compared on complete supply areas, because
the others are cut by the region border.

.. _mv-routing-graph:

Routing graph
-------------

Regional graph
~~~~~~~~~~~~~~

MV cables follow streets and field paths. An MV grid spans many postcodes, so the stage does not use
the postcode street graphs of the LV stage but the regional street graph of InfDB,
``<INFDB_SOURCE_SCHEMA>.ways_per_junction`` (street segments split at junctions). The graph is
built once in the database and stored in ``pylovo.mv_graph``, ``mv_graph_vertex``,
``mv_graph_edge`` and ``mv_graph_settlement``:

#. It holds the ways of every municipality (AGS) within ``region_buffer_m`` (5 km) of the planning
   region, always of whole municipalities, without motorways (``graph_excluded_klasse``).
#. Edge ends within ``graph_snap_tolerance_m`` (0.25 m) are one vertex, which joins the ways of
   neighbouring municipalities. Where an edge ends within 0.5 m of the interior of another edge (a
   T-junction at a municipality border), the other edge is split there.
#. Every edge gets the number of address buildings within ``settlement_buffer_m`` (50 m). An edge
   is in a settlement if it has at least ``settlement_min_buildings`` (2) of them and one per
   ``settlement_spacing_m`` (100 m) of its length, or if its midpoint lies in built-up OSM land use
   (residential, commercial, industrial or retail). The land-use rule covers industrial and
   commercial zones, whose large plots have about one address building per 200 m of street, so the
   building rule alone would treat them as open field. Without OSM land use the address buildings
   alone decide.
#. Connected settlement edges form one settlement. A settlement with at least
   ``urban_min_buildings`` (1,000) address buildings, about a small town, is urban.

The graph id is a hash of the municipalities, the build parameters and a fingerprint of the source
rows. A graph is therefore reused by every variant with the same inputs, and a changed source gives
a new graph instead of changing a stored one.

Every node and substation is attached to its nearest graph edge. A node lies in the settlement of
that edge and is urban if the settlement is urban. A node on an edge outside settlements is in open
field and rural.

District graph
~~~~~~~~~~~~~~

Each supply area is routed on a subgraph: the edges within ``subgraph_buffer_m`` (2 km, at least
the largest attachment offset plus 100 m) of the convex hull of its nodes, plus all attachment
edges. The edges are split at the attachment points. A node more than 0.5 m off the street, such as
a brownfield station, a substation busbar or a generator, gets a straight connector to its street
point. Connectors longer than ``long_connector_m`` (300 m) count ``bridge_detour`` (1.3) times
their length. Graph islands with nodes but without the busbar are joined by straight bridges
between their closest vertices (also 1.3 times their length, column ``mv_node.bridged``) instead of
losing their nodes. Distances and paths come from SciPy's Dijkstra on this graph.

.. _mv-rings:

Rings
-----

Most German MV grids are operated as open rings, about 84 % according to Büchner et al. (2014).
The stage therefore plans open rings from the busbar of each substation.

Line lengths and kinds
~~~~~~~~~~~~~~~~~~~~~~

The planner first computes the design length of every possible link between two nodes of a supply
area. With ``MV_ROUTING: street`` (default) it is the street distance on the district graph.
Meinecke et al. (2019) state that urban MV grids use cables only, while rural grids mix overhead
lines and cables. With ``MV_OVERHEAD_LINES: rural`` (default), a link is therefore an overhead line
if its two ends are not in the same settlement and neither end is urban (two open-field ends count
as different settlements). An overhead line takes a straight span with angle poles,
``overhead_detour`` (1.1) times the straight distance, where this is shorter than the street path.
Otherwise it follows the street. All other links are cables along the streets.

With ``MV_ROUTING: crowfly``, every link is a straight span of ``crowfly_detour`` (1.3) times the
straight distance, as in the first version of Ding0 (Amme et al. 2018). The overhead rule still
decides the kind. This variant serves as a comparison with straight-line models.

Ring check and normally-open point
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A ring :math:`R = (v_1, \ldots, v_k)` leaves the busbar :math:`v_0` and returns to it. Its sections
are :math:`s_j = (v_j, v_{j+1})`, :math:`j = 0, \ldots, k`, with :math:`v_{k+1} = v_0`. With
section :math:`s_m` open, half A (:math:`v_1 \ldots v_m`) is fed through :math:`s_0` and half B
(:math:`v_{m+1} \ldots v_k`) through :math:`s_k`. Every section carries the coincident load
:math:`P_{a,b}` of the positions :math:`a \ldots b` behind it. The voltage drop of half A is
linearised as

.. math::

   \Delta u_A(m) = \sum_{j=0}^{m-1} w_j\,P_{j+1,m}, \qquad
   w_j = \frac{(r_j + x_j \tan\varphi)\,\ell_j}{U_n^2},

and half B follows in the same way. The section current is
:math:`I = P / (\sqrt{3}\,U_n \cos\varphi)`. In (n-1) operation one exit is out of service and the
closed ring is fed from the other exit. A ring is feasible if

#. it has at most ``MV_MAX_STATIONS_PER_RING`` (20) members,
#. every section has a street path,
#. an open section exists next to a node with switchgear,
#. for one such open section, both half-rings are at most ``max_half_ring_length_m`` (28 km) long,
   every section current is at most ``MV_LINE_PLANNING_UTILIZATION`` (0.6) times the ampacity and
   both drops are at most ``MV_MAX_VOLTAGE_DROP_PERCENT`` (5 %), and
#. in both (n-1) directions, every current is at most ``n1_utilization`` (1.0) times the ampacity
   and the drop is at most ``max_drop_n1_percent`` (10 %).

The normally-open point is placed at the voltage balance of the two halves (Klabunde et al. 2022):
among the allowed open sections, the one with the smallest :math:`\max(\Delta u_A, \Delta u_B)` is
chosen, ties broken by the smaller difference of the half loads. The switch sits in the ring panel
of a station, MV customer or looped-in generator next to the open section, never at a T-joint.

The check is analytic and neglects cable charging. pandapower verifies the planned grid afterwards
(:ref:`mv-electrical`). The member cap of 20 follows Ding0 (Amme et al. 2018), and Kreutmayr
(2023) reports 5 to 30 stations per ring for a large-city grid. The planning guidelines of dena
(2012), Harnisch et al. (2016) and Wintzek et al. (2021) load MV lines with at most 60 % in normal
operation, which keeps the reserve for (n-1) operation. SimBench uses 50 % (Meinecke et al. 2019). The half-ring limit of 28 km is the
value of the Ding0 configuration.

Ring solvers
~~~~~~~~~~~~

Forming the rings of a supply area is a capacitated vehicle routing problem: the busbar is the
depot, the ring members are the customers and the ring check replaces the vehicle capacity. The
objective of every solver is the total ring length, both busbar sections included.
``MV_RING_SOLVER`` selects the construction:

``savings`` (default)
   Parallel Clarke-Wright savings (Clarke and Wright 1964). Every candidate starts as a ring of its
   own. Pairs are merged in the order of their saving :math:`d_{0i} + d_{0j} - d_{ij}` when both
   are ring ends and the ring check accepts the merged ring. Above 800 candidates, only the pairs of
   the 40 nearest neighbours of every node are considered.
``sweep``
   The sweep heuristic (Gillett and Miller 1974) as a baseline. The nodes are ordered by their
   angle around the busbar and fill one ring after the other until a ring becomes infeasible. Eight
   start angles are tried and the shortest result is kept.
``ortools``
   Reserved for an OR-Tools solver. The current version does not contain it and stops with an
   error.

A granular local search follows the construction (Toth and Vigo 2003; Bräysy and Gendreau 2005).
For every node and each of its ``neighbours`` (15) nearest nodes, the moves relocate, exchange,
2-opt*, 2-opt and Or-opt are tried. The first move that shortens the total length by more than 1 m
and keeps every changed ring feasible is applied. The search stops after a pass without a move or
after ``local_search_rounds`` (50) passes, and ``0`` switches it off. A wall-clock guard of
``ls_time_guard_s`` (120 s) stops it early and is flagged in the metrics. The solvers use no random
numbers and break ties by index, so a run is reproducible unless the guard stops it.

.. _mv-topology:

From rings to lines
~~~~~~~~~~~~~~~~~~~

The planner turns the rings into lines in this order: rings first, then stubs, large customers and
generators.

#. Rings follow their street paths or straight spans.
#. With ``MV_STUBS: rural`` (default), a rural station or MV customer leaves its ring for a stub
   (T-off) from the nearest point of the shortened ring if (a) the T-off is longer than
   ``stub_min_offset_m`` (100 m), (b) leaving the ring saves more ring length than the T-off costs,
   (c) the stub tree carries at most ``stub_max_kva`` (1 MVA, coincident) and is at most
   ``stub_max_length_m`` (2 km) long, and (d) the shortened ring stays feasible. Later stubs may
   hang on earlier ones (stub trees). Urban nodes always stay in the ring. The limits of 1 MVA, the
   size of an emergency generator, and 2 km come from Amme et al. (2018, after Consentec and FGH),
   and Harnisch et al. (2016) connect stations more than about 100 m off the trunk as stubs.
#. A rural ring with a single node of at most 1 MVA within 2 km street distance of the busbar
   becomes a busbar radial.
#. A customer above 5.5 MW gets a dedicated feeder with two legs from the busbar along the same
   route. Leg 2 is open at the customer, so the two legs together are (n-1) secure. Each leg gets as
   many parallel cables as the load needs at ``MV_LINE_PLANNING_UTILIZATION``, at most
   ``max_parallel_cables`` (2). Beyond that the customer is flagged ``dedicated_overload``.
#. The generators are connected in the order of decreasing power. A generator of class ``hvmv`` or
   above ``dedicated_feeder_kw`` gets a dedicated feeder from the busbar, with parallel cables for
   its apparent power at the full ampacity. Any other generator connects to the nearest point of a
   line within ``generator_max_tee_m`` (5 km). An urban generator within ``loop_in_max_offset_m``
   (100 m) of a ring is looped into the ring if the ring stays feasible. All others get a T-off
   whose kind follows the overhead rule. A generator without a line within 5 km gets a dedicated
   feeder.
#. The normally-open points of the final rings are placed, with the joints and looped-in
   generators as explicit positions.
#. The geometries follow the street pieces or are straight spans. Every line is at least 1 m long.

.. _mv-ties:

Ties between supply areas
~~~~~~~~~~~~~~~~~~~~~~~~~

The ring model plans each supply area on its own. Real MV grids, however, have more connections:
cross-ties, several infeeds and support stations, which ring models of single districts lack
(Dubielzig 2022). In a survey of German DSOs, Weisenstein and Wellßow (2021) found that most MV
grids are meshed but operated open. With ``MV_INTERCONNECTION: ties`` (default), the stage
therefore adds normally-open ties to the rings of neighbouring substations after all supply areas
are planned:

#. The candidates are the stations and MV customers looped into the rings of done supply areas.
#. Pairs of candidates of different supply areas with the same nominal voltage within
   ``tie_max_length_m`` (2 km) along the streets are taken in the order of their length, at most
   one tie per ring. The overhead rule decides the kind and the route of a tie.
#. A tie belongs to the supply area with the lower ``mv_grid_id`` and is open at the other end.

Ties carry no flow in any checked case. They are not part of the district nets, of ``circuit_km``
or of the district metrics. Their length, the share of rings with a tie and a takeover check are
stored in ``mv_variant.summary['ties']`` and ``mv_grid.metrics['ties']``. The takeover check is
informational. For each tie end, the stored nets of the two supply areas are joined, the tie is
closed and the exit of the half-ring at that end is taken out of service. The drop and the loading
are then compared with the (n-1) limits. Ties of supply areas cut by the region border or with a
substation outside the region are kept and flagged (``incomplete_district``,
``substation_outside_region``).

.. _mv-voltage-level:

Nominal voltage
---------------

The common nominal voltage of rural MV grids in Germany is 20 kV (Harnisch et al. 2016), and the
SimBench benchmark grids use 10 kV for the urban grid and 20 kV for the others (SimBench
documentation). With ``MV_NOMINAL_VOLTAGE_KV: auto`` (default), each supply area gets its voltage
from its installed MV/LV capacity per area,

.. math::

   \rho = \frac{\sum S_\mathrm{station} + \sum P_\mathrm{customer} / 0.95}{A},

with the station ratings of the MV model (an upsized station with its larger rating, which covers
its busbar customers), the own peaks of the MV customer nodes after the hall rule (without HV
customers) and the area :math:`A` of the supply area polygon. A supply area with :math:`\rho` of at
least ``mv_10kv_min_density_mva_km2`` (8 MVA/km²) gets 10 kV, every other one 20 kV. The threshold
comes from the structural data that DSOs publish under § 23c EnWG (2025): a large-city 10 kV DSO
has about 8.1 MVA of MV/LV capacity per km² of its whole area, large Bavarian 20 kV DSOs 0.3 to
0.6 MVA/km². It is a whole-area value and not calibrated on supply areas. The values ``20`` and
``10`` fix the voltage of all supply areas instead.

Every voltage-dependent part follows the supply area: the line and HV/MV transformer types, the
ring check, the nets and the ties, which connect supply areas of the same voltage only. The stored
LV grids have 20/0.4 kV transformers. The voltage budget uses their impedances in percent, which
hold for 10 kV as well, and ``pylovo-mv couple`` re-types them to 10 kV in 10 kV supply areas.

.. _mv-equipment:

Equipment
---------

Lines
~~~~~

One cable type and one overhead line type per nominal voltage are used, both pandapower standard
types:

.. list-table::
   :header-rows: 1
   :widths: 14 43 43
   :class: fixed-table

   * - Voltage
     - Cable (``MV_STANDARD_CABLE: null``)
     - Overhead line (``MV_OVERHEAD_LINE: null``)
   * - 20 kV
     - NA2XS2Y 1x150 RM/25 12/20 kV: 0.206 / 0.116 Ω/km, 250 nF/km, 319 A
     - 48-AL1/8-ST1A 20.0: 0.594 / 0.372 Ω/km, 210 A
   * - 10 kV
     - NA2XS2Y 1x150 RM/25 6/10 kV: 0.206 / 0.110 Ω/km, 360 nF/km, 315 A
     - 48-AL1/8-ST1A 10.0: 0.594 / 0.350 Ω/km, 210 A

These are the most used MV conductors in Germany. In the SimBench statistics of 74 real MV grids,
the cable length peaks at 150 mm² and the overhead line length at 50 mm² (SimBench documentation),
and more than 60 % of the cables of four southern German MV grids are 150 to 185 mm² (Almomani and
Witzmann 2024). The heavier types NA2XS2Y 1x240 RM/25 12/20 kV (the 20 kV standard cable of
eDisGo) and 122-AL1/20-ST1A 20.0 can be selected with ``MV_STANDARD_CABLE`` and
``MV_OVERHEAD_LINE``. A configured type must be a pandapower line type of the right kind and
voltage. With ``MV_NOMINAL_VOLTAGE_KV: auto`` it stands for its conductor, and the 10 kV supply
areas use the same conductor with 6/10 kV insulation or the ``10.0`` overhead type. The resolved
values are stored in the variant snapshot, so a stored variant does not change when pandapower's
type tables change. Parallel cables are used only for dedicated feeders and as a repair measure,
at most ``max_parallel_cables`` (2).

HV/MV transformers
~~~~~~~~~~~~~~~~~~

Each substation gets ``hv_mv_units`` (2) transformers of one rating from ``hv_mv_sizes_mva`` (25,
31.5, 40 and 63 MVA). Harnisch et al. (2016) describe two transformers per substation, each sized
for the peak with the other one out of service. The rating is the smallest one for which the
remaining unit carries the larger of the coincident load and the largest feed-in of the supply area
at ``hv_mv_n1_utilization`` (100 %). The normal loading then stays at or below 50 %. The load is the
coincidence law over all nodes of the supply area, divided by the power factor, and never the sum
of the feeder heads, which would overstate it. If even 63 MVA is too small, the substation gets as
many 63 MVA units as the peak needs plus one in reserve (``large_substation``). pandapower has no
31.5 MVA type, so it is interpolated between 25 and 40 MVA and marked ``interpolated`` in the
snapshot.

.. _mv-electrical:

Electrical checks
-----------------

Design net and cases
~~~~~~~~~~~~~~~~~~~~

Every supply area is checked with pandapower. The MV net is built directly, because the electrical
backend of the LV stage is fixed to 20/0.4 kV. The design net has an ideal slack at the MV busbar
at ``busbar_vm_pu`` (1.02 p.u.), the setpoint of the on-load tap changer (OLTC) of the HV/MV
transformers (Harnisch et al. 2016: 102.5 ± 1.5 %, Wintzek et al. 2021: 102 ± 2 %). Stations and
MV customers are loads, MV generators and the LV-side generation of the stations are static
generators. With an ideal busbar the rings are electrically independent, so the (n-1) cases of all
rings run in two power flows. The five cases follow the SimBench study cases hL, n1, lW and lPV
(Meinecke et al. 2020):

.. list-table::
   :header-rows: 1
   :widths: 17 23 24 36
   :class: fixed-table

   * - Case
     - Loads P / Q
     - Wind / PV / other
     - Topology
   * - ``load``
     - 1 / 1 (half-ring coincidence)
     - 0 / 0 / 0
     - normal operation, open points open
   * - ``n1a``
     - 1 / 1 (ring coincidence)
     - 0 / 0 / 0
     - exit A of every ring and leg 1 of every dedicated feeder out of service, all open points
       closed
   * - ``n1b``
     - 1 / 1 (ring coincidence)
     - 0 / 0 / 0
     - exit B of every ring and leg 2 out of service, all open points closed
   * - ``feed_in_wind``
     - 0.1 / 0.123
     - 1.0 / 0.8 / 1.0
     - normal operation
   * - ``feed_in_pv``
     - 0.1 / 0.123
     - 0.85 / 0.95 / 1.0
     - normal operation

MV generators run at ``mv_gen_cos_phi`` (0.95) under-excited, an assumption that follows common
practice under VDE-AR-N 4110 and lowers the voltage rise. The LV-side generation runs at
``lv_pv_cos_phi`` (1.0).

Criteria
~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 36 40 24
   :class: fixed-table

   * - Quantity
     - Limit (default)
     - Cases
   * - Voltage of every MV node
     - at least ``busbar_vm_pu`` minus ``MV_MAX_VOLTAGE_DROP_PERCENT`` (0.97 p.u.)
     - ``load``
   * - Voltage of every node with an (n-1) reserve
     - at least ``busbar_vm_pu`` minus ``max_drop_n1_percent`` (0.92 p.u.)
     - ``n1a``, ``n1b``
   * - Voltage of every MV node
     - at most ``busbar_vm_pu`` plus ``max_rise_percent`` (1.04 p.u.)
     - ``feed_in_wind``, ``feed_in_pv``
   * - Line loading
     - at most ``MV_LINE_PLANNING_UTILIZATION`` (60 %)
     - ``load``
   * - Line loading
     - at most ``n1_utilization`` (100 %)
     - ``n1a``, ``n1b`` and the feed-in cases

The MV share of the voltage band in normal operation (5 %) and the generation-driven rise of 2 %
follow Harnisch et al. (2016), SimBench and Ding0. The 2 % rise is the limit of VDE-AR-N 4110 (dena
2012). Nodes on stubs, T-offs, busbar radials and generator feeders have no (n-1) requirement. The
stations and customers among them are flagged ``not_n1_secure``.

Repair loop
~~~~~~~~~~~

Violations are repaired in up to ``repair_rounds`` (4) rounds. Each round applies all actions that
do not conflict, at most one per ring, and all cases run again afterwards:

.. list-table::
   :header-rows: 1
   :widths: 38 62
   :class: fixed-table

   * - Violation
     - Repair
   * - Voltage on a ring (``load``, ``n1a``, ``n1b``)
     - Move the open point to its best position. If it is there already, split the ring: its
       members are solved again with at most half of them per ring. As a last step, one more
       parallel cable on both exits.
   * - Loading of a ring section
     - Split the ring if the section is a busbar exit and the ring has at least 4 members, else one
       more parallel cable, up to ``max_parallel_cables``.
   * - Loading or voltage on a dedicated feeder, generator feeder or busbar radial
     - One more parallel cable, else the report ``dedicated_overload``.
   * - Rise or loading in a feed-in case on a feeder with a T-off or looped-in generator
     - Give the generator with the largest feed-in a dedicated feeder (one per feeder and round).
   * - Rise caused by LV-side generation alone
     - Report ``lv_pv_rise``, no repair.
   * - A case does not converge
     - Report, the topology is kept.

Rises caused by LV-side PV are not repaired, because real grids keep them within the band by
measures that the model does not represent, such as cos φ(P) control of the inverters and regulated
MV/LV transformers. The repair loop never changes line types. A supply area is stored with its
remaining violations, as the LV stage stores its grids, and ``mv_grid.power_flow_status`` is
``converged``, ``voltage_violation``, ``loading_violation`` or ``not_converged``.

Stored net and OLTC
~~~~~~~~~~~~~~~~~~~

The stored net of a supply area (``mv_grid.net``, pandapower JSON) adds the HV side to the design
net: a 110 kV bus with an external grid at 1.0 p.u. and the sized HV/MV transformers. Their OLTC tap
(±9 steps of 1.5 %) is fixed at the position that brings the MV busbar near 1.02 p.u. in the load
case. A plain power flow of the stored net therefore reproduces the load case without a controller.

.. _mv-voltage-budget:

Voltage budget and MV/LV taps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

pylovo designs each LV grid for an LV busbar voltage of ``LV_REFERENCE_VOLTAGE_PU`` (0.96 p.u.) at
its own station-coincident load (:ref:`validation-snapshot`). The stored LV net keeps the MV-side
voltage :math:`r` that achieves this with a neutral tap, and the MV grid has to deliver it. The
off-load tap of an MV/LV transformer (±2 steps of 2.5 %) can shift the LV side, but one tap must
serve both the load and the feed-in extreme. The stage therefore chooses one tap :math:`t` per
station from the MV voltages of the cases:

.. math::

   \Delta_T &= r - v_\mathrm{ref}, \qquad k(t) = \frac{1}{1 + t\,s}, \\
   v_\mathrm{LV}(t) &= (v_\mathrm{load} - b)\,k(t) - \Delta_T, \qquad
   v_\mathrm{LV,feed}(t) = (v_\mathrm{feed} + b)\,k(t) + \Delta v_T, \\
   m_\mathrm{low}(t) &= v_\mathrm{LV}(t) - (v_\mathrm{ref} - \varepsilon), \qquad
   m_\mathrm{high}(t) = v_\mathrm{max} - v_\mathrm{LV,feed}(t).

Here :math:`\Delta_T` is the drop over the station transformer at the LV design load (scaled to the
larger rating of an upsized station), :math:`s` is the tap step (``mvlv_tap_step_percent``, 2.5 %)
and :math:`t < 0` raises the LV side. :math:`v_\mathrm{load}` is the MV voltage of the station in
the load case and :math:`v_\mathrm{feed}` the higher one of the two feed-in cases. The busbar band
:math:`b` (``busbar_band_pu``, 0.015) puts the busbar at the lower edge of the OLTC band in the load
case and at the upper edge in feed-in. :math:`\Delta v_T` is the rise over the station transformer
under reverse flow. :math:`\varepsilon` (``lv_design_tolerance_pu``, 0.002) is a small tolerance
below the reference, and :math:`v_\mathrm{max}` (``lv_feed_in_max_pu``, 1.07 p.u.) is the upper
band limit of 1.10 p.u. minus the 3 % rise that VDE-AR-N 4105 allows inside the LV grid.

The tap :math:`t^*` maximises :math:`\min(m_\mathrm{low}, m_\mathrm{high})` from ``mvlv_tap_min``
to ``mvlv_tap_max`` (-2 to +2), which centres the LV busbar between the load and the feed-in bound.
Ties go to the tap closest to 0. A station has a ``band_conflict`` if even :math:`t^*` leaves a
negative margin, and ``tap_limit`` if the load-side margin is negative. A typical case is a distant
rural station with much PV, whose load case needs a raising tap and whose feed-in case needs a
lowering one. The results are stored per station in ``mv_node``: ``mv_lv_tap_pos``,
``lv_busbar_vm_pu``, ``lv_min_vm_pu`` (the LV busbar voltage minus the largest drop inside the
stored LV grid) and ``lv_busbar_vm_feed_in_pu``. The budget superposes the MV voltages and the
stored LV designs, and ``pylovo-mv couple`` verifies it with the LV nets.

.. _mv-coupling:

Coupling with the LV grids
--------------------------

``pylovo-mv couple`` checks the voltage budget with the stored LV nets and computes the operating
point of the joint MV and LV grid of every done supply area. It runs after ``pylovo-mv generate``
and never changes the MV or LV grids:

#. The design-point check takes one LV net at a time. The stored LV net with its own loads, the
   tap :math:`t^*` and the MV voltage :math:`v_\mathrm{load} - b` at its station gives
   ``coupled_lv_busbar_vm_pu`` and ``coupled_lv_min_vm_pu``. The feed-in cases (MV voltage
   :math:`v_\mathrm{feed} + b`, loads scaled by the case factors, the LV-side generation of the
   station at the LV busbar) give ``coupled_lv_busbar_vm_feed_in_pu`` and ``coupled_lv_max_vm_pu``.
   These values are the verdict for the LV grids, because each LV grid is designed for its own
   peak.
#. The merged operating point joins all LV nets of a supply area under its MV net. Each station
   transformer hangs on its MV node, the MV station loads are removed and the busbar customers
   become fixed loads at the LV busbar. The LV loads are scaled per MV feeder and consumer category
   with pylovo's law, so the feeder heads carry the loads of the MV design. The busbar is an ideal
   slack at the lower band edge (load) or the upper band edge (feed-in). The result per station is
   ``coupled_op_lv_busbar_vm_pu``. The difference of the MV voltages to the MV-only net is stored
   as a consistency check in ``mv_grid.metrics['coupling']``.

In 10 kV supply areas the LV transformers are re-typed to 10 kV on their MV side, and a station
upsized for its busbar customers gets the larger transformer. An LV net whose own check does not
converge is left out of the merged flow (its station keeps its MV load), and the supply area is
then ``partial``. The merged net is not stored, but ``pylovo-mv export --coupled`` rebuilds it for
one supply area.

Limitations
-----------

* The stage plans open rings, stubs, busbar radials and dedicated feeders only. Closed rings,
  meshed operation, chords between rings and support or remote stations, which urban grids have
  (Kreutmayr 2023), are not modelled.
* Every ring starts and ends at the busbar of one substation. Rings between two substations (A-B
  rings) are not formed, and the ties represent the reserve between substations only as
  normally-open lines outside the checked cases.
* There is no short-circuit check: short-circuit power, the reach of the protection at feeder ends
  and the earth-fault compensation are not checked. The plausibility notebook only estimates the
  earth-fault current from the cable length.
* The non-residential loads are area-based: floor areas times specific peaks per m² of the LV
  stage. Large sites can dominate dense supply areas and fill their substations, and the hall rule
  and the station-busbar customers reduce this effect only in part. Non-residential buildings that
  contain a transformer candidate are removed by the LV stage before loads exist and never become
  MV customers.
* The checks use five snapshot cases and no time series.
* One cable and one overhead type are used per voltage. The repair loop adds parallel cables and
  splits rings but never upgrades types, so long rural feeders forced to 10 kV can keep voltage
  violations. Transformer ratings mapped in OSM are not read.
* Overhead lines are not cabled inside settlements (mixed lines), so a straight span between two
  non-urban ends can cross a settlement.
* The LV grids are not changed. The stage chooses MV/LV taps and upsizes stations in the MV model
  only, but it does not reinforce LV grids.
* Streets outside the municipalities of the routing graph are missing, and supply areas cut by the
  region border are not complete.

.. _mv-references:

References
----------

Methods and models:

* J. Amme, G. Pleßmann, J. Bühler, L. Hülk, E. Kötter and P. Schwaegerl (2018), `The eGo grid
  model: An open-source and open-data based synthetic medium-voltage grid model for distribution
  power supply systems <https://doi.org/10.1088/1742-6596/977/1/012007>`_, *Journal of Physics:
  Conference Series* 977, 012007. The open-ring method of Ding0, cited as method reference.
* L. Hülk, L. Wienholt, I. Cußmann, U. P. Müller, C. Matke and E. Kötter (2017), `Allocation of
  annual electricity consumption and power generation capacities across multiple voltage levels in
  a high spatial resolution <https://doi.org/10.5278/ijsepm.2017.13.6>`_, *International Journal of
  Sustainable Energy Planning and Management* 13, 79–92. Filter of the HV/MV substations in
  OpenStreetMap.
* P. Dubielzig (2022), `Modellierung synthetischer Verteilnetztopologien in urbanen Gebieten
  <https://reiner-lemoine-institut.de/wp-content/uploads/2023/05/2022_MA_Paul_Dubielzig_UrbaneVerteilnetze.pdf>`_,
  master thesis, Technische Universität Berlin, published by the Reiner Lemoine Institut.
* I. Cußmann et al. (2024), final report of the eGon project,
  `doi:10.5281/zenodo.10677901 <https://doi.org/10.5281/zenodo.10677901>`_, and the eGon-data
  pipeline (`github.com/openego/eGon-data <https://github.com/openego/eGon-data>`_). Thresholds of
  the voltage levels, HV/MV substations and MV grid districts.
* F. Klabunde, C. Reinhold and B. Engel (2022), *Regionsabhängige Energiesystemanalysen auf Basis
  einer datengesteuerten Verteilnetzmodellierung*, 17. Symposium Energieinnovation, Graz. Open
  point at the voltage balance.
* G. Clarke and J. W. Wright (1964), Scheduling of vehicles from a central depot to a number of
  delivery points, *Operations Research* 12(4), 568–581.
* B. E. Gillett and L. R. Miller (1974), A heuristic algorithm for the vehicle-dispatch problem,
  *Operations Research* 22(2), 340–349.
* P. Toth and D. Vigo (2003), The granular tabu search and its application to the vehicle-routing
  problem, *INFORMS Journal on Computing* 15(4), 333–346.
* O. Bräysy and M. Gendreau (2005), Vehicle routing problem with time windows, part I: route
  construction and local search algorithms, *Transportation Science* 39(1), 104–118.

Planning principles and statistics:

* S. Meinecke et al. (2020), `SimBench: A benchmark dataset of electric power systems to compare
  innovative solutions based on power flow analysis <https://doi.org/10.3390/en13123290>`_,
  *Energies* 13(12), 3290, and the SimBench documentation (`simbench.de <https://simbench.de>`_).
  Study cases and conductor statistics of 74 real MV grids.
* S. Meinecke, A. Klettke, D. Sarajlić, J. Dickert, M. Hable, F. Fischer, M. Braun and A. Moser
  (2019), `General planning and operational principles in German distribution systems used for
  SimBench <https://doi.org/10.34890/752>`_, CIRED 2019.
* Deutsche Energie-Agentur (dena) (2012), *Ausbau- und Innovationsbedarf der Stromverteilnetze in
  Deutschland bis 2030* (dena-Verteilnetzstudie), final report, Berlin.
* J. Büchner et al. (2014), *Moderne Verteilernetze für Deutschland* (Verteilernetzstudie), study
  for the Federal Ministry for Economic Affairs and Energy (BMWi).
* S. Harnisch et al. (2016), *Planungs- und Betriebsgrundsätze für ländliche Verteilungsnetze*,
  Neue Energie aus Wuppertal, vol. 8.
* P. Wintzek, S. A. Ali, J. Monscheidt, B. Gemsjäger, A. Slupinski and M. Zdrallek (2021),
  *Planungs- und Betriebsgrundsätze für städtische Verteilnetze*, Neue Energie aus Wuppertal,
  vol. 35.
* M. Weisenstein and W.-H. Wellßow (2021), *Vermaschungsgrad der Verteilnetze in Deutschland*,
  TU Kaiserslautern.
* S. Kreutmayr (2023), `Elektromobilität im städtischen Verteilnetz: Modellierung und Auswirkungen
  auf die Nieder- und Mittelspannungsebene <https://mediatum.ub.tum.de/doc/1702040>`_, dissertation,
  Technische Universität München.
* T. Almomani and R. Witzmann (2024), *Statistische Analyse von Mittelspannungsnetzen nach
  Versorgungsaufgabe*, 18. Symposium Energieinnovation, Graz.

Data:

* Bundesnetzagentur, `Marktstammdatenregister <https://www.marktstammdatenregister.de>`_ (MaStR),
  licence dl-de/by-2-0, downloaded with `open-mastr <https://open-mastr.readthedocs.io>`_.
* OpenStreetMap contributors, power and land-use data (ODbL), imported into the InfDB.
