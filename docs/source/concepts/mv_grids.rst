Medium-voltage grids
====================

The MV stage connects the stations of a generated LV version to the HV/MV substations of its region
with medium-voltage (MV) grids. Every LV grid of the version becomes one MV node at its station.
The large non-residential consumers that the LV stage leaves to the MV grid (MV customers)
are added as further nodes. The stage plans open rings along the streets, sizes the lines and the
HV/MV transformers and checks the result with pandapower power flows for the peak load and the
(n-1) cases. It reads the stored LV results and never changes the LV grids. Its parameters belong
to an *MV variant* of the LV version, so several MV designs of one LV version can be stored and
compared.

This page describes the method. :doc:`../user_guide/mv_grids` explains how to run the stage, which
inputs it needs and where it stores the results. :doc:`../user_guide/mv_parameters` lists every
parameter.

The stage is implemented from scratch in :mod:`pylovo.mv_grid`. It follows published planning
methods and rules, in particular the open-ring model of Ding0 (Amme et al. 2018) and the substation
filter of Hülk et al. (2017). No code of Ding0 (AGPL-3.0) or of another grid generator is used. The
algorithms are reimplemented from the publications and textbooks listed in :ref:`mv-references`.

Method at a glance
------------------

For one MV variant of an LV version, :func:`~pylovo.mv_grid.stage.run_mv_stage` runs these steps:

#. Read the LV results of the requested postcodes: one node per LV grid with its load statistics,
   the MV customers and the stored LV voltage requirement of every station.
#. Build or reuse the routing graph of the region from the regional street graph of InfDB and the
   MV overhead lines of OpenStreetMap, with a settlement flag for every street edge
   (:ref:`mv-routing-graph`).
#. Select the HV/MV substations from OpenStreetMap and match each one to its 110 kV bus in the
   eGon-data grid model (:ref:`mv-substations`).
#. Attach all nodes and substations to the routing graph, give every node to the substation that it
   reaches first along the streets (:ref:`mv-supply-areas`), and give each supply area its nominal
   voltage (:ref:`mv-voltage-level`).
#. Plan every supply area on its own, in parallel worker processes: rings with normally-open
   points, rural stubs and satellites, feeders of large customers, line types, the load case and
   the two (n-1) cases, a repair loop, the HV/MV transformers and the voltage budget of every
   station (:ref:`mv-rings` to :ref:`mv-voltage-budget`).
#. Add normally-open ties between the rings of neighbouring supply areas (:ref:`mv-ties`) and
   normally-open links between the rings of one supply area (:ref:`mv-links`), and store the run
   summary.

The command ``pylovo-mv couple`` can afterwards solve the MV grids together with the stored LV
grids (:ref:`mv-coupling`).

.. note::

   The stage has one method for every step. The alternative methods of earlier development
   versions (other substation sources and supply areas, straight-line routing, a sweep solver,
   other rules for overhead lines, stubs and ties) and the planning of MV generators with feed-in
   cases are kept on the branch ``feature/mv-backup``. Its file ``MV_ALTERNATIVES.md`` explains
   them.

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
   The open switch of a ring. It sits in the ring panel of a station or an MV customer, never at a
   T-joint, which has no switchgear.
Ring member
   A station or MV customer on the main path of a ring. ``MV_MAX_STATIONS_PER_RING`` limits the
   members. An urban member counts 1, a rural member 1 / ``rural_ring_factor`` (½), so rural rings
   hold up to twice as many members. Joints and stub nodes do not count.
Stub (T-off)
   A radial branch from a joint on a ring to a rural node. A stub has no (n-1) reserve.
Satellite
   A rural station outside every settlement or alone in its settlement, such as a farm. Satellites
   are connected after the rings are solved, by a stub where the limits allow it.
Busbar radial
   One radial line from the busbar to a single small rural node.
Dedicated feeder
   A feeder of its own from the busbar to one large MV customer, with two legs.
Tie
   A normally-open line between the rings of two supply areas.
Link
   A normally-open line between two rings of one supply area, or from the end of a stub to another
   feeder.
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
customers, including those at station busbars (:ref:`mv-customers`). For a single station this is
the design load of its LV grid (``p_own_kw``). The station peaks are never added, because their sum
ignores the coincidence between the stations and overstates the load of a ring or a substation. The
law is not monotonic in the non-residential terms (:doc:`grid_dimensioning`), so every section load
is computed and none is inferred from a neighbouring section.

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

#. In the LV stage, a non-residential load component above
   ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` (200 kW installed, the boundary between the MV/LV
   station level and the MV level in eGon-data) leaves the LV grid, and the building is stored in
   ``pylovo.mv_customers``. The residential part of a mixed building stays in its LV grid. The
   threshold is a parameter of the LV version, not of the MV variant. A building with a
   transformer candidate inside (a customer substation) is stored too before it leaves the LV
   input, and so are the MV customers of a postcode that gets no LV grid. A load edit of a mixed building
   (:ref:`http-api-load-editing`) that moves its non-residential part across the threshold inserts or
   deletes this row in the same transaction, and an edit of an MV-direct part updates it; the undo
   restores it.
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

.. _mv-substations:

HV/MV substations
-----------------

The substations come from the current OpenStreetMap data in the InfDB (``INFDB_OSM_SCHEMA``):
every ``power=substation`` node, way or relation within ``region_buffer_m`` (5 km) of the planning
region, which is the union of the requested postcode polygons. Substations outside the region are
needed, because they supply its border areas. The filter follows Hülk et al. (2017, Table 1),
adapted. A candidate is kept if its ``voltage`` tag holds 110 kV or 60 kV and none of these rules
applies. A ``substation=transmission`` footprint without any voltage tag also counts as HV, because
some city utilities tag their 110 kV substations so and leave out the voltage (a point needs the
voltage tag).

#. ``substation`` is ``traction``, ``transition``, ``converter``, ``generation``, ``industrial`` or
   ``minor_distribution``. A ``transmission`` tag with 110 kV is kept, because many HV/MV
   substations of distribution grids are tagged so.
#. ``frequency`` holds 16.7 Hz or 0 (railway or DC).
#. The operator is a railway operator, the name marks a railway substation (Unterwerk, Bahnstrom,
   Umformerwerk, Umrichter), or a ``railway`` tag is set.
#. The candidate lies inside a ``power=plant`` or ``power=generator`` polygon (the substation of a
   power plant or PV park), unless a plant or generator within 100 m is a thermal plant
   (``plant:source`` gas, coal, oil, biomass, biogas, waste, lignite or diesel) or a heating plant
   by its name (Heizkraftwerk, Heizwerk, HKW, BHKW, KWK, CHP). City utilities run their HV/MV
   substations on their combined heat and power and heating plant sites; in a large-city test,
   this rule recovered three of them, each the only HV/MV substation of its supply area.

Kept candidates within ``substation_merge_m`` (75 m) of each other form one site. Its busbar is a
point inside its largest footprint polygon. An extra-high-voltage site (220 kV or more) without a
tagged MV level is dropped if an HV/MV site lies within ``ehv_partner_m`` (1 km), else it is kept
with the flag ``ehv_uncertain``. Of two sites closer than ``substation_drop_m`` (150 m), the one
with the smaller footprint is dropped. The MV level of a site is rarely tagged, so the nominal
voltage comes from the rule of :ref:`mv-voltage-level`. Without any HV/MV substation near the
region, the stage stops.

Each site is matched to the nearest eGon-data HV/MV substation within ``egon_match_m`` (300 m),
measured from its footprint or its point (``mv_substation.egon_bus_id``). The eGon bus id of an
HV/MV substation is the id of its 110 kV bus in the eTraGo grid model of eGon-data, so it links the
MV grid to the HV grid (``pylovo-mv boundary``). A site without a match gets the flag
``no_egon_bus``. Without the eGon-data table ``egon_hvmv_substation`` the match is skipped with a
warning, and no site has a bus.

A substation whose busbar lies more than ``substation_max_attach_m`` (2 km) from the routing graph
supplies nothing (flag ``long_attach``). A substation that supplies no node gets no MV grid (flag
``no_nodes``) but stays in ``mv_substation``. Transformer ratings mapped in OSM are not read, so
every substation is sized by the rule of :ref:`mv-equipment`.

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

Every node goes to the substation whose busbar it reaches first along the routing graph, the one
with the shortest *street distance*. This is a Voronoi partition on the street graph instead of
the plane. Since a node on the shortest path to a node of
a substation belongs to the same substation, the supply areas stay connected along the streets.
The OSM MV lines of the routing graph take no part in this assignment.

No rule moves load because of the capacity of a substation. The coincident load of each substation
(stations and MV customers of its area, divided by the power factor) is compared with its (n-1)
capacity, (``hv_mv_units`` - 1) units of the largest of ``hv_mv_sizes_mva`` (63 MVA with the
defaults). A substation above it keeps its load and gets the flag ``over_capacity`` and more
transformer units (:ref:`mv-equipment`). HV customers are assigned by the same rule but do not
count for the capacity. A node without any street path to a substation goes to the nearest
substation in straight line (column ``mv_node.bridged``).

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
#. The MV overhead lines mapped in OpenStreetMap (``<INFDB_OSM_SCHEMA>.infrastructure_line`` with
   ``tags``) become edges of the graph as well. A way counts if it is a ``line`` or ``minor_line``
   in service with an MV voltage (above 1 kV and below 60 kV, also on shared towers with 110 kV),
   or a ``minor_line`` without a voltage tag that is not an LV line. In the test regions, untagged
   ``minor_line`` ways were 98 % MV by length. An OSM line and a street are split where they cross,
   so the crossing is a common vertex. A line is split where a branch line ends on it, and each
   loose end gets a straight connector to the nearest street point within ``osm_line_snap_m``
   (100 m). OSM edges are never in a settlement, never the attachment edge of a node and not part
   of the supply-area routing. Without these OSM tables the graph holds the streets only.

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
a brownfield station or a substation busbar, gets a straight connector to its street point.
Connectors longer than ``long_connector_m`` (300 m) count ``bridge_detour`` (1.3) times their
length. Graph islands with nodes but without the busbar are joined by straight bridges between
their closest vertices (also 1.3 times their length, column ``mv_node.bridged``) instead of losing
their nodes. Distances and paths come from SciPy's Dijkstra on this graph.

.. _mv-rings:

Rings
-----

Most German MV grids are operated as open rings, about 84 % according to Büchner et al. (2014).
The stage therefore plans open rings from the busbar of each substation.

Line lengths and kinds
~~~~~~~~~~~~~~~~~~~~~~

The planner first computes the design length of every possible link between two nodes of a supply
area: its street distance on the district graph. Meinecke et al. (2019) state that urban MV grids
use cables only, while rural grids mix overhead lines and cables. Every link follows the routing
graph, and a line is an overhead line where its route runs on an OSM MV line and a cable elsewhere.
A line with both parts is a mixed line, with length-weighted resistance and reactance and the
smaller ampacity of its parts. The overhead lines are therefore real corridors, and no line is
overhead where OSM maps none. Where the OSM layer is incomplete, the overhead length is a lower
bound.

An existing overhead corridor needs no new trench, so an OSM line costs ``osm_line_cost_factor``
(0.7) times its length when routes and rings are planned. Routes and rings then follow the
corridors where the detour is moderate, while the line lengths, the half-ring limit and the
electrical checks use the true lengths. Without this preference the routes used an OSM line only
where it was the shortest way between two consecutive ring members. In the test regions that gave
385 km of overhead lines and 93 % cable. With 0.7 it gave 1,147 km, within the range that the
§ 23c structural data of the regional DSOs imply (1,080 to 1,690 km), for 1.8 % more route length.
With 0.5 it gave 1,789 km, and several rings shared one OSM line. A real line carries one or two
circuits, and the planner does not limit the circuits per corridor.

The busbar legs, the links from the busbar to the first and last member of a ring and to a radial
or dedicated node, follow the routing graph like every link and become overhead only on an OSM
line. Near the substation the feeders run as cables in shared trenches: legs in the same direction
share their streets instead of fanning out as straight spans. The busbar is joined by straight
cable connectors to every street vertex within ``busbar_exit_radius_m`` (200 m), so the feeders
can leave the substation in several directions.

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

#. its weighted members are at most ``MV_MAX_STATIONS_PER_RING`` (20): an urban ring holds 20
   members, a rural ring 40 (``rural_ring_factor`` 2). Rural feeders carry 11 to 28 supply points
   (SimBench; Almomani and Witzmann 2024), urban ones about 10,
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
of a station or MV customer next to the open section, never at a T-joint.

The check is analytic and neglects cable charging. pandapower verifies the planned grid afterwards
(:ref:`mv-electrical`). The member cap of 20 follows Ding0 (Amme et al. 2018), and Kreutmayr
(2023) reports 5 to 30 stations per ring for a large-city grid. The planning guidelines of dena
(2012), Harnisch et al. (2016) and Wintzek et al. (2021) load MV lines with at most 60 % in normal
operation, which keeps the reserve for (n-1) operation. SimBench uses 50 % (Meinecke et al. 2019).
The half-ring limit of 28 km is the value of the Ding0 configuration.

Ring solver
~~~~~~~~~~~

Forming the rings of a supply area is a capacitated vehicle routing problem: the busbar is the
depot, the ring members are the customers and the ring check replaces the vehicle capacity. The
objective is the total ring length, both busbar sections included. The rings are built by the
parallel Clarke-Wright savings (Clarke and Wright 1964). Every candidate starts as a ring of its
own. Pairs are merged in the order of their saving :math:`d_{0i} + d_{0j} - d_{ij}` when both are
ring ends and the ring check accepts the merged ring. Above 800 candidates, only the pairs of the
40 nearest neighbours of every node are considered.

A granular local search follows the construction (Toth and Vigo 2003; Bräysy and Gendreau 2005).
For every node and each of its ``neighbours`` (15) nearest nodes, the moves relocate, exchange,
2-opt*, 2-opt and Or-opt are tried. The first move that shortens the total length by more than 1 m
and keeps every changed ring feasible is applied. The search stops after a pass without a move or
after ``local_search_rounds`` (50) passes, and ``0`` switches it off. A wall-clock guard of
``ls_time_guard_s`` (120 s) stops it early and is flagged in the metrics. The solver uses no random
numbers and breaks ties by index, so a run is reproducible unless the guard stops it.

.. _mv-topology:

From rings to lines
~~~~~~~~~~~~~~~~~~~

The planner turns the rings into lines in this order: rings first, then stubs, satellites, busbar
radials and the feeders of large customers.

#. Rings follow their routes on the district graph.
#. A rural station or MV customer leaves its ring for a stub (T-off) from the nearest point of the
   shortened ring if (a) the T-off is longer than ``stub_min_offset_m`` (100 m), (b) leaving the
   ring saves more ring length than the T-off costs, (c) the stub tree carries at most
   ``stub_max_kva`` (1 MVA, coincident), (d) every node of the tree lies within the stub length of
   that load in true route length (the routing cost of OSM lines does not count here, as for
   busbar radials), and (e) the shortened ring stays feasible. Later stubs may hang on earlier ones
   (stub trees). Urban nodes always stay in the ring.

   The stub length shrinks with the load: at most ``stub_max_length_m`` (4 km), and load times
   length at most ``stub_max_kva_km`` (2 MVA·km), so 4 km up to 0.5 MVA, 2.5 km at 0.8 MVA and 2 km
   at 1 MVA. A stub has no reserve: after a fault its stations wait for the repair or for a mobile
   emergency generator, so the energy not supplied grows with the load and with the length to be
   searched and repaired. The 1 MVA is the size of a typical mobile emergency generator (Amme et al.
   2018, after LEW and an FGH report of 2008). The 2 km at 1 MVA follow Consentec (2006), cited by
   Amme et al., whose example rule allowed at most three stations, 0.5 MW and 2 km per stub. Tran et
   al. (2019) found load stubs of 2 to 4 km in real grids, longer for smaller loads. The product rule
   joins these numbers; it is a derived planning rule, not a published one. Harnisch et al. (2016)
   connect stations more than about 100 m off the trunk as stubs.
#. The *satellites*, rural stations outside every settlement or alone in their settlement such as
   farms and hamlets, are not part of the ring problem, as the satellites of Ding0 (Amme et al.
   2018). After the rings are solved, they are connected from the one nearest to the busbar
   outwards. Each hangs on a stub from the nearest line within the stub limits above, if its ring
   stays feasible. Otherwise it goes into the ring where it adds the least length and the ring stays
   feasible (flag ``satellite_in_ring``), else into a ring of its own (flag
   ``satellite_own_ring``). In a supply area where every candidate would be a satellite, none is.
#. A rural ring with a single node of at most 1 MVA within the stub length of its load (route
   length) of the busbar becomes a busbar radial.
#. A customer above 5.5 MW gets a dedicated feeder with two legs from the busbar along the same
   route. Leg 2 is open at the customer, so the two legs together are (n-1) secure. Each leg gets as
   many parallel cables as the load needs at ``MV_LINE_PLANNING_UTILIZATION``, at most
   ``max_parallel_cables`` (2). Beyond that the customer is flagged ``dedicated_overload``.
#. The normally-open points of the final rings are placed, with the joints as explicit positions.
#. The geometries follow the pieces of the district graph. Only the connectors to nodes off the
   street and the bridges between graph islands are straight. Every line is at least 1 m long.

.. _mv-ties:

Ties between supply areas
~~~~~~~~~~~~~~~~~~~~~~~~~

The ring model plans each supply area on its own. Real MV grids, however, have more connections:
cross-ties, several infeeds and support stations, which ring models of single districts lack
(Dubielzig 2022). In a survey of German DSOs, Weisenstein and Wellßow (2021) found that most MV
grids are meshed but operated open. The stage therefore adds normally-open ties to the rings of
neighbouring substations after all supply areas are planned:

#. The candidates are the stations and MV customers looped into the rings of done supply areas.
#. Pairs of candidates of different supply areas with the same nominal voltage within
   ``tie_max_length_m`` (2 km) along the streets are taken in the order of their length, at most
   one tie per ring. A tie follows the routing graph and is an overhead line where its route runs
   on an OSM MV line.
#. A tie belongs to the supply area with the lower ``mv_grid_id`` and is open at the other end.

Ties carry no flow in any checked case. They are not part of the district nets, of ``circuit_km``
or of the district metrics. Their length, the share of rings with a tie and a takeover check are
stored in ``mv_variant.summary['ties']`` and ``mv_grid.metrics['ties']``. The takeover check is
informational. For each tie end, the stored nets of the two supply areas are joined, the tie is
closed and the exit of the half-ring at that end is taken out of service. The drop and the loading
are then compared with the (n-1) limits. Ties of supply areas cut by the region border or with a
substation outside the region are kept and flagged (``incomplete_district``,
``substation_outside_region``).

.. _mv-links:

Links inside a supply area
~~~~~~~~~~~~~~~~~~~~~~~~~~

Real MV grids also join neighbouring rings of the same substation. Many stations have a third
cable to a station of another ring, so that a station can be resupplied in more ways than along its
own ring, and many loops of the meshed grid run between rings instead of through the busbar. Many
rural stations at the end of a stub have a second cable to a station of another feeder, so that
they keep a second infeed. In two real DSO grids, a fifth to a third of the stations had three or
more MV cables, and a tenth to a seventh were stub ends with one cable. The rings of the stage have
no such cables, so the stage adds normally-open links after the ties:

#. **Between rings:** pairs of ring members (the candidates of the ties) of different rings of the
   same supply area within their reach along the streets, in true length, are taken in the order
   of their length. Each ring member takes at most one link, as a station with a third cable bay.
   Two rings take at most two links, so that two links close a short loop between them.
#. **From stub ends** (``link_stub_ends``, on by default): each stub end (a station or MV customer
   on a stub with one MV line) may then take one link within its reach to a ring member or stub
   node of another feeder of its supply area, which can be the other half of its own ring. The
   pairs are again taken in the order of their length, and every node still takes at most one
   link.

The reach follows the settlement type of the PLZ, which the LV stage determines from the households
per building and the distance between houses (``link_max_length_m``):

.. list-table::
   :header-rows: 1
   :widths: 30 25 45
   :class: fixed-table

   * - Settlement type of the PLZ
     - Reach
     - Median distance between neighbouring stations (test region)
   * - rural (1)
     - 1,000 m
     - 320 m
   * - semi-urban (2)
     - 750 m
     - 250 m
   * - urban (3)
     - 500 m
     - 180 m

A link takes the shorter reach of its two ends, and an MV customer takes the type of its PLZ. The
reach is an assumption of about three times the distance between neighbouring stations. Stub ends
lie far from other feeders: in the test region the median straight distance from a stub end to a
station of another feeder was 0.7 km in urban, 1.0 km in semi-urban and 1.4 km in rural PLZ. With
500 m in all PLZ, only 5 % of the stub ends found a link. With 1 km in all PLZ, the town DSO grid
got twice as many loops as it has. With the reach by settlement type, the loops of the rural DSO
grid matched the real ones in count, supply points and length within 15 %, and the town DSO grid
kept about the loops it had without links from stub ends (a third more than real). The links come
to about 5 % of the circuit length.

Links are ``mv_line`` rows with ``role = 'link'``, normally open at their ``to_node``. Like the ties,
they carry no flow in the checked cases and are not part of the district nets or of
``circuit_km``. Their count, length, the rings and supply points with a link, the ring pairs joined
twice and the stub ends with a link are stored in ``mv_variant.summary['links']`` and
``mv_grid.metrics['links']``.

.. _mv-voltage-level:

Nominal voltage
---------------

The common nominal voltage of rural MV grids in Germany is 20 kV (Harnisch et al. 2016), and the
SimBench benchmark grids use 10 kV for the urban grid and 20 kV for the others (SimBench
documentation). The MV voltage is a decision of the grid operator for its whole grid: a city
utility runs 10 kV also in its parks and outskirts. With ``MV_NOMINAL_VOLTAGE_KV: auto`` (default),
the stage therefore looks at the installed MV/LV capacity per area of each municipality,

.. math::

   \rho = \frac{\sum S_\mathrm{station} + \sum P_\mathrm{customer} / 0.95}{A},

with the station ratings of the MV model (an upsized station with its larger rating, which covers
its busbar customers) and the own peaks of the MV customer nodes after the hall rule (without HV
customers) of all nodes on the municipality's streets, and the area :math:`A` of the municipality
inside the planning region, so that a municipality at the region border is not diluted by its
uncovered part. A supply area gets 10 kV when at least half of its installed capacity lies in
municipalities with :math:`\rho` of at least ``mv_10kv_min_density_mva_km2`` (5 MVA/km²), every
other one 20 kV.

The threshold comes from the structural data that DSOs publish under § 23c EnWG (2025): a
large-city 10 kV DSO has about 8.1 MVA of MV/LV capacity per km² of its whole area, large Bavarian
20 kV DSOs 0.3 to 0.6 MVA/km². The stations of pylovo hold about 0.6 of the real capacity (2.3 of
3.9 GVA in the large city), so the large city comes to 8.6 MVA/km² in pylovo, while the densest
towns of the Bavarian test regions, which are 20 kV grids, come to 2.5 to 3.4 MVA/km². The 5
MVA/km² lies between them; it is not calibrated on supply areas. Deciding by the supply area's own
density instead (the rule before stage version 11) gave 20 kV to most supply areas of the large
city, in its parks and outskirts. The values ``20`` and ``10`` fix the voltage of all supply areas
instead.

Every voltage-dependent part follows the supply area: the line and HV/MV transformer types, the
ring check, the nets and the ties, which connect supply areas of the same voltage only. The stored
LV grids have 20/0.4 kV transformers. The voltage budget keeps their impedances in percent also
in 10 kV supply areas. pandapower's 10/0.4 kV standard types have a short-circuit voltage of about
4 % instead of 6 %, so the budget is on the safe side: it expects a larger transformer drop than
a real 10/0.4 kV unit would have. ``pylovo-mv couple`` re-types the stations of 10 kV supply areas
to 10 kV.

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
     - NA2XS2Y 1x240 RM/25 12/20 kV: 0.122 / 0.112 Ω/km, 304 nF/km, 421 A
     - 122-AL1/20-ST1A 20.0: 0.2376 / 0.344 Ω/km, 410 A
   * - 10 kV
     - NA2XS2Y 1x240 RM/25 6/10 kV: 0.122 / 0.105 Ω/km, 456 nF/km, 416 A
     - 122-AL1/20-ST1A 10.0: 0.2376 / 0.323 Ω/km, 410 A

Another type can be configured, for example the narrower NA2XS2Y 1x150 RM/25 cable. It must be a
pandapower line type of the right kind and voltage. With ``MV_NOMINAL_VOLTAGE_KV: auto`` it stands
for its conductor, and the 10 kV supply areas use the same conductor with 6/10 kV insulation or
the ``10.0`` overhead type. The resolved values are stored in the variant snapshot, so a stored
variant does not change when pandapower's type tables change. Dedicated feeders get parallel cables
for their load. Otherwise, larger conductors and parallel cables are used only as repair measures
(:ref:`mv-repair`), at most ``max_parallel_cables`` (2) parallel cables.

HV/MV transformers
~~~~~~~~~~~~~~~~~~

Each substation gets ``hv_mv_units`` (2) transformers of one rating from ``hv_mv_sizes_mva`` (25,
31.5, 40 and 63 MVA). Harnisch et al. (2016) describe two transformers per substation, each sized
for the peak with the other one out of service. The rating is the smallest one for which the
remaining unit carries the coincident load of the supply area at ``hv_mv_n1_utilization``
(100 %). The normal loading then stays at or below 50 %. The load is the coincidence law over all
nodes of the supply area, divided by the power factor, and never the sum of the feeder heads, which
would overstate it. If even 63 MVA is too small, the substation gets as many 63 MVA units as the
peak needs plus one in reserve (``large_substation``). pandapower has no 31.5 MVA type, so it is
interpolated between 25 and 40 MVA and marked ``interpolated`` in the snapshot.

.. _mv-electrical:

Electrical checks
-----------------

Design net and cases
~~~~~~~~~~~~~~~~~~~~

Every supply area is checked with pandapower. The MV net is built directly, because the electrical
backend of the LV stage is fixed to 20/0.4 kV. The design net has an ideal slack at the MV busbar
at ``busbar_vm_pu`` (1.02 p.u.), the setpoint of the on-load tap changer (OLTC) of the HV/MV
transformers (Harnisch et al. 2016: 102.5 ± 1.5 %, Wintzek et al. 2021: 102 ± 2 %). Stations and
MV customers are loads. With an ideal busbar the rings are electrically independent, so the (n-1)
cases of all rings run in two power flows. The stage runs three cases, after the SimBench study
cases hL and n1 (Meinecke et al. 2020):

.. list-table::
   :header-rows: 1
   :widths: 17 33 50
   :class: fixed-table

   * - Case
     - Loads
     - Topology
   * - ``load``
     - peak, half-ring coincidence
     - normal operation, open points open
   * - ``n1a``
     - peak, ring coincidence
     - exit A of every ring and leg 1 of every dedicated feeder out of service, all open points
       closed
   * - ``n1b``
     - peak, ring coincidence
     - exit B of every ring and leg 2 out of service, all open points closed

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
   * - Line loading
     - at most ``MV_LINE_PLANNING_UTILIZATION`` (60 %)
     - ``load``
   * - Line loading
     - at most ``n1_utilization`` (100 %)
     - ``n1a``, ``n1b``

The MV share of the voltage band in normal operation (5 %) follows Harnisch et al. (2016),
SimBench and Ding0. Nodes on stubs and busbar radials have no (n-1) requirement. The stations and
customers among them are flagged ``not_n1_secure``.

.. _mv-repair:

Repair loop
~~~~~~~~~~~

Violations are repaired in up to ``repair_rounds`` (8) rounds. Each round applies all actions that
do not conflict, at most one per ring, and all cases run again afterwards. As in Ding0's
reinforcement (Amme et al. 2018), larger conductors come before changes of the topology: a line
gets the next larger pandapower conductor of its kind and voltage, or for an overload the smallest
one that carries the current within the limit. A mixed line upgrades both parts. The cables end at
NA2XS2Y 1x240, the overhead lines at 243-AL1/39-ST1A (149-AL1/24-ST1A at 10 kV), so with the default
types only overhead lines can grow; the cables then get parallel cables.

.. list-table::
   :header-rows: 1
   :widths: 38 62
   :class: fixed-table

   * - Violation
     - Repair
   * - Voltage on a ring (``load``, ``n1a``, ``n1b``)
     - Move the open point to its best position. If it is there already, the next larger conductor
       on every line of the path from the busbar to the node in that case, round by round while a
       line can grow. Then split the ring: its members and the stations on its stubs are solved
       again with at most about half of them per ring. As a last step, one more parallel cable on
       both exits.
   * - Loading of a ring section
     - A conductor that carries the current. At the largest conductor, split the ring if the section
       is a busbar exit and the ring has at least 4 members, else one more parallel cable, up to
       ``max_parallel_cables``.
   * - Loading or voltage on a dedicated feeder or busbar radial
     - Larger conductors (the overloaded line, or every line of the feeder for voltage), then one
       more parallel cable, else the report ``dedicated_overload``.
   * - A case does not converge
     - Report, the topology is kept.

A supply area is stored with its remaining violations, as the LV stage stores its grids, and
``mv_grid.power_flow_status`` is ``converged``, ``voltage_violation``, ``loading_violation`` or
``not_converged``. The repair actions are kept in ``mv_grid.repairs`` (``upgrade`` with the new
types, ``parallel``, ``move_nop``, ``split_ring``). After a split, the parallel cables and larger
conductors of earlier rounds stay only on the new lines that run on the repaired line, and the ring
checks and open points use the upgraded conductors.

Stored net and OLTC
~~~~~~~~~~~~~~~~~~~

The stored net of a supply area (``mv_grid.net``, pandapower JSON) adds the HV side to the design
net: a 110 kV bus with an external grid at 1.0 p.u. and the sized HV/MV transformers. Their OLTC tap
(±9 steps of 1.5 %) is fixed at the position that brings the MV busbar near 1.02 p.u. in the load
case. A plain power flow of the stored net therefore reproduces the load case without a controller.
The HV bus carries the eTraGo bus id of its substation in the column ``etrago_bus_id``.

.. _mv-voltage-budget:

Voltage budget and MV/LV taps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

pylovo designs each LV grid for an LV busbar voltage of ``LV_REFERENCE_VOLTAGE_PU`` (0.96 p.u.) at
its own station-coincident load (:ref:`validation-snapshot`). The stored LV net keeps the MV-side
voltage :math:`r` that achieves this with a neutral tap, and the MV grid has to deliver it. The
off-load tap of an MV/LV transformer (±2 steps of 2.5 %) can shift the LV side. The stage chooses
one tap :math:`t` per station from the MV voltage of the station in the load case:

.. math::

   \Delta_T &= r - v_\mathrm{ref}, \qquad k(t) = \frac{1}{1 + t\,s}, \\
   v_\mathrm{LV}(t) &= (v_\mathrm{load} - b)\,k(t) - \Delta_T, \qquad
   m(t) = v_\mathrm{LV}(t) - (v_\mathrm{ref} - \varepsilon).

Here :math:`\Delta_T` is the drop over the station transformer at the LV design load (scaled to the
larger rating of an upsized station), :math:`s` is the tap step (``mvlv_tap_step_percent``, 2.5 %)
and :math:`t < 0` raises the LV side. :math:`v_\mathrm{load}` is the MV voltage of the station in
the load case. The busbar band :math:`b` (``busbar_band_pu``, 0.015) puts the busbar at the lower
edge of the OLTC band. :math:`\varepsilon` (``lv_design_tolerance_pu``, 0.002) is a small tolerance
below the reference.

The tap :math:`t^*` is the one closest to 0 with :math:`m(t) \ge 0`, from ``mvlv_tap_min`` to
``mvlv_tap_max`` (-2 to +2): the neutral tap where it suffices, else the smallest raising tap. If no
tap reaches the reference, :math:`t^*` is the most raising tap, and the station gets the flags
``band_conflict`` and ``tap_limit``. The results are stored per station in ``mv_node``:
``mv_lv_tap_pos``, ``lv_busbar_vm_pu`` and ``lv_min_vm_pu`` (the LV busbar voltage minus the
largest drop inside the stored LV grid). The budget superposes the MV voltages and the stored LV
designs, and ``pylovo-mv couple`` verifies it with the LV nets.

.. _mv-coupling:

Coupling with the LV grids
--------------------------

``pylovo-mv couple`` checks the voltage budget with the stored LV nets and computes the operating
point of the joint MV and LV grid of every done supply area. It runs after ``pylovo-mv generate``
and never changes the MV or LV grids:

#. The design-point check takes one LV net at a time. A copy of the stored LV net with its own
   loads, the fixed loads of its station-busbar customers (with the power factor of the LV
   version), the tap :math:`t^*` and the MV voltage :math:`v_\mathrm{load} - b` at its station
   gives ``coupled_lv_busbar_vm_pu`` and ``coupled_lv_min_vm_pu``. These values are the verdict for
   the LV grids, because each LV grid is designed for its own peak.
#. The merged operating point joins all LV nets of a supply area under its MV net. Each station
   transformer hangs on its MV node, the MV station loads are removed and the busbar customers
   become fixed loads at the LV busbar. The LV loads are scaled per MV feeder and consumer category
   with pylovo's law, so the feeder heads carry the loads of the MV design. The busbar is an ideal
   slack at the lower band edge, and the HV side is left out. The result per station is
   ``coupled_op_lv_busbar_vm_pu``. The difference of the MV voltages to the MV-only net is stored
   as a consistency check in ``mv_grid.metrics['coupling']``.

In 10 kV supply areas the LV transformers are re-typed to 10 kV on their MV side, and a station
upsized for its busbar customers gets the larger transformer. Every LV net is merged, also one
whose own check does not converge. Lines shorter than 1 m get 1 m, because stored LV nets can hold
lines of a few micrometres that stop Newton-Raphson, and a failed Newton-Raphson is repeated with
Iwamoto damping. The merged net is not stored, but ``pylovo-mv export --coupled`` rebuilds it for
one supply area.

A supply area is ``partial`` when a station with an LV grid could not be coupled, has no voltage
budget or has a skipped or non-converging design-point check (also with ``--no-merged``), or when
the merged power flow does not converge. The status describes the checks performed, not
compliance with voltage limits. Stored coupling results stay unchanged until ``pylovo-mv couple``
is run again. A retired variant, planned with an option that the stage no longer has, is not
coupled (:ref:`mv-variants`).

.. _mv-osm-coverage:

OSM completeness
----------------

After the supply areas are planned, the stage reports how complete the OSM power layer of the region
is (``mv_variant.summary['osm_coverage']`` and one summary line), after the land-use comparison of
Babli et al. (2025). The planning never reads it. It shows how far the OSM-based parts can be
trusted: the overhead lines on OSM corridors and the brownfield stations of the LV stage.

* **Mapped MV/LV station sites**: ``power=substation`` features that are no site of a higher level
  (no voltage above 30 kV; not transmission, traction, converter, generation or compensation; a
  polygon of at least 1,000 m² counts as an HV/MV site unless its voltages are MV only),
  ``power=transformer`` features up to 30 kV outside the HV/MV sites, ``building=transformer_tower``
  and ``power=pole`` nodes with a ``transformer`` tag. Features out of service do not count, and
  features within 15 m of each other are one site.
* **Per class**: the stations of the variant, the mapped sites, their ratio, and the OSM MV line km
  and km per km². The classes are the settlement classes of the routing graph (town, village, open
  field) and the OSM land-use classes (industrial, commercial, residential, agricultural, forest,
  other, none).

On the test regions, OSM mapped 1,270 station sites for 4,580 planned stations (28 %: town 22 %,
village 30 %, open field 37 %) and 1,357 km of MV overhead line. Against the stations of two DSOs,
the ratio per settlement class matched the true share of mapped stations within 2 points where the
planned count was right, and overstated it by 5 to 9 points elsewhere.

Limitations
-----------

* The stage plans open rings, stubs, busbar radials and dedicated feeders only, with normally-open
  ties and links between rings. Closed rings, meshed operation and support or remote stations,
  which urban grids have (Kreutmayr 2023), are not modelled.
* Every ring starts and ends at the busbar of one substation. Rings between two substations (A-B
  rings) are not formed, and the ties represent the reserve between substations only as
  normally-open lines outside the checked cases.
* Every station belongs to its nearest HV/MV substation along the streets. A DSO may feed its area
  from fewer substations or from one outside it. In one checked rural DSO area, five substations
  of the stage supply the stations that the DSO feeds from one infeed. Open data do not tell which
  substation feeds which area, so rural supply paths can be shorter than real ones.
* The stage plans for load only. MV generators, the PV of the LV grids and reverse power flows are
  neither planned nor checked, so the results say nothing about the feed-in that a grid can take.
* There is no short-circuit check: short-circuit power, the reach of the protection at feeder ends
  and the earth-fault compensation are not checked. :mod:`pylovo.analysis.mv_validation` only
  estimates the earth-fault current from the cable and overhead length.
* The non-residential loads are area-based: floor areas times specific peaks per m² of the LV
  stage. Large sites can dominate dense supply areas and fill their substations, and the hall rule
  and the station-busbar customers reduce this effect only in part. Non-residential buildings that
  contain a transformer candidate are removed by the LV stage before loads exist and never become
  MV customers.
* The checks use three snapshot cases (the peak load and both (n-1) directions) and no time series.
* One cable and one overhead type are planned per voltage. The repair loop upgrades conductors up
  to the largest pandapower type, adds parallel cables and splits rings; long rural feeders forced to
  10 kV can still keep voltage violations. Transformer ratings mapped in OSM are not read.
* The overhead lines exist only where OpenStreetMap maps them. Their use depends on the completeness
  of the OSM layer, which varies by district, and MV cables are hardly mapped.
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
* J. Tran, C. Wirtz, P. Pfeifer, D. Wursthorn, H. Vennegeerts and A. Moser (2019), `Modelling of
  synthetic power distribution systems in consideration of the local electricity supply task
  <https://doi.org/10.34890/956>`_, CIRED 2019, Madrid, paper 2107. Lengths of load stubs in real
  grids.
* M. Babli, M. Gebhard and E. Brucherseifer (2025), `Open data-driven reconstruction of power
  distribution grid: a land use-based approach <https://doi.org/10.3390/electronics14071414>`_,
  *Electronics* 14(7), 1414. Completeness of the OSM stations per land-use class.
* P. Dubielzig (2022), `Modellierung synthetischer Verteilnetztopologien in urbanen Gebieten
  <https://reiner-lemoine-institut.de/wp-content/uploads/2023/05/2022_MA_Paul_Dubielzig_UrbaneVerteilnetze.pdf>`_,
  master thesis, Technische Universität Berlin, published by the Reiner Lemoine Institut.
* I. Cußmann et al. (2024), final report of the eGon project,
  `doi:10.5281/zenodo.10677901 <https://doi.org/10.5281/zenodo.10677901>`_, and the eGon-data
  pipeline (`github.com/openego/eGon-data <https://github.com/openego/eGon-data>`_). Thresholds of
  the voltage levels and the HV/MV substations with their eTraGo buses.
* F. Klabunde, C. Reinhold and B. Engel (2022), *Regionsabhängige Energiesystemanalysen auf Basis
  einer datengesteuerten Verteilnetzmodellierung*, 17. Symposium Energieinnovation, Graz. Open
  point at the voltage balance.
* G. Clarke and J. W. Wright (1964), Scheduling of vehicles from a central depot to a number of
  delivery points, *Operations Research* 12(4), 568–581.
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

* OpenStreetMap contributors, power and land-use data (ODbL), imported into the InfDB.
* eGon-data HV/MV substations with their eTraGo bus ids (see Cußmann et al. 2024 above).
