Database setup
==============

``pylovo-setup`` prepares the schema ``pylovo`` in the database configured in ``.env``. It is the
first command to run on a new database and the only one that deletes everything.

.. danger::

   ``pylovo-setup`` executes ``DROP SCHEMA IF EXISTS pylovo CASCADE``. All versions, grids,
   transformer candidates and analysis results in that schema are lost, together with objects in
   other schemas that depend on pylovo tables (for example views). Make a backup
   (``pg_dump --schema=pylovo``) before running it on a database with results.

Set up InfDB first (``USE_INFDB=True``)
---------------------------------------

`InfDB <https://github.com/tum-ens/InfDB>`_ is an open-source infrastructure database. It
harmonises LoD2 building geometry and attributes, Zensus 2022 demographic data, official street
and address data, and postcode polygons in PostgreSQL/PostGIS. Its preprocessing assigns
households and building uses and creates street segments and building connection lines that
pylovo needs. See the `InfDB paper <https://doi.org/10.21105/joss.10458>`_ for the framework and
its `setup and import instructions <https://tum-ens.github.io/InfDB/usage/>`_ for current
commands.

#. Set up InfDB, import data for the intended region and run its pylovo building and street
   preprocessing. Confirm the tables in :ref:`infdb-tables` exist for your postcode.
#. Put the **same InfDB database** connection in pylovo's ``.env`` and set ``USE_INFDB=True``.
   pylovo reads ``basedata`` and ``opendata`` while writing its own ``pylovo`` schema alongside
   them. It does not create or populate the InfDB source tables.
#. Run ``uv run pylovo-setup`` once to create the pylovo schema.

File-based setup (``USE_INFDB=False``)
--------------------------------------

If InfDB is unavailable, prepare the postcode CSV, osm2po street SQL dump and building
shapefiles described in :ref:`file-based-input`. Set ``USE_INFDB=False`` in ``.env`` and connect
to a PostgreSQL/PostGIS database. ``pylovo-setup`` loads the postcode and street files into
``pylovo``; buildings are imported for each region during generation. This mode does not require
InfDB tables.

Running the setup
-----------------

.. code-block:: bash

   grep -E '^(HOST|PORT|DBNAME)' .env      # check the target first
   uv run pylovo-setup --help              # shows the steps, changes nothing
   uv run pylovo-setup                     # shows host, port, database; asks for the name
   uv run pylovo-setup --yes               # no prompt, for scripts and the browser UI

Without ``--yes`` the setup only starts after you typed the database name exactly; a mismatch,
:kbd:`Ctrl+D` or a non-interactive input (for example a pipe) abort with exit code 1 before
anything is changed. The setup resets ``log/`` (all files except ``.gitkeep``) and logs to
``log/log.txt``.

Prerequisites:

* the database user may create schemas and tables;
* PostGIS and pgRouting are available as extensions; the setup creates missing ones in
  ``public`` (the user then needs the privilege to create them) and refuses to run while PostGIS
  is installed in the schema ``pylovo`` (see :doc:`../getting_started/installation`);
* ``ogr2ogr`` (GDAL) is on the ``PATH``;
* with InfDB: the table ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany`` exists in the same database.

What the setup does
-------------------

.. list-table::
   :header-rows: 1
   :widths: 6 44 50
   :class: fixed-table

   * - #
     - Step
     - Details
   * - 1
     - Drop and create the schema ``pylovo``
     - :meth:`~pylovo.database.database_constructor.DatabaseConstructor.reset_schema`,
       :meth:`~pylovo.database.database_constructor.DatabaseConstructor.create_schema`.
   * - 2
     - Create the extensions and all tables
     - ``CREATE EXTENSION IF NOT EXISTS ... SCHEMA public`` for PostGIS and pgRouting, then every table of
       :doc:`../concepts/database_schema`. With InfDB the file-based input tables ``res``,
       ``oth`` and ``ways`` are skipped.
   * - 3
     - Load transformer candidates
     - Loads
       ``data/transformer_data/processed_trafos/2145268_trafos_processed_<TARGET_EPSG>.geojson``
       (Bavaria, OSM relation 2145268) into ``pylovo.transformers``. If the file does not exist
       for your ``TARGET_EPSG``, it is downloaded from the Overpass API and processed first
       (:doc:`transformer_data`).
   * - 4
     - Load postcode polygons
     - InfDB: all rows of ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany``, transformed to
       ``TARGET_EPSG``. File-based: ``data/postcode.csv``, followed by the street network from
       ``data/ways/ways_public_2po_4pgr.sql`` (:ref:`file-based-input`).
   * - 5
     - Install SQL functions
     - Loads ``src/pylovo/ways_preprocessing_functions/{utils,core}/*.sql`` (street segmentation,
       building and transformer connection lines).
   * - 6
     - Fill the municipal register
     - Joins the Gemeindeverzeichnis and RegioStaR tables in ``data/municipal_register`` into
       ``pylovo.municipal_register`` (PLZ, AGS, population, area, RegioStaR classes). Skipped if
       the table already contains rows.

Downloading and processing the transformers of a whole federal state (step 3 without the
processed file) takes long, typically 30 to 50 minutes; the other steps are much faster.

After the setup
---------------

The database now contains reference data but no grids. Continue with
:doc:`generating_grids`. Things you do **not** need to repeat the setup for:

* **More postcodes (InfDB)** -- postcodes that are missing locally are copied from InfDB on demand
  when a grid is generated.
* **More transformers** -- ``pylovo-import transformers-osm``, ``transformers-dso-csv`` or the
  editors add candidates to the existing table (:doc:`transformer_data`).
* **Changed generation parameters** -- use a new ``VERSION_ID`` (:ref:`versions`).
* **Cleaning up** -- delete versions or single postcodes with ``pylovo-delete`` (:doc:`cli`).

Temporary tables and concurrency
--------------------------------

During a run, every postcode gets its own tables ``pylovo.buildings_tem_<plz>``,
``pylovo.ways_tem_<plz>`` and ``pylovo.ways_tem_<plz>_vertices_pgr``; each database session
accesses them through temporary views ``buildings_tem``, ``ways_tem`` and
``ways_tem_vertices_pgr``. They are dropped when the postcode is finished, also after errors.

At its start, ``pylovo-generate`` drops all leftover ``*_tem_<plz>`` tables of interrupted runs.
Do not start two ``pylovo-generate`` commands against the same database at the same time; use
one command with several postcodes, which runs them in parallel worker processes.
