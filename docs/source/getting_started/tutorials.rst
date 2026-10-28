Tutorial notebooks
==================

Learning path
-------------

Six notebooks show what pylovo produces and how to use it, one question each. They need only the ``notebooks`` extra
(``uv sync --extra notebooks``): without a database, or without the areas in it, they read the example data in
``data/examples`` (:class:`pylovo.examples.TutorialData`), three typical Bavarian postcode areas of a run for all of
Bavaria with four version variants. The pages below show their stored outputs; the notebooks are in
``notebook_tutorials/`` of the repository, and ``notebook_tutorials/README.md`` explains the kernel selection in
VS Code and JupyterLab.

.. grid:: 1 2 3 3
   :gutter: 3

   .. grid-item-card:: 1 · A tour of pylovo
      :img-top: /tutorials/_thumbs/01_tour.png
      :link: /tutorials/01_tour
      :link-type: doc
      :class-img-top: tutorial-card-image

      What does pylovo produce? All grids of a postcode area, one grid and its voltage profile.

   .. grid-item-card:: 2 · How pylovo builds the grids
      :img-top: /tutorials/_thumbs/02_building_grids.png
      :link: /tutorials/02_building_grids
      :link-type: doc
      :class-img-top: tutorial-card-image

      Settlement type, buildings and loads, connections, clusters and stations, cables and the checks.

   .. grid-item-card:: 3 · One grid in depth
      :img-top: /tutorials/_thumbs/03_one_grid.png
      :link: /tutorials/03_one_grid
      :link-type: doc
      :class-img-top: tutorial-card-image

      The pandapower model, feeders and their voltage, loading at peak load, the grid as a tree, export.

   .. grid-item-card:: 4 · City, suburb and village
      :img-top: /tutorials/_thumbs/04_comparing_areas.png
      :link: /tutorials/04_comparing_areas
      :link-type: doc
      :class-img-top: tutorial-card-image

      Three areas side by side, and all Bavarian grids by settlement type.

   .. grid-item-card:: 5 · How much do the settings matter?
      :img-top: /tutorials/_thumbs/05_versions.png
      :link: /tutorials/05_versions
      :link-type: doc
      :class-img-top: tutorial-card-image

      Existing stations, a longer reach, cable cabinets and larger stations against the defaults.

   .. grid-item-card:: 6 · From the HV grid to the house
      :img-top: /tutorials/_thumbs/06_voltage_levels.png
      :link: /tutorials/06_voltage_levels
      :link-type: doc
      :class-img-top: tutorial-card-image

      One LV grid on its MV ring, the HV/MV substation and the 110 kV grid.

Classification
--------------

Two notebooks walk through a classification of 501 Bavarian postcode areas (:doc:`../user_guide/classification`).
They read the classification from the database.

.. grid:: 1 2 3 3
   :gutter: 3

   .. grid-item-card:: Sample and key figures
      :img-top: /tutorials/_thumbs/01_sample_and_key_figures.png
      :link: /tutorials/classification/01_sample_and_key_figures
      :link-type: doc
      :class-img-top: tutorial-card-image

      Which key figures describe an LV grid, and which should the clustering use?

   .. grid-item-card:: Clusters and representatives
      :img-top: /tutorials/_thumbs/02_clusters_and_representatives.png
      :link: /tutorials/classification/02_clusters_and_representatives
      :link-type: doc
      :class-img-top: tutorial-card-image

      Which types of grids are there in Bavaria, and which grid represents each?

.. toctree::
   :hidden:

   /tutorials/01_tour
   /tutorials/02_building_grids
   /tutorials/03_one_grid
   /tutorials/04_comparing_areas
   /tutorials/05_versions
   /tutorials/06_voltage_levels
   /tutorials/classification/01_sample_and_key_figures
   /tutorials/classification/02_clusters_and_representatives

Previous LV notebooks
---------------------

The directory ``notebook_tutorials/grid_generation`` contains the earlier Jupyter notebooks that walk through
the generated grids of one postcode area, kept with their stored outputs for comparison. Four of them call plotting
functions that were removed, so they no longer run. They read from a database with generated grids (for
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

MV notebooks
------------

The directory ``notebook_tutorials/mv_grids`` contains notebooks on the MV grids of the MV stage
(:doc:`../user_guide/mv_grids`). They read the MV variants of an LV version (built with
``pylovo-mv generate``) and carry no stored outputs. Their parameter cell selects the LV version
and the variants; ``notebook_tutorials/mv_grids/README.md`` describes all parameters.

.. list-table::
   :header-rows: 1
   :widths: 36 64
   :class: fixed-table

   * - Notebook
     - Content
   * - ``0_mv_generation.ipynb``
     - How to run the MV stage, the MV variants of a version with their parameter differences and
       run summaries, and the quality tables of the routing graph.
   * - ``1_mv_results_overview.ipynb``
     - Tables per variant, area class and supply area; maps of the supply areas and rings;
       distributions of ring sizes, lengths, loadings, voltages and MV/LV taps.
   * - ``2_mv_variant_comparison.ipynb``
     - Variants against a reference variant (parameter differences, metrics, distributions, ring
       membership) and the same variant on two LV versions.
   * - ``3_mv_plausibility.ipynb``
     - Metrics against literature ranges and Ding0 grids, the demand anchor and earth-fault
       currents (:mod:`pylovo.analysis.mv_validation`).
   * - ``4_mv_lv_coupling.ipynb``
     - Results of ``pylovo-mv couple``: LV busbar voltages, band conflicts, taps and the merged
       operating point.
