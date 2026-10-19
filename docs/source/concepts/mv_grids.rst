Medium-voltage grids
====================

The MV stage connects the stations of a generated LV version to the HV/MV substations of its region
with medium-voltage (MV) grids. Every LV grid of the version becomes one MV node at its station.
The large non-residential consumers that the LV stage leaves to the MV grid (MV customers)
are added as further nodes. By default, the stage plans for normal load and N-1 only.
``MV_INCLUDE_GENERATION: true`` adds MaStR generator nodes, LV-side generation and feed-in cases
for an explicit generation-enabled variant. The stage plans open rings along the streets, sizes the lines and the HV/MV transformers and
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
   MV generators of MaStR only when generation is enabled (:ref:`mv-generators`).
#. Attach all nodes and substations to the routing graph, split the region into supply areas, one
   per substation (:ref:`mv-supply-areas`), and give each supply area its nominal voltage
   (:ref:`mv-voltage-level`).
#. Plan every supply area on its own, in parallel worker processes: rings with normally-open
   points, rural stubs, feeders of large customers, line types, three load/N-1 power-flow
   cases (five including feed-in when generation is enabled), a repair loop, the HV/MV transformers and the voltage budget of every station
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
   ``MV_MAX_STATIONS_PER_RING`` limits the members. An urban member counts 1, a rural member
   1 / ``rural_ring_factor`` (½), so rural rings hold up to twice as many members. Joints and stub
   nodes do not count.
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
cross-check (``mv_substation.egon_bus_id``). A site without one gets the flag ``no_egon_bus``. The
eGon bus id of an HV/MV substation is the id of its 110 kV bus in the eTraGo grid model of
eGon-data, so it links the MV grid to the HV grid (``pylovo-mv boundary``). With
``MV_SUBSTATIONS: egon``, the eGon-data substations themselves are the sites, since eGon-data has
filtered and aggregated them already.

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

All rules start from the *street distance*: the distance along the routing graph from the busbar of
a substation to a node. With ``MV_SUPPLY_AREAS: routed`` (default), every node goes to the
substation that it reaches first along the streets. This is a Voronoi partition on the street graph
instead of the plane. Since a node on the shortest path to a node of a substation belongs to the
same substation, the supply areas stay connected along the streets.

No rule moves load because of the capacity of a substation. The coincident load of each substation
(stations and MV customers of its area, divided by the power factor) is compared with its (n-1)
capacity, (``hv_mv_units`` - 1) units of the largest of ``hv_mv_sizes_mva`` (63 MVA with the
defaults). A substation above it gets the flag ``over_capacity`` and more transformer units
(:ref:`mv-equipment`). Earlier versions handed the outer nodes of an overloaded substation to its
neighbours (``rebalance_max_iter`` above 0, routed areas only). Because the area-based loads of the
LV stage are high, this moved whole towns to substations 8 to 10 km away. Against the data of two
DSOs, the feeders of the checked town then started at three substations instead of its own one,
with a median supply path of 8.4 km against 2.7 km real. Without the rebalancing, the town hangs on
its own substation with a median supply path of 2.8 km, so it is off by default. Generators and HV
customers are assigned by the same rules but do not count for the capacity. A node without any
street path to a substation goes to the nearest substation in straight line (column
``mv_node.bridged``).

With ``MV_SUPPLY_AREAS: municipal``, municipalities stay whole, as in the MV grid districts of
open_eGo and Ding0 (Hülk et al. 2017), applied to street distances instead of Voronoi polygons:

#. A municipality with exactly one substation is supplied by that substation.
#. A municipality with several substations is split among them by the street distance to these
   substations alone.
#. A municipality without a substation goes as a whole to the substation that is nearest along the
   streets to the largest part of its coincident load.

The municipality of a node is the one of its street edge (``ways_per_junction.ags``). A substation
belongs to the municipality of its street edge if it lies within ``municipal_site_max_offset_m``
(250 m) of it, else to none. Nodes without a municipality go to their nearest substation. Hülk et
al. argue that concessions for MV grids were mostly awarded per municipality. On the test regions
the rule was not closer to the DSO data than the routed areas, and it was 4.7 % longer: a city whose
other substations lie in neighbouring municipalities goes as a whole onto the one substation that
OpenStreetMap maps inside it (155 MW on one site), and whole municipalities without a substation
get long feeders.

With ``MV_SUPPLY_AREAS: egon``, which needs ``MV_SUBSTATIONS: egon``, a node goes to the eGon-data
MV grid district that contains it (else to the nearest one), and the depot is the eGon substation
with the same bus id.

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
#. With ``MV_OVERHEAD_LINES: osm`` (default), the MV overhead lines mapped in OpenStreetMap
   (``<INFDB_OSM_SCHEMA>.infrastructure_line`` with ``tags``) become edges of the graph as well.
   A way counts if it is a ``line`` or ``minor_line`` in service with an MV voltage (above 1 kV and
   below 60 kV, also on shared towers with 110 kV), or a ``minor_line`` without a voltage tag that
   is not an LV line. In the test regions, untagged ``minor_line`` ways were 98 % MV by length.
   An OSM line and a street are split where they cross, so the crossing is a common vertex. A line
   is split where a branch line ends on it, and each loose end gets a straight connector to the
   nearest street point within ``osm_line_snap_m`` (100 m). OSM edges are never in a settlement,
   never the attachment edge of a node and not part of the supply-area routing.

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
lines and cables. With ``MV_OVERHEAD_LINES: osm`` (default), every link follows the routing graph,
and a line is an overhead line where its route runs on an OSM MV line and a cable elsewhere. A
line with both parts is a mixed line, with length-weighted resistance and reactance and the smaller
ampacity of its parts. The overhead lines are therefore real corridors, and no line is overhead
where OSM maps none. Where the OSM layer is incomplete, the overhead length is a lower bound.

An existing overhead corridor needs no new trench, so an OSM line costs ``osm_line_cost_factor``
(0.7) times its length when routes and rings are planned. Routes and rings then follow the
corridors where the detour is moderate, while the line lengths, the half-ring limit and the
electrical checks use the true lengths. Without this preference the routes used an OSM line only
where it was the shortest way between two consecutive ring members. In the test regions that gave
385 km of overhead lines and 93 % cable. With 0.7 it gave 1,147 km, within the range that the
§ 23c structural data of the regional DSOs imply (1,080 to 1,690 km), for 1.8 % more route length.
With 0.5 it gave 1,789 km, and several rings shared one OSM line. A real line carries one or two
circuits, and the planner does not limit the circuits per corridor.

With ``MV_OVERHEAD_LINES: rural``, a link is an overhead line if its two ends are not in the same
settlement and neither end is urban (two open-field ends count as different settlements). An
overhead line then takes a straight span with angle poles, ``overhead_detour`` (1.1) times the
straight distance, where this is shorter than the street path. Otherwise it follows the street.
All other links are cables along the streets. Straight spans are synthetic and do not follow real
corridors.

With ``MV_OVERHEAD_LINES: rural``, the busbar legs are always cables along the streets: the links
from the busbar to the first and last member of a ring and to a radial or dedicated node. With
``osm`` they follow the routing graph like every link and become overhead only on an OSM line.
Feeders leave HV/MV substations as cables
in shared trenches, so legs in the same direction share their streets near the substation instead
of fanning out as straight overhead spans. The busbar is joined by straight cable connectors to
every street vertex within ``busbar_exit_radius_m`` (200 m), so the feeders can leave the
substation in several directions.

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
   (c) the stub tree carries at most ``stub_max_kva`` (1 MVA, coincident), (d) every node of the
   tree lies within the stub length of that load in true route length (the routing cost of OSM lines
   does not count here, as for busbar radials and generator T-offs), and (e) the shortened ring
   stays feasible. Later stubs may hang on earlier ones (stub trees). Urban nodes always stay in the
   ring.

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
#. Also with ``MV_STUBS: rural``, the *satellites*, rural stations outside every settlement or alone
   in their settlement such as farms and hamlets, are not part of the ring problem, as the
   satellites of Ding0 (Amme et al. 2018). After the rings are solved, they are connected from the
   one nearest to the busbar outwards. Each hangs on a stub from the nearest line within the stub
   limits above, if its ring stays feasible. Otherwise it goes into the ring where it adds the least
   length and the ring stays feasible (flag ``satellite_in_ring``), else into a ring of its own
   (flag ``satellite_own_ring``). In a supply area where every candidate would be a satellite, none
   is.
#. A rural ring with a single node of at most 1 MVA within the stub length of its load (street
   distance) of the busbar becomes a busbar radial.
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

.. _mv-links:

Links between rings of one supply area
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Real MV grids also join neighbouring rings of the same substation. Many stations have a third
cable to a station of another ring, so that a station can be resupplied in more ways than along its
own ring, and many loops of the meshed grid run between rings instead of through the busbar. In the
two DSO grids checked on 2026-10-19, a fifth to a third of the stations had three or more MV
cables. The rings of the stage have none. With ``MV_INTERCONNECTION: ties``, the stage therefore
adds normally-open links after the ties:

#. The candidates are the same ring members as for the ties.
#. Pairs of candidates of different rings of the same supply area within ``link_max_length_m``
   (500 m) along the streets, in true length, are taken in the order of their length.
#. Each candidate takes at most one link, as a station with a third cable bay. Two rings take at
   most two links, so that two links close a short loop between them.

Links are ``mv_line`` rows with ``role = 'link'``, normally open at their ``to_node``. Like the ties,
they carry no flow in the checked cases and are not part of the district nets or of
``circuit_km``. Their count, length, the rings and supply points with a link and the ring pairs
joined twice are stored in ``mv_variant.summary['links']`` and ``mv_grid.metrics['links']``. The
500 m is an assumption: a cross connection joins neighbouring stations, two to three times the
distance between neighbouring stations.

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
     - NA2XS2Y 1x240 RM/25 12/20 kV: 0.122 / 0.112 Ω/km, 304 nF/km, 421 A
     - 122-AL1/20-ST1A 20.0: 0.2376 / 0.344 Ω/km, 410 A
   * - 10 kV
     - NA2XS2Y 1x240 RM/25 6/10 kV: 0.122 / 0.105 Ω/km, 456 nF/km, 416 A
     - 122-AL1/20-ST1A 10.0: 0.2376 / 0.323 Ω/km, 410 A

These thicker starting types are the selected planning defaults. The former 150 mm² cable and
48-AL1 overhead types remain available through explicit configuration and in historical snapshots. A configured type must be a pandapower line type of the right kind and
voltage. With ``MV_NOMINAL_VOLTAGE_KV: auto`` it stands for its conductor, and the 10 kV supply
areas use the same conductor with 6/10 kV insulation or the ``10.0`` overhead type. The resolved
values are stored in the variant snapshot, so a stored variant does not change when pandapower's
type tables change. Larger conductors and parallel cables are used only for dedicated feeders and
as repair measures (:ref:`mv-repair`), at most ``max_parallel_cables`` (2) parallel cables.

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
rings run in two power flows. Default load-only variants run ``load``, ``n1a`` and ``n1b``.
Generation-enabled variants also run ``feed_in_wind`` and ``feed_in_pv``. The cases follow the
SimBench study cases hL, n1, lW and lPV (Meinecke et al. 2020):

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
       line can grow. Then split the ring: its members are solved again with at most half of them
       per ring. As a last step, one more parallel cable on both exits.
   * - Loading of a ring section
     - A conductor that carries the current. At the largest conductor, split the ring if the section
       is a busbar exit and the ring has at least 4 members, else one more parallel cable, up to
       ``max_parallel_cables``.
   * - Loading or voltage on a dedicated feeder, generator feeder or busbar radial
     - Larger conductors (the overloaded line, or every line of the feeder for voltage), then one
       more parallel cable, else the report ``dedicated_overload``.
   * - Rise or loading in a feed-in case on a ring feeder
     - Larger conductors on the path to the node (or on the overloaded line). At the largest
       conductors, give the T-off or looped-in generator with the largest feed-in a dedicated
       feeder (one per feeder and round); without such a generator, the report ``lv_pv_rise``.
   * - A case does not converge
     - Report, the topology is kept.

Rises caused by LV-side PV get larger conductors but no further measures. Real grids also keep them
within the band by measures that the model does not represent, such as cos φ(P) control of the
inverters and regulated MV/LV transformers. A supply area is stored with its remaining violations,
as the LV stage stores its grids, and ``mv_grid.power_flow_status`` is ``converged``,
``voltage_violation``, ``loading_violation`` or ``not_converged``. The repair actions are kept in
``mv_grid.repairs`` (``upgrade`` with the new types, ``parallel``, ``move_nop``, ``split_ring``,
``convert_to_dedicated``).

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

Load-only variants check the LV design point and merged load case. Feed-in fields are not requested;
empty values do not imply a failed check or a feed-in acceptance result. The feed-in descriptions
below apply when ``MV_INCLUDE_GENERATION`` is enabled.

``pylovo-mv couple`` checks the voltage budget with the stored LV nets and computes the operating
point of the joint MV and LV grid of every done supply area. It runs after ``pylovo-mv generate``
and never changes the MV or LV grids:

#. The design-point check takes one LV net at a time. A copy of the stored LV net includes its own
   loads and fixed station-busbar customer loads with the LV version's power factor. These loads, the
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
upsized for its busbar customers gets the larger transformer. Every LV net is merged, also one
whose own check does not converge; the supply area is then ``partial``. Lines shorter than 1 m get
1 m, because stored LV nets can hold lines of a few micrometres that stop Newton-Raphson, and a
failed Newton-Raphson is repeated with Iwamoto damping. The merged net is not stored, but
``pylovo-mv export --coupled`` rebuilds it for one supply area.

Missing voltage budgets and failed or skipped design/feed-in checks also produce ``partial`` when
using ``--no-merged``. Completion describes the checks performed, not compliance with voltage limits.
Previously stored coupling results are unchanged until ``pylovo-mv couple`` is run again.

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
* There is no short-circuit check: short-circuit power, the reach of the protection at feeder ends
  and the earth-fault compensation are not checked. The plausibility notebook only estimates the
  earth-fault current from the cable length.
* The non-residential loads are area-based: floor areas times specific peaks per m² of the LV
  stage. Large sites can dominate dense supply areas and fill their substations, and the hall rule
  and the station-busbar customers reduce this effect only in part. Non-residential buildings that
  contain a transformer candidate are removed by the LV stage before loads exist and never become
  MV customers.
* The checks use five snapshot cases and no time series.
* One cable and one overhead type are planned per voltage. The repair loop upgrades conductors up
  to the largest pandapower type, adds parallel cables and splits rings; long rural feeders forced to
  10 kV can still keep voltage violations. Transformer ratings mapped in OSM are not read.
* With ``MV_OVERHEAD_LINES: osm`` the overhead lines exist only where OpenStreetMap maps them. Their
  use depends on the completeness of the OSM layer, which varies by district, and MV cables are
  hardly mapped. With ``rural``, a straight span between two non-urban ends can cross a
  settlement.
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
