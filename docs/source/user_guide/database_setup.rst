Database setup
==============

PyLovo stores tables in the ``pylovo`` schema of the database configured in
``.env``. Run ``uv run pylovo-setup`` to create a new schema or apply pending
migrations to an existing one. Existing grids and source data are retained.
Migrations are recorded in ``pylovo.schema_migrations`` and validate existing
rows before committing new foreign keys and checks.

.. danger::

   To delete all PyLovo data, use the explicit command
   ``uv run pylovo-setup reset --database NAME``. Check ``HOST``, ``PORT`` and
   ``DBNAME`` in ``.env`` first. Without ``--yes`` you must type the configured
   database name. Reset refuses to remove extensions in ``pylovo`` and refuses
   while objects of other schemas depend on it (for example views or foreign keys
   of other tools on ``grid_result``). It renames the schema to
   ``pylovo_backup_<UTC time>``, builds a new one and drops the backup once the
   rebuild is complete. If the rebuild fails or is cancelled, it drops the new
   schema and renames the backup back, so the previous data is kept. A backup
   schema left by a killed process is reported by the next setup run.
   The HTTP API's reset job (typed confirmation) invokes this reset; its setup
   job runs the non-destructive setup.

Set up InfDB first (USE_INFDB=True)
-----------------------------------

InfDB provides buildings, streets and postcode polygons in PostgreSQL/PostGIS.
Import the intended region and run its PyLovo preprocessing first. Set
``USE_INFDB=True`` in PyLovo's ``.env`` and point it at the same database. A new
PyLovo schema copies postcode polygons from InfDB; later runs add missing
postcodes on demand.

To keep a central InfDB untouched, the schema ``pylovo`` and its results can live
in another database, e.g. on your own machine: point ``DBNAME``, ``HOST`` and the
other connection variables at that database, set ``USE_EXTERNAL_BASEDATA=True``
and give the central InfDB as ``INFDB_DBNAME``, ``INFDB_HOST``, ``INFDB_PORT``,
``INFDB_USER`` and ``INFDB_PASSWORD``. PyLovo only reads from the central InfDB,
in short read-only transactions, also through PgBouncer. This covers the LV grids and the
HTTP API; the MV stage still needs the InfDB schemas in the pylovo database.

File-based setup (USE_INFDB=False)
----------------------------------

Prepare ``postcode.csv``, the osm2po street SQL dump and the building
shapefiles in the data directory, as described in :ref:`file-based-input`. On a new schema, setup loads
the postcode and street files; generation imports buildings by region.

Running setup
-------------

.. code-block:: bash

   grep -E '^(HOST|PORT|DBNAME)' .env
   uv run pylovo-setup --help
   uv run pylovo-setup
   uv run pylovo-setup reset --database NAME
   uv run pylovo-setup reset --database NAME --yes  # explicit scripted reset

Setup first checks that the server runs PostgreSQL 15 or newer (the schema uses foreign
keys with ``ON DELETE SET NULL (column)``) and stops before any change otherwise. It
creates PostGIS and pgRouting in ``public`` if needed, creates missing
tables and applies pending migrations. It then imports every reference table
that is still empty: transformer candidates, postcode data (and the street
network with ``USE_INFDB=False``) and the municipal register. Tables that have
rows are kept, so running setup again completes an interrupted setup. Before
it changes anything, setup checks the input data of the tables it will import
(the InfDB table ``postcodes_germany``, the CSV and street files, the municipal
register files) and stops with a list of what is missing. Finally
it installs or updates the street preprocessing functions. The OSM transformer
import reprojects the shipped Bavarian GeoJSON for another ``TARGET_EPSG`` and fetches data only
for another relation (``RELATION_ID``) without a processed GeoJSON.

A migration stops if existing rows violate a new constraint, with the message
``Migration <name> stopped, nothing was changed: ... Repair or delete these rows, then run
pylovo-setup again.`` It names the constraint or column. Correct those rows in a reviewed data
repair and rerun setup. A migration also stops, and names them, if views of other schemas depend
on a view it has to rebuild. No migration marker is written for a failed step. Run
database-backed tests and performance comparisons on an isolated database, never the active
InfDB.

Migration ``0012_indexes`` adds and drops indexes of the result tables and drops a function of
v0.8.0 whose SQL file is gone. On a large database it can take a while. Migration
``0013_schema_cleanup`` deletes the tables ``mastr_units`` and ``mastr_postcode_aggregates`` (left by
databases that ran an earlier version of ``0009_mv_grids``, which imported MaStR data) and
``grid_parameters`` (no code writes it), together with their rows, and renames the primary key and the
identity sequence of ``lines_result_cache`` to the names of a fresh setup (a database set up by v0.8.0
keeps the names ``lines_result_view_*``). Migration ``0014_classification_choices`` adds the columns
``list_of_clustering_parameters``, ``n_clusters_kmeans``, ``n_clusters_gmm`` and ``additional_filtering`` to
``classification_version``, where step 7 of the classification stores its choices. Classifications made before keep
NULL there and use ``config_clustering.yaml`` (:doc:`classification`). A database that an
earlier version set up needs one ``pylovo-setup`` run before generating: until all migrations are
recorded, ``pylovo-generate`` stops with ``PyLovo schema has pending migrations (...); run
pylovo-setup first``.

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
changes; ``lines_result_view`` is a compatibility SQL view over it. The
``buildings_result_with_grid`` layer is now a live SQL view. Its ``result_uid``
has a collision-safe format, so external projects that saved the old
``version_id_objectid`` value as a feature identifier must reload those identifiers.
``grid_result.grid`` remains the complete pandapower network document used for
network reconstruction; the typed pandapower tables serve SQL queries and reports.
The existing ``postcode_result_plz``, ``vertice_id`` and ``ont_vertice_id``
column names remain available to callers.
