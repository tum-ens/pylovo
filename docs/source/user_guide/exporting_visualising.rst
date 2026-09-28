Exporting and visualising
=========================

Generated grids live in the database. You can look at them directly with QGIS, export them as
CSV or pandapower JSON, or plot them with the helpers in :mod:`pylovo.plotting`.

QGIS templates
--------------

The directory ``QGIS/`` contains QGIS project templates that read the pylovo tables directly:

``template_remote_db.qgz`` (recommended)
   Reads the schemas ``pylovo``, ``basedata`` and ``opendata`` through the PostgreSQL service
   ``infdb_postgres``. Change the connection in one place, the service file.
``template_local_db.qgz``
   Older template with a fixed connection (localhost, port 5432, database ``pylovo_db_local``) and
   tables in the schema ``public``; it does not match the current schema.
``template_remote_db_expansion.qgz``
   The remote template plus layers of an external expansion-planning schema; not needed for
   pylovo itself.

Connect the remote template:

#. Create the service file ``~/.pg_service.conf`` (Linux) or set the environment variable
   ``PGSERVICEFILE`` to its location (Windows, e.g. ``%userprofile%\.pg_service.conf``).
#. Add a section named like the service used by the template:

   .. code-block:: ini

      [infdb_postgres]
      host=localhost
      port=54321
      dbname=infdb
      user=infdb_user
      password=your_password
      sslmode=disable

   ``QGIS/.pg_service.conf`` is an example of the format; its section is called ``qwc_geodb``, so
   rename the section or add a second one.
#. Open the template. Choose the version to display with the project variable ``version_id``
   (*Project → Properties → Variables*); the grid layers are filtered by it.

The template contains the input layers (postcodes, transformer candidates, InfDB buildings,
street segments and connection lines) and the grid layers (``transformer_positions_with_grid``,
``lines_result_view``, ``split_points``, ``buildings_result``). Further project variables set
styles: ``building_border_thickness``, ``circle_diameter``, ``lines_thickness``,
``postcode_border_thickness`` and ``ways_thickness``. See ``QGIS/README.rst`` for details.

Tips:

* Transformer labels show ``kcid.bcid``; negative ``bcid`` values are brownfield transformers.
* Filter single grids with an expression such as ``"version_id" = '1' AND "kcid" = 1 AND "bcid" = 3``.
* ``buildings_result_with_grid`` is a regular view over the current building rows; the
  base geometry has a GiST index for spatial filters.

CSV export
----------

``pylovo-export`` writes the buses and lines of the grids of the active ``VERSION_ID`` to CSV
files, for QGIS layers that do not need a database connection or for other tools:

.. code-block:: bash

   uv run pylovo-export --plz 85653 --output exports/          # all grids of one postcode
   uv run pylovo-export --plz 85653 80803 --output exports/    # several postcodes
   uv run pylovo-export --grid --plz 85653 --kcid 1 --bcid 3 --output exports/

.. list-table::
   :header-rows: 1
   :widths: 40 60
   :class: fixed-table

   * - Files
     - Written for
   * - ``lines_single_grid.csv``, ``bus_single_grid.csv``
     - one postcode, or one grid with ``--grid``
   * - ``lines_multiple_grids.csv``, ``bus_multiple_grids.csv``
     - several postcodes

The rows are the pandapower ``line`` and ``bus`` tables plus a ``geometry`` column (WKT, EPSG:4326),
``net`` (running number of the grid in the export), ``plz`` and, for buses, ``consumer_bus``.
Without ``--output`` the files go to ``QGIS/`` in the working directory and overwrite the CSV
files there.

pandapower JSON
---------------

Every grid is stored as pandapower JSON in ``pylovo.grid_result.grid``. Read it with
:meth:`~pylovo.database.analysis_mixin.AnalysisMixin.read_net_db` and save it as a file with
pandapower:

.. code-block:: python

   import pandapower as pp
   from pylovo.database.database_client import DatabaseClient

   with DatabaseClient() as dbc:
       for kcid, bcid in dbc.get_list_from_plz(85653):          # grids of VERSION_ID
           net = dbc.read_net_db(85653, kcid, bcid)
           pp.to_json(net, f"grid_85653_{kcid}_{bcid}.json")

Alternatively, ``SAVE_GRID_FOLDER: True`` writes the JSON files during generation
(:doc:`generating_grids`). The browser UI offers a download button per grid.

Plotting helpers
----------------

:mod:`pylovo.plotting.generation.networks` plots single grids; install the ``plots`` extra first.
The functions read the grid of the active ``VERSION_ID``.

.. list-table::
   :header-rows: 1
   :widths: 44 56
   :class: fixed-table

   * - Function
     - Output
   * - :func:`~pylovo.plotting.generation.networks.plot_contextily`
     - Matplotlib map with lines, buildings coloured by peak load, the transformer and an
       OpenStreetMap basemap (needs internet access).
   * - :func:`~pylovo.plotting.generation.networks.plot_simple_grid`
     - Interactive plotly figure of the pandapower net with geographic coordinates.
   * - :func:`~pylovo.plotting.generation.networks.plot_grid_on_map`
     - The same on an OpenStreetMap basemap (returns the figure).
   * - :func:`~pylovo.plotting.generation.networks.plot_with_generic_coordinates`
     - The net with generic tree coordinates (requires the package ``igraph``, which is not part
       of the ``plots`` extra).
   * - :func:`~pylovo.plotting.generation.networks.draw_tree_network`,
       :func:`~pylovo.plotting.generation.networks.draw_tree_network_spacing`,
       :func:`~pylovo.plotting.generation.networks.draw_radial_network`
     - Tree and radial drawings of a networkx graph of the grid
       (``pandapower.topology.create_nxgraph(net)``); node colours from ``NETWORK_COLORS``.
   * - :func:`~pylovo.plotting.generation.networks.draw_tree_network_with_spacing_from_grid_id`
     - Reads a grid and draws it with :func:`~pylovo.plotting.generation.networks.draw_tree_network_spacing`.

.. code-block:: python

   from pylovo.plotting.generation.networks import plot_contextily

   fig = plot_contextily(plz=85653, kcid=1, bcid=4, zoomfactor=17)
   fig.savefig("grid.png", dpi=150)

.. figure:: /images/plotting/plot_contextily.png
   :alt: Map of one grid with lines, buildings coloured by peak load and the transformer on an OpenStreetMap basemap
   :width: 75%

   Output of ``plot_contextily`` for PLZ 85653, kcid 1, bcid 4. Basemap and demo data
   © OpenStreetMap contributors (ODbL). Figure made with ``docs/scripts/plot_pylovo_examples.py``.

.. figure:: /images/plotting/draw_tree_network_spacing.png
   :alt: Tree drawing of a small grid with transformer, connection and consumer nodes
   :width: 100%

   ``draw_tree_network_spacing`` for a small grid (version ``docs_km``, kcid 1, bcid 2): transformer
   buses at the top, connection buses in green, consumers in light blue. The tree drawings are
   readable for grids with up to about a hundred buses.

.. note::

   The OpenStreetMap tile servers reject requests without an identifying User-Agent. If the
   basemap of ``plot_contextily`` shows "Access blocked", set one before plotting, for example
   ``import contextily.tile; contextily.tile.USER_AGENT = "my-project/1.0 (contact@example.org)"``,
   and keep the number of tile downloads small (`tile usage policy
   <https://operations.osmfoundation.org/policies/tiles/>`_).

For whole postcode areas, :mod:`pylovo.plotting.gis_preparation.io_geodata` returns the buses and
lines of all grids as GeoDataFrames
(:func:`~pylovo.plotting.gis_preparation.io_geodata.get_bus_line_geo_for_plz`); ``pylovo-export`` uses
the same functions. The maps in :doc:`../concepts/pipeline` are made from the result tables with
``docs/scripts/plot_generation_steps.py``, which can serve as an example for custom maps.
