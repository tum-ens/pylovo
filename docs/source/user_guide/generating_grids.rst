Generating grids
================

``pylovo-generate`` runs the pipeline of :doc:`../concepts/pipeline` for every selected postcode
area and stores the grids under the ``VERSION_ID`` of ``config/config_generation.yaml``.

.. code-block:: bash

   uv run pylovo-generate --plz 85653

.. figure:: /images/generation/step5_grids_by_grid.png
   :alt: Four generated LV grids of PLZ 85653, one colour per grid, with transformer ratings
   :width: 90%

   Result for the demo region PLZ 85653 with the shipped configuration: four greenfield grids.
   Demo extract derived from OpenStreetMap, © OpenStreetMap contributors, ODbL; figure made with
   ``docs/scripts/plot_generation_steps.py``.

Workflow with versions
----------------------

#. Choose a new numeric ``VERSION_ID`` (for example ``2`` after ``1``; at most 10 characters)
   and describe it in ``VERSION_COMMENT``.
#. Change the parameters in ``config/config_generation.yaml`` (and, if needed, the power-flow
   limits in ``config_analysis.yaml``).
#. Run ``pylovo-generate``. The first run stores the parameter snapshot of the version; later runs
   with the same ``VERSION_ID`` must use the same parameters (:ref:`versions`).
#. Compare versions with SQL, the analysis tables or QGIS (filter on ``version_id``).

Postcodes that already have grids in the active version are skipped, so an interrupted
multi-postcode run can simply be started again. To regenerate a postcode, delete it first:

.. code-block:: bash

   uv run pylovo-delete networks --plz 85653 --version 1

Sequential and parallel runs
----------------------------

A single postcode runs in the current process. With several postcodes (``--plz`` with more than
one value, or ``--ags``), pylovo starts up to ``N_JOBS`` worker processes, one postcode per
process, where ``N_JOBS`` is ``N_JOBS_PERCENT`` percent of the CPU cores, rounded (at least 1).
Every worker has its own database connection. If ``N_JOBS`` is 1 (for example 50 % of 2 cores),
the postcodes run sequentially.

* ``--no-parallel`` (or ``PARALLEL: False`` in the configuration) processes the postcodes one
  after the other in the main process, which is easier to debug; ``--parallel`` forces worker
  processes.
* A failing postcode does not stop the others; a parallel run ends with a summary such as
  ``Parallel grid generation finished with 1 failed PLZ: 80805``.
* :kbd:`Ctrl+C` cancels pending postcodes and waits up to ``GRACEFUL_SHUTDOWN_TIMEOUT`` seconds
  (default 5) for running ones. Temporary tables of interrupted postcodes are removed at the
  next start.
* Every postcode is one database transaction: either all its grids are stored or none.

Logs
----

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - File
     - Content
   * - console
     - Progress of all steps, warnings and errors (level ``LOG_LEVEL``).
   * - ``log/log.txt``
     - Log of the main process (appended; setup keeps previous log files).
   * - ``log/log_<plz>.txt``
     - Log of one postcode in a parallel run (overwritten when the postcode runs again).

Useful messages to look for:

``Settlement type determined (avg_households_per_building=..., house_distance=..., settlement_type=...)``
   The settlement type selects the allowed transformer ratings (``TRANSFORMER_MAPPING``).
``BCID dimensioning complete for PLZ ..., KCID ...: n single-transformer clusters``
   Result of the building clustering of one k-means cluster.
``End-to-end feeder voltage sizing finished ...`` and ``Feeder planning voltage-drop envelope remains violated``
   Outcome of the voltage-drop planning (:doc:`../concepts/grid_dimensioning`).
``Grid with kcid:... bcid:... will be stored with status=voltage_violation``
   The power-flow check found voltages outside ``POWER_FLOW_VOLTAGE_LIMITS``; the grid is kept.
``Cable installation finished for PLZ ...: processed_clusters=..., power_flow_converged=.../...``
   Summary of one postcode.

Results
-------

A grid is identified by ``(version_id, plz, kcid, bcid)``: ``kcid`` is the k-means cluster (street
component), ``bcid`` the building cluster inside it. Brownfield grids built around existing
transformers have negative ``bcid`` values, greenfield grids positive ones.

.. list-table::
   :header-rows: 1
   :widths: 30 70
   :class: fixed-table

   * - Table
     - Content per grid
   * - ``grid_result``
     - Transformer rating and type, ``power_flow_status`` (``converged``, ``voltage_violation`` or
       ``not_converged``), planning and solved voltage-drop diagnostics, the pandapower net as
       JSON (``grid``).
   * - ``transformer_positions``
     - Transformer location (and ``osm_id`` for brownfield positions).
   * - ``buildings_result``
     - Supplied buildings with loads, households and routing vertices.
   * - ``lines_result``, ``lines_result_cache``
     - Feeder and service lines as geometries (the cache adds offsets for parallel feeders; lines_result_view remains a compatibility SQL view).
   * - ``pandapower_bus``, ``pandapower_line``, ``pandapower_trafo``, ``pandapower_load``
     - The pandapower tables as SQL rows, for queries without JSON parsing.

``ways_result`` and ``postcode_result`` hold the street graph and the settlement metrics of the
postcode. The full schema is described in :doc:`../concepts/database_schema`. With
``SAVE_GRID_FOLDER: True`` every net is also written to
``results/grids/version_<VERSION_ID>/<plz>/kcid<k>bcid<b>.json``.

Example: grids and diagnostics of a postcode

.. code-block:: sql

   SELECT kcid, bcid, transformer_rated_power AS kva, power_flow_status,
          round(selected_max_feeder_voltage_drop_percent::numeric, 2) AS feeder_drop_pct,
          feeder_voltage_drop_limit_met,
          round(max_total_lv_voltage_drop_pu::numeric, 3) AS solved_drop_pu
   FROM pylovo.grid_result
   WHERE version_id = '1' AND plz = 85653
   ORDER BY kcid, bcid;

.. code-block:: text

    kcid | bcid | kva | power_flow_status | feeder_drop_pct | feeder_voltage_drop_limit_met | solved_drop_pu
   ------+------+-----+-------------------+-----------------+-------------------------------+----------------
       1 |    1 | 400 | converged         |            7.19 | t                             |          0.062
       1 |    2 | 800 | converged         |            7.97 | t                             |          0.071
       1 |    3 | 630 | voltage_violation |            9.77 | f                             |          0.089
       1 |    4 | 630 | converged         |            7.93 | t                             |          0.072

Using the Python API
--------------------

The command-line tool is a thin wrapper around :class:`pylovo.grid_generator.GridGenerator`:

.. code-block:: python

   import pandas as pd
   from pylovo.grid_generator import GridGenerator

   gg = GridGenerator(plz=85653)
   gg.generate_grid_for_single_plz(plz=85653, analyze_grids=True)

   # several postcodes, in parallel worker processes
   GridGenerator().generate_grid_for_multiple_plz(
       df_plz=pd.DataFrame({"plz": [80803, 80802]}), analyze_grids=False, parallel=True
   )

Configuration values are read when :mod:`pylovo.config_loader` is imported, so start a new Python
process after editing the YAML files. In the file-based mode, import the building shapefiles
first with :func:`pylovo.data_import.import_buildings.import_buildings_for_single_plz`.
