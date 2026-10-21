Transformer data
================

Existing transformer positions let pylovo build *brownfield* grids around real substations.
All candidates are stored in the unversioned table ``pylovo.transformers``; whether a
generation run uses them is decided by two configuration keys.

Sources of transformer candidates
---------------------------------

.. list-table::
   :header-rows: 1
   :widths: 20 30 50
   :class: fixed-table

   * - Source
     - ``osm_id`` pattern
     - How it gets into ``pylovo.transformers``
   * - OpenStreetMap
     - ``node/<id>``, ``way/<id>`` (a multipolygon relation: ``multipolygon/<id>``)
     - ``pylovo-setup`` (Bavaria) or ``pylovo-import transformers-osm --relation-id <id>``.
   * - LoD2 stations
     - ``lod2/<objectid>``
     - Added automatically during generation (InfDB mode) from LoD2 buildings with function
       code ``31001_2523``. If an OSM transformer lies within 3 m or inside the building, that
       OSM row is flagged ``lod2 = true`` instead of creating a new row.
   * - DSO list
     - ``dso/<source>/<external_id>``
     - ``pylovo-import transformers-dso-csv <file.csv>``.
   * - Manual edits
     - ``manual/<unix-time>``
     - Transformer editor of the GridPlanner UI (:doc:`browser_ui`).

Which candidates are used
-------------------------

.. list-table::
   :header-rows: 1
   :widths: 40 60
   :class: fixed-table

   * - Setting in ``config_generation_lv.yaml``
     - Candidates used as grid roots
   * - ``USE_OPEN_TRANSFORMER_POSITIONS: True``
     - OSM, LoD2 and manual candidates (everything that is not a DSO row)
   * - ``USE_DSO_TRANSFORMER_POSITIONS: True``
     - DSO rows (``type = 'dso'`` or ids starting with ``dso/``)
   * - ``USE_MANUAL_TRANSFORMER_POSITIONS: True``
     - Only the positions placed in the transformer editor (ids ``manual/…``), without the OSM
       and LoD2 candidates
   * - both ``False`` (shipped configuration)
     - none -- all grids are greenfield grids with optimised transformer positions

Candidates inside the postcode polygon are used. A candidate inside a building whose load is
supplied directly from the MV grid (see ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``) is dropped as
a customer station. Buildings are assigned to the nearest used
transformer along the streets up to ``MAX_BROWNFIELD_TRAFO_DISTANCE``; transformers without
buildings are dropped (:doc:`../concepts/pipeline`).

The column ``transformer_rated_power`` (kVA) matters for brownfield grids: a transformer with a
known rating is filled with buildings only up to that rating and keeps it. A transformer without
rating is filled up to the largest rating allowed for the settlement type and then gets the
smallest catalogue rating that covers the planned load.

OpenStreetMap download
----------------------

The processed transformers of Bavaria (relation 2145268) ship with pylovo, in the repository and in the
wheel, as ``transformer_data/processed_trafos/2145268_trafos_processed_25832.geojson`` (EPSG:25832).
``pylovo-setup`` loads this file. With another ``TARGET_EPSG`` in ``.env`` it first reprojects the shipped
file to ``2145268_trafos_processed_<TARGET_EPSG>.geojson`` in the data directory (a few seconds, no
download) and loads that file; delete it to reproject again. Only for another relation without a processed
file does the setup fetch the data from the Overpass API, see below.

.. note::

   The shipped file was created before the substation query fetched ``power=pole`` nodes, so it
   holds no pole-mounted stations. ``pylovo-import transformers-osm --relation-id 2145268`` fetches
   Bavaria again with the current query and adds the stations that are not in ``pylovo.transformers`` yet.

To add another area, import it by the id of its OSM boundary relation:

.. code-block:: bash

   uv run pylovo-import transformers-osm --relation-id 62611      # Baden-Württemberg

To find the relation id, **search for the area by name** (for example ``Munich``) on
`openstreetmap.org <https://www.openstreetmap.org/>`_. Open the matching boundary relation in
the search results and copy its number from the URL or sidebar:

.. figure:: /images/screenshots/osm_relation_id.png
   :alt: OpenStreetMap page of the relation Munich with its id 62428
   :width: 70%

   The relation id of Munich is 62428 (screenshot of openstreetmap.org, © OpenStreetMap
   contributors).

The import runs two Overpass queries from ``transformer_data/overpass_queries/`` of the data
directory (``data/``, or ``PYLOVO_DATA_DIR``, see :ref:`env-variables`; ``$relation_id$`` is replaced
by the area id) and processes the result:

``substations_query.txt``
   All ``power=transformer`` and ``power=substation`` objects and the ``power=pole`` nodes with a
   ``transformer`` tag (pole-mounted stations on overhead lines) in the area, without Deutsche Bahn
   operators, historic and abandoned objects.

``shopping_mall_query.txt``
   Areas whose transformers are assumed not to supply an LV grid: shopping malls, oil industry
   areas, power plants (including solar parks), military training areas, a large parking area
   (``Festplatz``), education and railway land use.

Processing (:func:`pylovo.data_import.import_transformers.process_trafos`) removes, in this order:

#. points inside transformer polygons,
#. polygons with an area of at least ``AREA_THRESHOLD`` (60 m²), such as HV substations,
#. objects tagged with a voltage of at least ``VOLTAGE_THRESHOLD`` (110 kV),
#. transformers closer than ``MIN_DISTANCE_BETWEEN_TRAFOS`` (8 m) to another one,
#. transformers inside the areas of the second query.

If no transformer is left after these steps, the import stops with a message.

The remaining centroids are written to
``transformer_data/processed_trafos/<relation-id>_trafos_processed_<TARGET_EPSG>.geojson``
of the data directory and appended to ``pylovo.transformers``; the raw downloads are kept in
``transformer_data/fetched_trafos/`` (the repository ships none). The thresholds and the setup relation
(``RELATION_ID = 2145268``, Bavaria) are constants at the top of
``src/pylovo/data_import/import_transformers.py``; distances and areas are computed in
``TARGET_EPSG`` from ``.env``. To load a different default area with
``pylovo-setup``, change ``RELATION_ID``: the first setup then fetches and processes that relation.

.. note::

   Processing all transformers of a federal state takes a long time (30 to 50 minutes). The
   Overpass API is a shared service; avoid repeated large downloads. When the server answers with
   HTTP 429 or 504, pylovo asks again (3 attempts, after 30 s and 60 s). An answer that carries a
   ``remark`` means that the server ran into its time or memory limit and the result is incomplete:
   the import stops with that remark and writes no files, so no truncated result is kept.

DSO transformer positions from CSV
----------------------------------

Transformer lists of a distribution system operator are imported from a CSV file with WGS84
coordinates:

.. list-table::
   :header-rows: 1
   :widths: 28 14 58
   :class: fixed-table

   * - Column
     - Required
     - Description
   * - ``external_id``
     - yes
     - Stable id of the transformer in the source dataset. Read as text (leading zeros are kept),
       not empty and unique per source after the source label is normalised.
   * - ``lon``
     - yes
     - Longitude (EPSG:4326).
   * - ``lat``
     - yes
     - Latitude (EPSG:4326).
   * - ``transformer_rated_power``
     - no
     - Rating in kVA: empty (unknown) or a whole number above 0.
   * - ``source``
     - no
     - Short source label; default ``csv``, also for an empty cell.

.. code-block:: text

   external_id,lon,lat,transformer_rated_power
   ST-0001,11.7713,47.9712,400
   ST-0002,11.7651,47.9688,

.. code-block:: bash

   uv run pylovo-import transformers-dso-csv stations.csv --source aying --replace-source

Rows are stored with ``type = 'dso'`` and ids ``dso/<source>/<external_id>`` (the source label is
lower-cased; characters other than ``a-z``, ``0-9``, ``_``, ``.`` and ``-`` become ``_``). ``--source`` overrides the
CSV column; ``--replace-source`` deletes the existing rows whose id starts exactly with
``dso/<source>/`` before the import, which is useful for corrected re-imports. All cells are read as
text, so ids keep their leading zeros. The import stops before it changes anything and names the CSV
rows (the header is row 1) of every problem: an empty or repeated id, invalid coordinates, or a
rating that is neither empty nor a whole number of kVA above 0. A row whose id already exists in
the table from an earlier import is updated. Then enable the positions:

.. code-block:: yaml

   USE_DSO_TRANSFORMER_POSITIONS: True
   USE_OPEN_TRANSFORMER_POSITIONS: False   # or True to use OSM, LoD2 and manual positions as well

and generate the grids in a new ``VERSION_ID``.

Manual editing
--------------

Transformer candidates can be viewed and edited on a map:

The GridPlanner browser UI (:doc:`browser_ui`, step 3 *Data*) contains a transformer editor on
top of the :doc:`http_api` (``/api/transformers``). It works on ``pylovo.transformers``: load a
PLZ, add transformers by clicking on the map (ids ``manual/<unix-time>``), delete transformers,
set the rating of one transformer or of all transformers in the postcode (uniformly or by a
percentage distribution of ratings), or clear the ratings. Manual positions are used with
``USE_OPEN_TRANSFORMER_POSITIONS`` (together with all OSM and LoD2 candidates) or alone with
``USE_MANUAL_TRANSFORMER_POSITIONS``. A rating set on a candidate sizes the station of the grid
built around it; ratings of candidates whose source is switched off are ignored.

Removing candidates
-------------------

``pylovo-delete transformers`` deletes all rows of ``pylovo.transformers``, including the manual
and DSO positions, which cannot be recreated from the OSM files. Generated grids keep their
transformer positions (``pylovo.transformer_positions``) but lose the link to the raw transformer:
``osm_id`` becomes ``NULL``. To remove one source only, delete its rows with SQL, for example
``DELETE FROM pylovo.transformers WHERE osm_id LIKE 'dso/aying/%';``. The grids that used them stay
as they are.
