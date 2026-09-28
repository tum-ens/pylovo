Grid generation pipeline
========================

This page follows :meth:`GridGenerator.generate_grid <pylovo.grid_generator.GridGenerator.generate_grid>`
through one postcode area. The figures show the OSM-derived demo region PLZ 85653 (Aying): 522
input buildings, of which 468 are supplied. Unless stated otherwise they use the shipped
configuration (version ``1``); two illustrative versions change one setting each:
``docs_bf`` (``USE_OPEN_TRANSFORMER_POSITIONS: True``) and ``docs_km``
(``MAX_BUILDINGS_PER_KCID: 150``). All maps are made with ``docs/scripts/plot_generation_steps.py``
from the result tables; demo data derived from OpenStreetMap, © OpenStreetMap contributors, ODbL.

.. figure:: /images/diagrams/pipeline.*
   :alt: Pipeline overview with five phases and the tables they write
   :width: 100%

   The five phases of the pipeline and the tables they write.

Before the first step, the :class:`~pylovo.grid_generator.GridGenerator` stores or checks the
parameter snapshot of ``VERSION_ID`` (:ref:`versions`). A postcode that already has grids in the
version is skipped. The postcode gets its temporary tables ``buildings_tem_<plz>`` and
``ways_tem_<plz>``, and the equipment catalogue and consumer categories of the configuration are
written to ``equipment_data`` and ``consumer_categories``.

Phase 1: prepare
----------------

1 -- Postcode area
~~~~~~~~~~~~~~~~~~

The postcode polygon is copied from ``pylovo.postcode`` to ``postcode_result`` (in InfDB mode a
missing postcode is fetched from ``postcodes_germany`` first).

2 -- Buildings and loads
~~~~~~~~~~~~~~~~~~~~~~~~

The buildings of the postcode are read (:doc:`../user_guide/input_data`) and cleaned: LoD2
transformer stations become transformer candidates, non-residential buildings on top of
transformer candidates, duplicates and buildings without load are removed. Each building gets its
residential and non-residential load components; missing household counts and floor areas are
filled.

The **settlement type** of the postcode is derived from two metrics, stored in
``postcode_result``:

* the average number of households per residential building,
* the house distance: the mean distance from every building to its four nearest neighbours.

Both are normalised to [0, 1] between the rural and urban thresholds (``RURAL_MAX_HOUSEHOLDS`` ...
``URBAN_MIN_HOUSEHOLDS`` and ``RURAL_MIN_BUILDING_DISTANCE`` ... ``URBAN_MAX_BUILDING_DISTANCE``,
distance inverted) and averaged. A score below 1/3 gives type 1 (rural), below 2/3 type 2
(semi-urban), otherwise type 3 (urban). The type selects the allowed transformer ratings in
``TRANSFORMER_MAPPING``. Aying has 1.70 households per building and a house distance of 28.9 m,
which gives type 3.

Finally, non-residential components above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` are marked as
supplied from the MV grid and removed from the LV load.

3 -- Transformer candidates
~~~~~~~~~~~~~~~~~~~~~~~~~~~

If ``USE_OPEN_TRANSFORMER_POSITIONS``, ``USE_DSO_TRANSFORMER_POSITIONS`` or
``USE_MANUAL_TRANSFORMER_POSITIONS`` is set, the selected
candidates inside the postcode polygon are added to the building table as points of type
``Transformer`` (:doc:`../user_guide/transformer_data`). Building rows that represent the
transformers themselves are removed, and candidates inside MV-supplied buildings are dropped.

.. figure:: /images/generation/step1_inputs.png
   :alt: Buildings coloured by use, street network and five transformer candidates
   :width: 85%

   Buildings by use, streets and the transformer candidates (OSM and one LoD2 station) of the demo
   region.

4 -- Street graph and connection points
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The street segments of the postcode are copied to ``ways_tem_<plz>``. With InfDB they already
contain a connection line from every building centroid to its street; pylovo adds connection
lines for the transformers and splits the street segments at their foot points. In the file-based
mode pylovo splits intersecting streets and creates the building connection lines itself.

pgRouting then builds the routing graph (``pgr_extractVertices``; endpoints snapped to a 10⁻⁶
grid) with the segment length in metres as cost. Every building gets two vertices:

``vertice_id``
   the vertex at the building centroid; it becomes the consumer bus.
``connection_point``
   the street-side end of its connection line; it becomes a connection bus of the feeder.

Buildings without a connection point are removed. With ``AGGREGATE_NEARBY_CONNECTION_POINTS``,
nearby connection points on the same street (within ``CONNECTION_POINT_AGGREGATION_RADIUS_M`` and
with at most ``CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS`` buildings) are merged into
``agg_connection_point``, which then replaces ``connection_point`` in all later steps.

.. figure:: /images/generation/step2_connections.png
   :alt: Detail of streets, buildings and the connection lines from building centroids to the street
   :width: 85%

   Connection lines from the building vertices to the street graph (detail).

Phase 2: partition
------------------

5 -- Street components and k-means
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The connected components of the street graph are computed (``pgr_connectedComponents``). A
component with at most one building is removed together with its transformer candidates. A
component with more than ``MAX_BUILDINGS_PER_KCID`` buildings is split with k-means on the building
centroids into ⌈n / ``MAX_BUILDINGS_PER_KCID``⌉ clusters (seed ``K_MEANS_SEED``); every other
component is one cluster. The result is the **k-means cluster id** ``kcid`` of every building. It
bounds the size of the routed distance matrices of the next step.

.. figure:: /images/generation/step3_kmeans.png
   :alt: Buildings of the demo region coloured by four k-means clusters
   :width: 85%

   With ``MAX_BUILDINGS_PER_KCID: 150`` (version ``docs_km``) the single street component of the
   demo region is split into four k-means clusters. With the shipped value of 1000 the whole
   postcode is one cluster.

6 -- Building clusters
~~~~~~~~~~~~~~~~~~~~~~

Each k-means cluster is divided into **building clusters** (``bcid``), one per transformer and
thus one per grid. Loads are compared as coincident peak loads (:doc:`grid_dimensioning`) divided by
``TRANSFORMER_PLANNING_UTILIZATION``.

**Brownfield** (k-means clusters with transformer candidates). The routed distances between all
connection points and all transformers are computed (``pgr_dijkstraCost``). Pairs are processed
from the shortest distance up to ``MAX_BROWNFIELD_TRAFO_DISTANCE``: a connection point is assigned
to the transformer unless that transformer would exceed its capacity, which is its known rating or,
without a known rating, the largest rating allowed for the settlement type. Transformers without
buildings are dropped. Each remaining transformer forms a cluster with a negative ``bcid`` (-1,
-2, ...) and keeps its known rating or gets the smallest allowed rating above the planned load.
Connection points that no transformer could take are clustered greenfield; a single remaining
consumer is dropped.

**Greenfield** (all remaining connection points). A routed distance matrix between the connection
points (``pgr_dijkstraCostMatrix``) feeds an average-linkage hierarchical clustering. pylovo splits
the tree into two clusters and checks each:

* too large -- the planned load needs more than the largest allowed rating and the cluster has at
  least five connection points, or
* too far -- no connection point of the cluster reaches all others within the greenfield distance
  limit (``MAX_GREENFIELD_TRAFO_DISTANCE``; with ``MAX_GREENFIELD_TRAFO_DISTANCE_STD`` > 0 a
  per-cluster limit drawn around it).

Invalid clusters are split again until every cluster is valid. A valid cluster gets the smallest
allowed rating above its planned load; a small cluster (fewer than five connection points) above
the largest rating is rated in multiples of 630 kVA (parallel units). With
``MERGE_GREENFIELD_CLUSTERS``, neighbouring clusters are merged afterwards if the merged cluster
still fits a rating of ``GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA`` and the distance limit. The
clusters are numbered 1, 2, ... in the order of their smallest vertex id, so equal partitions get
equal ids.

Phase 3: place
--------------

7 -- Transformer positions
~~~~~~~~~~~~~~~~~~~~~~~~~~

Brownfield transformers stay where they are. For a greenfield cluster every connection point is a
candidate position; its cost is the sum of routed distance × load to all other connection points,
and it is feasible if it reaches all of them within the cluster's distance limit. pylovo takes the
feasible candidate with the lowest cost, or -- with ``GREENFIELD_TRAFO_POSITION_TOLERANCE`` > 0 --
draws one at random (seeded per cluster) among the feasible candidates whose cost is at most
(1 + tolerance) times the lowest. The station is placed on the street vertex and stored in
``transformer_positions``; its rating is stored in ``grid_result``.

.. figure:: /images/generation/step4_building_clusters.png
   :alt: Two maps: four greenfield building clusters with optimised transformers, and five brownfield clusters around existing transformers
   :width: 100%

   Building clusters and transformer positions. Left: greenfield grids of the shipped
   configuration; the stations are drawn among near-optimal positions. Right: the same postcode
   with ``USE_OPEN_TRANSFORMER_POSITIONS: True`` -- five brownfield clusters around the OSM and LoD2
   transformers.

Phase 4: design
---------------

8 -- Feeder planning and cable sizing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each grid is built in its own electrical backend by a
:class:`~pylovo.cable_installer.CableInstaller`: MV bus (20 kV) with external grid, transformer,
LV bus, one connection bus per street vertex on the routes and one consumer bus with one load per
category for every supplied building (:doc:`electrical_backends`).

The feeder topology is planned before any cable is chosen
(:func:`~pylovo.feeder_planning.plan_feeder_branches`):

#. pgRouting computes the shortest routes from the transformer to all connection points and
   consumer vertices of the cluster.
#. Starting with the connection point farthest from the transformer, the route towards the
   transformer is followed and connection points are added to a **branch** as long as the
   coincident current of the branch stays below ``FEEDER_SPLIT_MAX_CURRENT_KA``.
#. The branch is attached to the deepest node on its route that already belongs to another branch
   if that node is at least ``MIN_SHARED_PREFIX_LENGTH_M`` away from the transformer; otherwise it
   starts a new feeder at the transformer.
#. This repeats until all connection points belong to a branch. The result is a radial tree along
   the streets.

Then the feeder edges between hard nodes (transformer and branching points) are grouped into
uniform sections, sized for ampacity and upsized where the end-to-end voltage-drop limit requires
it (:func:`~pylovo.feeder_planning.size_feeder_tree`). Service cables run in a straight line from the connection bus to the consumer bus and are
sized for the building's own load. The complete method is described in :doc:`grid_dimensioning`.

pylovo also writes GIS helper geometries: ``split_points`` at branching nodes and offset copies of
lines where several feeders share a street (``lines_result_helper``, ``lines_result_view``).

.. figure:: /images/generation/step6_grids_by_cable.png
   :alt: Generated grids with feeders coloured by cable cross-section and thin service connections
   :width: 85%

   Cable types of the generated grids: feeders (thick) from NAYY 4×150 up to 4×300, service
   connections (thin) mostly NAYY 4×50.

9 -- Power-flow check
~~~~~~~~~~~~~~~~~~~~~

The grid is solved for its validation snapshot (:ref:`validation-snapshot`) with Newton-Raphson,
with the LV busbar at ``LV_REFERENCE_VOLTAGE_PU`` and, where needed, the transformer tap moved by
up to ``MAX_TAP_STEPS`` steps.
A converged result inside ``POWER_FLOW_VOLTAGE_LIMITS`` is stored as ``converged``, a converged
result outside the band as ``voltage_violation``, otherwise ``not_converged``. The solved feeder,
service and total voltage drops are stored in ``grid_result``. Grids are kept in every case.

Phase 5: persist
----------------

10 -- Result tables
~~~~~~~~~~~~~~~~~~~

Each grid is stored in ``grid_result`` (with the pandapower JSON) and in the ``pandapower_*``
tables. When all grids of the postcode are done, the supplied buildings go to
``buildings_result`` and the street graph to ``ways_result``, and the transaction is committed.
With ``ANALYZE_GRIDS`` the postcode key figures follow (:doc:`../user_guide/analysing_grids`).
The session-local working tables are dropped. The regular view ``buildings_result_with_grid``
reads the committed buildings immediately.

.. figure:: /images/generation/step5_grids_by_grid.png
   :alt: The four generated grids of the demo region, one colour per grid, with transformer ratings
   :width: 85%

   The generated grids of version ``1``. Buildings without a grid have no address
   (``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS``) or are large commercial loads supplied from the MV grid.
