Tutorial notebooks
==================

The directory ``notebook_tutorials/grid_generation`` contains Jupyter notebooks that walk through
the generated grids of one postcode area. They read from a database with generated grids (for
example ``uv run pylovo-generate --plz 85653``) and need the ``notebooks`` extra
(``uv sync --extra notebooks``: the plotting extra plus a Jupyter kernel). Each notebook starts with
a parameter cell (``plz``, and ``kcid``/``bcid`` of one grid; ``None`` selects the first grid).
``notebook_tutorials/README.md`` explains the kernel selection in VS Code and JupyterLab. The stored
outputs show the OSM-derived demo region 85653 (Aying; map data © OpenStreetMap contributors, ODbL).

.. list-table::
   :header-rows: 1
   :widths: 36 64
   :class: fixed-table

   * - Notebook
     - Content
   * - ``0_basic_grid_generation.ipynb``
     - How grids are identified (``plz``, ``kcid``, ``bcid``): list the grids of a PLZ with a
       :class:`~pylovo.database.database_client.DatabaseClient` (``get_list_from_plz``), read one
       pandapower net (``read_net_db``) and plot it.
   * - ``1_map_visualization_examples.ipynb``
     - Build a map layer by layer: postcode area, buildings, building clusters, one grid with
       its buildings coloured by peak load, the transformer, the streets and finally the cables.
   * - ``2_0_LV_pandapower_network.ipynb``
     - The pandapower data model of a generated grid: bus types (MV bus, LV bus, connection
       buses, consumer buses), loads, lines, the transformer table and power-flow results.
   * - ``2_1_pandapower_networks_generic_coord.ipynb``
     - Plot a grid with generic (tree-like) coordinates instead of geographic ones.
   * - ``3_LV_networkx_graph.ipynb``
     - Convert a grid into a networkx graph, draw it as tree or radial graph and compute path
       lengths from the transformer to the consumers.
   * - ``4_basic_plotting.ipynb``
     - Statistics of all grids of a postcode area (transformer sizes, cable types, distances);
       needs the PLZ analysis (``pylovo-analyze --plz <plz>``).
