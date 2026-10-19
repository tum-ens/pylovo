Map viewer
==========

``pylovo-viewer`` shows the database of your ``.env`` as an interactive map in the browser: the
German high-voltage grid, the MV grids of an MV run, the LV grids of a version and the generating
units. It is the quick alternative to the QGIS templates (:doc:`exporting_visualising`). It only
reads: every query runs in a read-only transaction, and the viewer never creates, migrates or resets
anything. For setup, generation and editing use GridPlanner (:doc:`browser_ui`).

.. figure:: /images/viewer/viewer-detail.png
   :alt: The map viewer with LV grids coloured by grid and one selected feeder section
   :width: 100%

   LV grids of one postcode in the default view: areas, cables and buildings coloured by grid and
   the MV/LV substations as transformer symbols. A click on a cable selects its feeder section
   (yellow), and the inspector shows the section and the cable's database row. MV test region from open data; map data © OpenStreetMap contributors
   (ODbL).

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

**One switch per level.** The *Layers* tab has one switch each for the HV grid, the MV grids, the LV
grids and the generation. The arrow next to a level opens its single layers, their colour modes and
an *i* with each layer's source. LV cables and buildings appear when you zoom in (from zoom 13).

**Regions.** Click a postcode or an MV supply area on the map, pick one in the *Regions* tab, or
search: postcode, place name, municipality key (AGS), grid id, supply area or substation name and
building ``objectid`` all work. The map flies to the region and marks it, and the *Key figures* tab
shows its numbers.

**Identify.** Hover over a feature for a short tooltip. Click it for the inspector (a clicked line
turns bright yellow): its key facts,
actions (*Zoom to*, *Show this LV grid*, *Show supply area*) and *All attributes*, the full database
row as in QGIS' identify tool. When several features overlap, the inspector lists them all. A click
on an LV feeder cable selects its whole feeder section, from the station or a cable cabinet to the
next cabinet or the end of the feeder, with its length, cable types and house connections. Clicking
section by section follows the feeder through the grid. A click on an MV line selects its whole ring
with the stubs, marks the normally-open point on it and adds the ring's length, overhead length,
stations and highest (n-1) loading to the facts.

**Colours and legend.** Layers with several colour modes have a small menu next to their name
(for example cables by grid, feeder, type or feeder/service). By default the LV grid areas, cables
and buildings are coloured by grid. The legend shows the categories of the loaded data with their
counts. Colours that only tell grids, rings or supply areas apart have no legend entry.

**3D.** The map starts in 3D, tilted by 25°, on the OpenStreetMap basemap (another basemap is kept
in the address bar, not between visits). The *3D* button switches
to the flat map and back; buildings are extruded by their height in 3D. With a
``citydb`` schema (3DCityDB v5, see :doc:`input_data`) the buildings in view are drawn as their LoD2
models with roofs and walls from zoom 15 (up to 5,000 buildings).

**Sharing a view.** The address bar keeps version, MV run, region, levels, layers, colour modes,
basemap and camera, so a view can be bookmarked or sent. Keys: ``/`` search, ``3`` 3D, ``L`` layer panel,
``Esc`` close the inspector and then clear the region, ``?`` help.

.. figure:: /images/viewer/viewer-lod2.png
   :alt: LoD2 buildings of several grids in 3D, coloured by grid
   :width: 100%

   3D view with LoD2 buildings coloured by grid. Demo region 85653 (Aying) derived from
   OpenStreetMap with synthetic LoD2 models.

Layers
------

Every substation belongs to both of its levels: the HV/MV substations to the HV and the MV grids,
the MV/LV substations to the MV and the LV grids. It is one layer listed in both, so it looks the
same whichever level shows it. Substations are drawn as transformer symbols, the HV/MV ones larger
and framed. A layer whose tables are missing or empty in the database is greyed out with the
reason, so one viewer works with any pylovo database.

.. list-table::
   :header-rows: 1
   :widths: 16 24 60

   * - Level
     - Layer
     - Source and content
   * - Regions
     - Postcode areas
     - ``pylovo.postcode_result``: postcodes of the version with the number of grids; settlement
       type as colour mode.
   * - HV grid
     - HV and EHV lines
     - The 110, 220 and 380 kV grid of Germany from eGon-data (see below).
   * - HV and MV grids
     - HV/MV substations
     - ``pylovo.mv_substation`` of the MV run with their site outlines; candidates the run did not use
       are hollow. The facts give the eTraGo 110 kV bus of each substation.
   * - MV grids
     - MV lines
     - ``pylovo.mv_line``: rings, stubs and dedicated feeders by ring, kind, role or loading, with the
       normally-open points; overhead lines dashed, ties between supply areas dotted. Mixed lines run
       partly on an OSM MV line: that part is dashed, and their facts give the cable and overhead
       length.
   * -
     - OSM MV lines (off by default)
     - The MV overhead lines mapped in OpenStreetMap as corridors of the run's routing graph
       (``pylovo.mv_graph_edge``, ``MV_OVERHEAD_LINES: osm``).
   * - MV and LV grids
     - MV/LV substations
     - ``pylovo.grid_result`` and ``transformer_positions``, with the MV ring and voltage of the
       station from ``pylovo.mv_node`` when an MV run is selected. Shown from zoom 10.2.
   * -
     - Supply areas, MV customers
     - ``pylovo.mv_grid`` (areas cut by the region border have a dashed outline) and
       ``pylovo.mv_customers``.
   * - LV grids
     - Grid areas
     - Convex hull of the buildings of every grid of the version.
   * -
     - Cables, Buildings
     - ``pylovo.lines_result_cache`` with the cable cabinets of ``pylovo.split_points``, and
       ``pylovo.buildings_result``, for the map view from zoom 13. Feeders are numbered from the
       station along the cables, 1 being the longest. From zoom 15 an arrow at each cabinet marks
       the cable that supplies it.
   * - Generation
     - MaStR units
     - ``pylovo.mastr_units`` (``pylovo-import mastr``) by technology or grid level.

Grids, supply areas and rings are coloured so that neighbours never share a colour. Voltage levels
use the usual grid-map colours, 380 kV red, 220 kV green and 110 kV violet, each with its own line
width.

The German high-voltage grid (eTraGo)
-------------------------------------

The layer *HV and EHV lines* shows the grid of eGon-data's eTraGo model: the AC lines at 380, 220
and 110 kV in Germany, in PyPSA format, built by osmTGmod from
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

* Data travel as GeoJSON per version, per MV run or for the map view, gzipped. A layer returns at
  most 60,000 features per request and says so when it is cut.
* The server keeps loaded data in memory. After a new generation or MV run, the reload button in
  the top bar reads everything from the database again.
* The browser loads MapLibre GL JS, Vue, the fonts and the basemaps (OpenStreetMap by default,
  OpenFreeMap) from the internet. The data layers also work without a basemap (*Basemap: No
  basemap*).
* LoD2 models need WebGL2; without it the buildings stay extruded footprints.
