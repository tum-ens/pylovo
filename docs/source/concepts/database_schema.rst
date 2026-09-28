Database schema
===============

All pylovo tables are in the schema ``pylovo``. They are defined in
:mod:`pylovo.database.config_table_structure` and created by ``pylovo-setup``. The
:doc:`database_tables` lists every column.

.. figure:: /images/diagrams/database_schema.*
   :alt: Entity diagram of the pylovo schema with reference tables, per-postcode and per-grid results
   :width: 100%

   Tables of the schema ``pylovo`` with primary and foreign keys, generated from the table
   definitions with ``docs/scripts/plot_database_diagram.py``. Tables of the classification module
   are not shown.

Structure
---------

The schema is organised around three keys:

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

All foreign keys of result tables use ``ON DELETE CASCADE``. Deleting a ``version`` row therefore
removes all of its postcodes, grids and key figures; deleting a ``postcode_result`` row removes the
grids of that postcode (this is what ``pylovo-delete`` does). The only exception is the link from
``grid_result`` to ``equipment_data`` (``ON DELETE SET NULL``).

Reference and input tables are not versioned: ``postcode``, ``transformers``,
``consumer_categories`` (synchronised with the configuration at every run), ``municipal_register``
and, in the file-based mode, ``res``, ``oth``, ``ways`` and ``ags_log``.

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

Views and temporary tables
--------------------------

``transformer_positions_with_grid``
   View of ``transformer_positions`` with ``kcid``, ``bcid``, ``plz`` and the equipment data.
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
