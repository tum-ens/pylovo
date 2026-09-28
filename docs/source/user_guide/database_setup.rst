Database setup
==============

PyLovo stores tables in the ``pylovo`` schema of the database configured in
``.env``. Run ``uv run pylovo-setup`` to create a new schema or apply pending
migrations to an existing one. Existing grids and source data are retained.
Migrations are recorded in ``pylovo.schema_migrations``.

.. danger::

   To delete all PyLovo data, use the explicit command
   ``uv run pylovo-setup reset --database NAME``. Check ``HOST``, ``PORT`` and
   ``DBNAME`` in ``.env`` first. Without ``--yes`` you must type the configured
   database name. Reset refuses to remove extensions in ``pylovo`` and rolls
   back if PostgreSQL's cascade would remove an object in another schema.
   The browser API's confirmed database setup action invokes this reset.

Set up InfDB first (USE_INFDB=True)
-----------------------------------

InfDB provides buildings, streets and postcode polygons in PostgreSQL/PostGIS.
Import the intended region and run its PyLovo preprocessing first. Set
``USE_INFDB=True`` in PyLovo's ``.env`` and point it at the same database. A new
PyLovo schema copies postcode polygons from InfDB; later runs add missing
postcodes on demand.

File-based setup (USE_INFDB=False)
----------------------------------

Prepare ``data/postcode.csv``, the osm2po street SQL dump and building
shapefiles described in :ref:`file-based-input`. On a new schema, setup loads
the postcode and street files; generation imports buildings by region.

Running setup
-------------

.. code-block:: bash

   grep -E '^(HOST|PORT|DBNAME)' .env
   uv run pylovo-setup --help
   uv run pylovo-setup
   uv run pylovo-setup reset --database NAME
   uv run pylovo-setup reset --database NAME --yes  # explicit scripted reset

On a new schema, setup creates PostGIS and pgRouting in ``public`` if needed,
creates tables, imports transformer candidates and postcode data, installs
street preprocessing functions and fills the municipal register. OSM
transformer import may fetch and process data if the processed GeoJSON is
missing. On an existing schema, setup applies pending migrations and updates
the SQL functions without reimporting raw data.

No migration marker is written for a failed step. Run database-backed tests and performance comparisons on
an isolated database, never the active InfDB.

Temporary tables and concurrency
--------------------------------

Road and building working tables are local to each database session. PostgreSQL
removes them when the session ends, even after a crash. Generation takes a
session advisory lock per postcode so two runs cannot write results for the
same postcode concurrently. Old persistent staging tables left by earlier
versions are not removed automatically; inspect their owners and activity
before manual cleanup.

The integer ``plz`` and ``ags`` database keys remain unchanged. Pad postcodes
to five digits and AGS codes to eight digits when displaying or exporting them.
The ``lines_result_cache`` table is a per-grid spatial cache, rebuilt after line
changes; ``lines_result_view`` is a compatibility SQL view over it.
