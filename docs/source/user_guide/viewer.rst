Map viewer
==========

``pylovo-viewer`` shows the database of your ``.env`` as an interactive map in the browser: the LV
grids of a version, the MV grids of an MV run, their input data and the German high-voltage grid as
context. It is the quick alternative to the QGIS templates (:doc:`exporting_visualising`). It only
reads: every query runs in a read-only transaction, and the viewer never creates, migrates or resets
anything. For setup, generation and editing use GridPlanner (:doc:`browser_ui`).

.. figure:: /images/viewer/viewer-detail.png
   :alt: The map viewer with LV cables and buildings coloured by feeder and the attributes of one cable
   :width: 100%

   LV grids of one postcode, cables and buildings coloured by feeder, with the inspector showing
   one cable and its database row. MV test region from open data; map data © OpenStreetMap
   contributors (ODbL), basemap OpenFreeMap / OpenMapTiles.

Start
-----

.. code-block:: bash

   uv sync --extra viewer
   uv run --extra viewer pylovo-viewer          # http://127.0.0.1:8770/, opens the browser

The viewer uses the database of the ``.env`` that pylovo uses (the one of the checkout), prints it
at start and shows it in the top bar with a *read-only* badge. On a remote machine, VS Code forwards
the port; otherwise use ``ssh -L 8770:127.0.0.1:8770 <host>``.

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Option
     - Default
     - Meaning
   * - ``--env FILE``
     - the ``.env`` pylovo uses
     - Database settings: ``DBNAME``, ``DBUSER``, ``PASSWORD``, ``HOST``, ``PORT``, ``TARGET_EPSG``
       and the ``INFDB_*_SCHEMA`` names, as in :doc:`configuration`.
   * - ``--host``
     - ``127.0.0.1``
     - Interface to listen on. The viewer has no login, so only widen this on a trusted network.
   * - ``--port``
     - 8770, or the next free port
     -
   * - ``--no-browser``
     -
     - Do not open the browser.
   * - ``--data-dir``
     - ``~/.cache/pylovo-viewer``
     - Downloaded context data (eTraGo, see below).

Working with the map
--------------------

**Version and MV run.** The top bar selects the LV version and, if the version has MV results, the
MV run (:doc:`mv_grids`). The newest ones are preselected.

**Regions.** Click a postcode or an MV supply area on the map, pick one in the *Regions* tab, or
search: postcode, place name, municipality key (AGS), grid id, supply area or substation name and
building ``objectid`` all work. The map flies to the region and loads its cables, buildings and
cable cabinets; the *Key figures* tab follows the selection. Without a region, these layers load for
the map view from zoom 14 on. Selecting an MV supply area loads the LV grids of all its stations.

**Identify.** Hover over a feature for a short tooltip. Click it for the inspector: its key facts,
actions (*Zoom to*, *Show this LV grid*, *Show supply area*) and *All attributes*, the full database
row as in QGIS' identify tool. When several features overlap, the inspector lists them all.

**Colours and legend.** Layers with several colour modes have a small menu next to their name
(for example cables by type, feeder, grid or feeder/service). The legend shows the categories of
the loaded data with their counts. The *i* next to a layer explains it and names its source table.

**3D.** The *3D* button tilts the map and extrudes the buildings by their height. With a
``citydb`` schema (3DCityDB v5, see :doc:`input_data`) the buildings of the selected region are
drawn as their LoD2 models with roofs and walls (up to 5,000 buildings).

**Sharing a view.** The address bar keeps version, MV run, region, layers, colour modes, basemap and
camera, so a view can be bookmarked or sent. Keys: ``/`` search, ``3`` 3D, ``L`` layer panel,
``Esc`` close the inspector and then clear the region, ``?`` help.

.. figure:: /images/viewer/viewer-lod2.png
   :alt: LoD2 buildings of several grids in 3D, coloured by grid
   :width: 100%

   3D view with LoD2 buildings coloured by grid. Demo region 85653 (Aying) derived from
   OpenStreetMap with synthetic LoD2 models.

Layers
------

A layer whose tables are missing or empty in the database is greyed out with the reason, so one
viewer works with any pylovo database.

.. list-table::
   :header-rows: 1
   :widths: 18 26 56

   * - Group
     - Layer
     - Source and content
   * - Regions
     - Postcode areas
     - ``pylovo.postcode_result``: postcodes of the version with the number of grids; settlement
       type as colour mode.
   * - LV grids
     - Grid areas
     - Convex hull of the buildings of every grid of the version.
   * -
     - Substations (MV/LV)
     - ``pylovo.grid_result`` and ``transformer_positions``: size in kVA; grids with voltage
       violations or without a converged power flow stand out.
   * -
     - Cables
     - ``pylovo.lines_result_cache``: feeder and service cables, drawn side by side where they
       share a trench. Feeders are numbered from the station along the cables, 1 being the
       longest.
   * -
     - Cable cabinets, Buildings
     - ``pylovo.split_points`` and ``pylovo.buildings_result``.
   * - LV input data
     - InfDB buildings, Transformer candidates, Streets, House connection lines
     - The inputs of the generation: ``buildings``, ``ways_per_connection`` and ``connection_lines``
       of the InfDB source schema, and ``pylovo.transformers``.
   * - Grid classification
     - Grid clusters
     - ``pylovo.transformer_classified`` (``pylovo-classify``); representative grids are ringed.
   * - MV grids
     - Supply areas, HV/MV substations, Substation sites
     - ``pylovo.mv_grid`` and ``mv_substation`` of the MV run; areas cut by the region border have a
       dashed outline.
   * -
     - MV lines, Normally-open points
     - ``pylovo.mv_line``: rings, stubs and dedicated feeders by ring, kind, role or loading;
       overhead lines dashed, ties between supply areas dotted.
   * -
     - MV nodes, MV customer buildings
     - ``pylovo.mv_node`` (busbars, stations, MV customers, generators, joints) and
       ``pylovo.mv_customers``.
   * -
     - Settlements, Routing graph
     - ``pylovo.mv_graph_settlement`` and ``mv_graph_edge`` of the run, for checking the routing.
   * - Grid context
     - eTraGo HV/EHV lines and buses
     - The 110, 220 and 380 kV grid of Germany from eGon-data (see below).
   * -
     - eGon MV grid districts, eGon HV/MV substations
     - ``egon_mv_grid_district`` and ``egon_hvmv_substation`` of the InfDB open-data schema.
   * -
     - OSM power lines, OSM substations
     - ``infrastructure_line`` and ``infrastructure_polygon`` of the InfDB OSM schema, by voltage.
   * -
     - MaStR units
     - ``pylovo.mastr_units`` (``pylovo-import mastr``) by technology or grid level.

Grids, supply areas and rings are coloured so that neighbours never share a colour. Voltage levels
use the usual grid-map colours, 380 kV red, 220 kV green and 110 kV violet, each with its own line
width.

The German high-voltage grid (eTraGo)
-------------------------------------

The context layers *eTraGo HV/EHV lines* and *buses* show the grid of eGon-data's eTraGo model:
the AC buses and lines at 380, 220 and 110 kV in Germany, in PyPSA format, built by osmTGmod from
OpenStreetMap (2021) and published on the Open Energy Platform. Download it once (about 40 s,
9 MB of GeoJSON in ``--data-dir``):

.. code-block:: bash

   uv run --extra viewer pylovo-viewer fetch etrago      # scenario eGon2035

The 110 kV grid is the reason for this source: the prebuilt OpenStreetMap network of PyPSA-Eur
covers 220 kV and above only. The data derive from OpenStreetMap (ODbL); the map shows the
attribution.

.. figure:: /images/viewer/viewer-overview.png
   :alt: MV supply areas, rings and substations of a test region with the eTraGo high-voltage grid
   :width: 100%

   MV supply areas, rings and HV/MV substations of an MV run in a test region, with the eTraGo
   110, 220 and 380 kV lines. Map data © OpenStreetMap contributors (ODbL), eGon-data eTraGo grid.

Data and limits
---------------

* Data travel as GeoJSON per region (postcode, MV supply area, grid), per MV run or for the map
  view, gzipped. A layer returns at most 60,000 features per request and says so when it is cut.
* The server keeps loaded data in memory. After a new generation or MV run, the reload button in
  the top bar reads everything from the database again.
* The browser loads MapLibre GL JS, Vue, the fonts and the basemaps (OpenFreeMap, OpenStreetMap)
  from the internet. The data layers also work without a basemap (*Basemap: No basemap*).
* LoD2 models need WebGL2; without it the buildings stay extruded footprints.
