Database schema
===============

All pylovo tables are in the schema ``pylovo``. They are defined in
:mod:`pylovo.database.config_table_structure` and created by ``pylovo-setup``. The
:doc:`database_tables` lists every column. The schema needs PostgreSQL 15 or newer.

.. figure:: /images/diagrams/database_schema.*
   :alt: Entity diagram of the pylovo schema with reference tables, per-postcode and per-grid results
   :width: 100%

   Tables of the schema ``pylovo`` with primary and foreign keys, generated from the table
   definitions with ``docs/scripts/plot_database_diagram.py``. Tables of the classification module
   are not shown.

Structure
---------

The schema is organised around four keys:

``version_id``
   Every result belongs to a version. ``version`` holds the parameter snapshot; ``equipment_data``
   the transformer and cable catalogue of the version.
``(version_id, plz)``
   ``postcode_result`` is the per-postcode result: polygon, house distance, households per building
   and settlement type. ``ways_result`` and ``plz_parameters`` hang on it.
``grid_result_id``
   ``grid_result`` has one row per grid (``version_id``, ``plz``, ``kcid``, ``bcid``). All per-grid
   tables reference it: ``buildings_result``, ``transformer_positions``, ``lines_result`` and its
   GIS helpers, ``split_points``, the ``pandapower_*`` tables, ``clustering_parameters`` and the
   audit table ``load_edit``.
``(version_id, mv_variant_id)``
   ``mv_variant`` holds one MV variant of a version with its MV parameter snapshot. All MV result
   tables reference it (:ref:`mv-tables`).

Deleting a ``version`` row removes its postcodes, grids and key figures through
cascading foreign keys. Deleting a ``postcode_result`` row removes the grids of
that postcode (this is what ``pylovo-delete`` does). Deleting equipment clears only
``grid_result.transformer_equipment_name``, and deleting a raw transformer clears
only ``transformer_positions.osm_id``. Pandapower bus references are constrained
within their grid; dependent elements are deleted before buses.

Reference and input tables are not versioned: ``postcode``, ``transformers``,
``consumer_categories`` (synchronised with the configuration at every run), ``municipal_register``,
the MV routing graphs
``mv_graph*`` and, in the file-based mode, ``res``, ``oth``, ``ways`` and ``ags_log``.

Result tables in short
----------------------

.. list-table::
   :header-rows: 1
   :widths: 28 72
   :class: fixed-table

   * - Table
     - Content
   * - ``grid_result``
     - Transformer rating and description, routing vertex of the transformer
       (``ont_vertice_id``), ``power_flow_status``, planning and solved voltage-drop diagnostics
       (:ref:`stored-diagnostics`), the pandapower net as JSON (``grid``).
   * - ``buildings_result``
     - Supplied buildings: InfDB attributes, households, floor-area split, load components
       (``residential_peak_load_in_kw``, ``nonresidential_peak_load_in_kw``,
       ``nonresidential_mv_direct``, ``peak_load_in_kw``) and the vertices ``vertice_id``,
       ``connection_point``, ``agg_connection_point``.
   * - ``transformer_positions``
     - Transformer point, ``osm_id`` of brownfield positions and the flags ``osm`` and ``lod2``;
       ``comment`` is ``Normal`` (brownfield) or ``on_way`` (greenfield).
   * - ``lines_result``
     - Line geometries with type, buses, parallel count, length and ``feeder_section_id``.
   * - ``lines_result_helper``, ``lines_result_cache``
     - Offset geometries for parallel and split feeders, and the union of real and helper lines
       with grid identifiers for GIS layers. The old lines_result_view name remains a compatibility view.
   * - ``split_points``
     - Branching nodes of the feeders.
   * - ``pandapower_bus``, ``pandapower_line``, ``pandapower_trafo``, ``pandapower_load``
     - The pandapower element tables, including sizing provenance on lines and the snapshot,
       design and installed loads on loads.
   * - ``ways_result``
     - Street graph of the postcode, including connection lines.
   * - ``plz_parameters``, ``clustering_parameters``
     - Key figures of ``pylovo-analyze`` (:doc:`../user_guide/analysing_grids`).
   * - ``load_edit``
     - Audit rows of load edits made through the HTTP API (:ref:`http-api-load-editing`), with the
       state before each edit for the exact undo. Deleting the grid removes them.
   * - ``mv_customers``
     - Buildings whose non-residential load the LV stage leaves to the MV grid
       (``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``), per version and postcode; input of the MV stage.

.. _mv-tables:

MV stage tables in short
------------------------

The MV stage (:doc:`mv_grids`) writes its results per MV variant. Every result table carries
``version_id`` and ``mv_variant_id`` and references ``mv_variant`` with ``ON DELETE CASCADE``, so
deleting a variant (``pylovo-mv delete``) or its version removes all its rows. Deleting a postcode
with ``pylovo-delete networks`` deletes the MV variants that contain it. The migrations
``0009_mv_grids`` and ``0010_mv_display_geometry`` create the tables and the display geometry columns
on schemas set up before.

.. list-table::
   :header-rows: 1
   :widths: 28 72
   :class: fixed-table

   * - Table
     - Content
   * - ``mv_variant``
     - One row per MV variant: parameter snapshot, postcodes, routing graph, status, LV fingerprint
       (staleness) and run summary.
   * - ``mv_substation``
     - HV/MV substations of the variant with busbar point, footprint, eGon match, transformer sizing
       and flags.
   * - ``mv_grid``
     - One MV grid per supply area: status, nominal voltage, polygon, counts, lengths, voltage and
       loading extremes, metrics, repairs and the stored pandapower net (``net``).
   * - ``mv_node``
     - Busbars, stations (``grid_result_id``), MV customers and joints with loads, connection, MV
       voltages, voltage budget, MV/LV tap and coupling results.
   * - ``mv_line``
     - Ring, stub, dedicated, tie and link lines with kind, type, lengths and loadings.
   * - ``mv_ring``
     - One row per ring or feeder with members, lengths, loads, open point and extremes.
   * - ``mv_graph``, ``mv_graph_vertex``, ``mv_graph_edge``, ``mv_graph_settlement``
     - Routing graphs with settlement flags. They are not versioned and are shared by all variants
       with the same inputs (content-addressed ``graph_id``).

The view ``mv_variant_stats`` sums the grids of every variant for comparisons.

Views and temporary tables
--------------------------

``transformer_positions_with_grid``
   View of ``transformer_positions`` with ``kcid``, ``bcid``, ``plz``, the station rating
   ``transformer_rated_power`` and the equipment data of one unit (``s_max_kva``, ``cost_eur``, ...);
   ``transformer_units`` is the number of units of the station.
``buildings_result_with_grid``
   Regular view of current ``buildings_result`` rows with ``kcid``, ``bcid`` and ``plz``.
``buildings_tem_<plz>``, ``ways_tem_<plz>``, ``ways_tem_<plz>_vertices_pgr``
   Session-local working tables of one run (:doc:`../user_guide/database_setup`).

Coordinates
-----------

Geometries are stored in ``TARGET_EPSG`` (default EPSG:25832). The ``geo`` columns of
``pandapower_bus`` and ``pandapower_line`` hold GeoJSON in WGS84, as in the pandapower nets.

Querying without JSON
---------------------

The ``pandapower_*`` tables make it possible to analyse grids in SQL. Line and load totals per grid:

.. code-block:: sql

   SELECT gr.grid_result_id, gr.version_id, gr.plz, gr.kcid, gr.bcid,
          l.line_count, l.total_line_length_km, d.load_count, d.total_active_load_mw
   FROM pylovo.grid_result gr
   LEFT JOIN (SELECT grid_result_id, COUNT(*) AS line_count,
                     SUM(length_km * parallel) AS total_line_length_km
              FROM pylovo.pandapower_line GROUP BY grid_result_id) l USING (grid_result_id)
   LEFT JOIN (SELECT grid_result_id, COUNT(*) AS load_count, SUM(p_mw) AS total_active_load_mw
              FROM pylovo.pandapower_load GROUP BY grid_result_id) d USING (grid_result_id)
   ORDER BY gr.version_id, gr.plz, gr.kcid, gr.bcid;

Line geometries in the target CRS from the GeoJSON column:

.. code-block:: sql

   SELECT pl.std_type, pl.parallel,
          ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), 25832) AS geom
   FROM pylovo.pandapower_line pl
   JOIN pylovo.grid_result gr USING (grid_result_id)
   WHERE gr.version_id = '1' AND gr.plz = 85653;
