Troubleshooting
===============

Messages are quoted as pylovo prints or logs them. Check ``log/log.txt`` (and ``log/log_<plz>.txt``
for parallel runs) for the full context.

Connection and configuration
----------------------------

``ValueError: Missing required environment variable: DBNAME``
   No ``.env`` was found or it lacks the variable. Create ``.env`` in the repository root
   (:ref:`env-variables`).

pylovo connects to an unexpected database
   ``.env`` overrides environment variables. Check the file that pylovo finds (the first ``.env``
   above the installed package) with ``grep -E '^(HOST|PORT|DBNAME)' .env``.

``Connecting to <db> was not successful. Make sure, that you have established the SSH connection ...``
   The database is not reachable with the settings in ``.env``: check host, port, password and,
   for remote databases, the SSH tunnel.

``FileNotFoundError: Config file 'config_generation_lv.yaml or config_generation.yaml' not found in any search location.``
   Run the command from the repository root or set ``PYLOVO_CONFIG_DIR``
   (:ref:`config-file-lookup`). The message lists the folders that were searched.

Setup
-----

``FileNotFoundError: [Errno 2] No such file or directory: 'ogr2ogr'`` (during ``pylovo-setup``)
   Install GDAL (``sudo apt install gdal-bin``).

``function pgr_extractvertices(...) does not exist`` or ``extension "pgrouting" is not available``
   pgRouting is missing or older than 3.8 on the database server. Install the pgRouting package
   for your PostgreSQL version and create the extension (:doc:`../getting_started/installation`).

``pylovo needs PostgreSQL 15 or newer, the server runs PostgreSQL ...``
   The server is older than PostgreSQL 15. Nothing was changed. Use a newer server, for example a
   container, or another database cluster (:doc:`../getting_started/installation`).

``Migration <name> stopped, nothing was changed: ... Repair or delete these rows, then run pylovo-setup again.``
   Existing rows violate a constraint that the migration adds. The message names it. Correct or
   delete the rows and run ``pylovo-setup`` again (:doc:`database_setup`).

``PyLovo schema has pending migrations (...); run pylovo-setup first``
   The schema is older than the code, for example after an upgrade. Run ``uv run pylovo-setup``
   once: it applies the migrations and keeps all grids. The message lists the pending migrations
   (``PyLovo schema is not migrated`` for a schema without the migration ledger). Run the setup
   when no generation is running.

The Overpass download fails (HTTP 429 or 504, or a timeout)
   The public Overpass API limits and queues requests. Wait and retry, or use the processed
   GeoJSON files that ship with the repository.

Generation
----------

``Generation parameters differ from the stored snapshot for version <id>``
   The configuration changed since the version was created. Set a new ``VERSION_ID``
   (:ref:`versions`), or restore the previous parameters.

``Error during grid generation for PLZ <plz>: ...`` followed by ``Skipped PLZ <plz> due to generation error.``
   The postcode failed and was skipped, the others continue. ``pylovo-generate`` still exits with
   0, so read the error line and the context above it in ``log/log.txt`` (``log/log_<plz>.txt``
   for parallel runs).

``Grid for the postcode area <plz> has already been generated.``
   The postcode is skipped because grids exist in the active version. Delete them with
   ``pylovo-delete networks --plz <plz> --version <id>`` or use a new version.

``PLZ <plz> not found in InfDB opendata.postcodes_germany``
   The postcode has no polygon in InfDB; check the PLZ and ``INFDB_OPENDATA_SCHEMA``.

``No ways found in remote DB intersecting the given PLZ geometry``
   InfDB has no street segments for the postcode. Run the InfDB street preprocessing for the
   municipality and check ``INFDB_SOURCE_SCHEMA`` (``basedata`` for current InfDB releases).

``Settlement type classification failed`` followed by ``No settlement_type found in postcode_result``
   The postcode has no residential buildings with households, so the settlement type (and with it
   the allowed transformer ratings) cannot be determined.

``Invalid source building area components: negative=..., incomplete=..., inconsistent with gross floor area=...``
   The residential and non-residential floor areas of some InfDB buildings do not add up to
   ``floor_area * floor_number`` (:doc:`input_data`). Fix the input data.

``PLZ not found in municipal_register`` or ``AGS not found in municipal_register``
   The code is unknown to the register filled by ``pylovo-setup`` (needed for ``--ags`` and in the
   file-based mode).

A parallel run processes one postcode at a time
   ``N_JOBS_PERCENT`` of the CPU cores rounds to 1 worker. Increase ``N_JOBS_PERCENT``.

A run was interrupted
   Start it again: postcodes that are already complete are skipped, and the working tables of the
   interrupted run are session-local and gone.

Grids with ``voltage_violation`` or ``not_converged``
   The grids are stored anyway. ``voltage_violation`` means that the validation snapshot has bus
   voltages outside ``POWER_FLOW_VOLTAGE_LIMITS``; often ``feeder_voltage_drop_limit_met`` is false
   as well because even the largest configured feeder cable cannot meet the planning limit. Look
   at the diagnostics in ``grid_result`` (:doc:`../concepts/grid_dimensioning`). Larger cables in
   ``FEEDER_CABLES`` or different clustering limits change the result (in a new version).

MV stage
--------

The messages of ``pylovo-mv`` and of ``pylovo-generate --mv`` are explained in
:ref:`mv-troubleshooting`.

Plotting
--------

The basemap of ``plot_contextily`` shows "Access blocked"
   The OpenStreetMap tile servers require an identifying User-Agent; see the note in
   :doc:`exporting_visualising`.

``ModuleNotFoundError: No module named 'matplotlib'`` (or ``contextily``, ``plotly``)
   Install the plotting extra: ``uv sync --extra plots``.
