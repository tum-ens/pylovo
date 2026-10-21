Command-line reference
======================

pylovo installs these commands (``[project.scripts]`` in ``pyproject.toml``). Run them from the
repository root, either with ``uv run <command>`` or in the activated environment. All of them
read ``.env`` and ``config/*.yaml`` at start (:doc:`configuration`), after they have read their
arguments: ``--help``, the help that ``pylovo-delete`` and ``pylovo-import`` show without a command,
and wrong arguments are answered without a ``.env`` and without configuration files.

Exit codes: 0 on success, 1 when the command stops with an error (also an aborted reset or delete, an MV
stage that does not end ``done``, ``pylovo-analyze`` for a postcode without grids and
``pylovo-export`` when no postcode has grids), 2 for wrong arguments, 3 for
``pylovo-analyze --coverage`` when source buildings are missing, and 4 for ``pylovo-generate``
when the run finished but postcodes failed or got no grids. Such postcodes are logged and
skipped, the others are generated, and one line ``✗ N of M PLZ not generated: ...`` names them;
the reasons are in the log (``Skipped PLZ <plz> due to generation error.``, :doc:`troubleshooting`).

.. list-table::
   :header-rows: 1
   :widths: 25 75
   :class: fixed-table

   * - Command
     - Purpose
   * - ``pylovo-setup``
     - Create the schema ``pylovo`` with reference data, or migrate an existing one in place.
       Only ``reset`` drops existing results.
   * - ``pylovo-generate``
     - Generate grids for postcodes or municipalities.
   * - ``pylovo-mv``
     - Generate and manage the MV grids of a stored LV version (:doc:`mv_grids`).
   * - ``pylovo-analyze``
     - Compute postcode and per-grid key figures, and the coverage and border reports.
   * - ``pylovo-classify``
     - Group the generated grids into representative grid types (:doc:`classification`).
   * - ``pylovo-import``
     - Import transformer candidates (OSM, DSO CSV).
   * - ``pylovo-export``
     - Export grid buses and lines as CSV.
   * - ``pylovo-delete``
     - Delete versions, postcodes, transformer candidates or classifications.
   * - ``pylovo-api``
     - Start the HTTP API of the GridPlanner UI (:doc:`http_api`).

pylovo-setup
------------

.. code-block:: text

   pylovo-setup [-h] [--database DATABASE] [--yes] [{setup,reset}]

With no command, creates a new schema or migrates an existing one in place and
imports every reference table that is still empty. The database server must run
PostgreSQL 15 or newer, which the command checks before it changes anything
(:doc:`../getting_started/installation`). Only ``reset --database NAME``
deletes the PyLovo schema; the name must match ``DBNAME`` in ``.env``. Reset asks
you to type the database name unless ``--yes`` is supplied; it keeps the previous
schema as a backup until the rebuild is complete and restores it if the rebuild
fails. The HTTP API's reset job runs this command after its
own typed confirmation; its setup job runs the non-destructive ``pylovo-setup``. See :doc:`database_setup`.

pylovo-generate
---------------

.. code-block:: text

   pylovo-generate [-h] [--plz PLZ [PLZ ...]] [--ags AGS [AGS ...]] [--parallel | --no-parallel]
                   [--mv | --no-mv]

Generates the grids of the given regions under the ``VERSION_ID`` of the configuration
(:doc:`generating_grids`). Exactly one of ``--plz`` and ``--ags`` is required; they cannot be
combined. Several postcodes run in parallel when ``PARALLEL`` in the configuration is ``True``
(the default); the flags override it.

``--plz PLZ [PLZ ...]``
   One or more postcodes (5 digits, integers).
``--ags AGS [AGS ...]``
   One or more municipality keys (8 digits, integers); all postcodes of the municipalities are
   generated (:doc:`region_selection`).
``--parallel`` / ``--no-parallel``
   Process several postcodes (also those of ``--ags``) in worker processes, or one after the
   other in the main process, whatever ``PARALLEL`` says.
``--mv`` / ``--no-mv``
   Run the MV stage after the LV grids of all postcodes, for the configured MV variant, or skip it,
   whatever ``MV_GRID_GENERATION`` says (:doc:`mv_grids`). The MV parameters are checked before the
   LV generation starts, so a wrong cable type stops the command at once. The command exits with
   code 1 if the MV stage does not end ``done``; the LV grids stay stored.

.. code-block:: bash

   pylovo-generate --plz 80803
   pylovo-generate --plz 80803 80802 80801 --no-parallel
   pylovo-generate --ags 09162000
   pylovo-generate --plz 80803 80802 --mv

pylovo-mv
---------

.. code-block:: text

   pylovo-mv [-h] {generate,couple,delete,list,graph,export,boundary,display} ...

Generates and manages the MV grids of stored LV versions (:doc:`mv_grids`). Every subcommand takes
the LV version explicitly with ``--version``; ``VERSION_ID`` of the configuration is not used.

``pylovo-mv generate --version VERSION (--plz PLZ [PLZ ...] | --ags AGS [AGS ...] | --all) [OPTIONS]``
   Run the MV stage for one MV variant of the version. The options are ``--variant``,
   ``--comment``, ``--set KEY=VALUE ...`` (``MV_*`` keys of this variant, :ref:`mv-variants`),
   ``--parallel`` or ``--no-parallel``, ``--jobs`` and ``--replace``.
``pylovo-mv couple --version VERSION --variant VARIANT [--grid GRIDS ...] [--jobs JOBS] [--no-merged]``
   Check the done MV grids with the stored LV grids and solve the merged MV and LV operating point.
``pylovo-mv list [--version VERSION]``
   List the MV variants with status, postcodes, grids, retired options, length and stale flag.
``pylovo-mv delete --version VERSION [--variant VARIANTS ...] [--yes]``
   Delete MV variants with all their results (all variants of the version without ``--variant``).
   Asks you to type the database name first, like ``pylovo-delete``; ``--yes`` skips the question and
   is required without a terminal on stdin. When no variant matches the command exits with 1.
``pylovo-mv export --version VERSION --variant VARIANT --grid GRID --output OUTPUT [--case CASE] [--coupled]``
   Write the pandapower net of one MV grid (case ``load``, ``n1a`` or ``n1b``) to a JSON file.
``pylovo-mv boundary --version VERSION --variant VARIANT --output OUTPUT``
   Write the HV boundary of a variant (eTraGo 110 kV bus, P and Q per HV/MV substation) to a CSV
   file.
``pylovo-mv display --version VERSION --variant VARIANT [VARIANT ...]``
   Store the display geometry of variants again.
``pylovo-mv graph --version VERSION (--plz PLZ [PLZ ...] | --ags AGS [AGS ...] | --all) [--qa]``
   Build or reuse the MV routing graph of the postcodes and print its quality tables.

Every output line starts with ✓ or ✗. The exit code is 0 when the command, the variant or the
coupling is done, and 1 otherwise.

.. code-block:: bash

   pylovo-mv generate --version 1 --all --variant r15 --set MV_MAX_STATIONS_PER_RING=15
   pylovo-mv couple --version 1 --variant r15
   pylovo-mv list --version 1

pylovo-analyze
--------------

.. code-block:: text

   pylovo-analyze [-h] --plz PLZ [PLZ ...] [--per-grid] [--all] [--coverage] [--border-diagnostics]
                  [--version-id VERSION_ID] [--output OUTPUT]

Computes key figures of the grids of one or more postcodes in the active version
(:doc:`analysing_grids`). Without further options only the postcode key figures
(``plz_parameters``) are computed. A postcode without grids in the version, or with a missing
postcode analysis for ``--per-grid``, is reported with ✗ and the command exits with 1; a
postcode that is already analysed is skipped.

``--plz PLZ [PLZ ...]``
   Postcodes to analyse (required).
``--per-grid``
   Compute the key figures of every grid (``clustering_parameters``); the postcode key figures
   must exist already.
``--all``
   Postcode key figures followed by the per-grid key figures.
``--coverage``
   Account for every source building of the postcodes (in a grid, left out with a reason, or
   missing) and write CSV and JSON reports. Exits with 3 if any building is missing.
``--border-diagnostics``
   Report the postcode-border and data-edge context of the postcodes and their grids.
``--version-id VERSION_ID``, ``--output OUTPUT``
   Version (default ``VERSION_ID`` of the configuration) and directory (default
   ``results/diagnostics``) of the two reports. They have no effect on the key figures, which
   always use ``VERSION_ID`` of the configuration. ``--coverage`` and ``--border-diagnostics``
   replace the key-figure calculation: do not combine them with ``--per-grid`` or ``--all``.
   An option that the chosen mode ignores prints a warning. Details in :doc:`analysing_grids`.

pylovo-classify
---------------

.. code-block:: text

   pylovo-classify [-h]

Runs the classification of :doc:`classification` interactively: it draws the sample set of
``CLASSIFICATION_VERSION``, generates and analyses the sample grids, filters them, chooses the
clustering parameters and the numbers of clusters, clusters the grids and writes
``pylovo.transformer_classified``. It takes no options and asks three yes/no questions (``y`` and
``n`` are accepted). It needs the plotting extra (``uv run --extra plots pylovo-classify``) and
writes the chosen parameters and numbers of clusters to ``config/config_clustering.yaml``.

pylovo-import
-------------

.. code-block:: text

   pylovo-import [-h] {transformers-osm,transformers-dso-csv,transformers-ui} ...

Imports data into the database (:doc:`transformer_data`). Without a subcommand the help is shown.

``pylovo-import transformers-osm --relation-id RELATION_ID``
   Download the transformers inside the OSM relation from the Overpass API, process them and
   append them to ``pylovo.transformers``. Works for federal states as well as small areas.

``pylovo-import transformers-dso-csv [--source SOURCE] [--replace-source] csv_path``
   Import DSO transformer positions from a CSV file with the columns ``external_id``, ``lon``,
   ``lat`` and optionally ``transformer_rated_power`` and ``source``.

   ``--source SOURCE``
      Source label for the ids ``dso/<source>/<external_id>``; overrides a ``source`` column.
   ``--replace-source``
      Delete the existing rows ``dso/<source>/...`` before the import.

``pylovo-import transformers-ui``
   Deprecated: the old Flask transformer map. It needs the extra ``legacy-ui``
   (``uv sync --extra legacy-ui``); the transformer editor of GridPlanner covers all its features
   (:doc:`browser_ui`).

pylovo-export
-------------

.. code-block:: text

   pylovo-export [-h] --plz PLZ [PLZ ...] [--grid] [--kcid KCID] [--bcid BCID] [--output OUTPUT]

Writes buses and lines of the grids of the active version as CSV (:doc:`exporting_visualising`).

``--plz PLZ [PLZ ...]``
   Postcode(s) to export (required).
``--grid``
   Export a single grid; needs exactly one postcode and ``--kcid`` and ``--bcid``.
``--kcid KCID``, ``--bcid BCID``
   Identifiers of the grid for ``--grid``; ``bcid`` is negative for brownfield grids.
``--output OUTPUT``
   Output directory (default ``QGIS/`` in the working directory; existing files are overwritten).

A postcode without grids in the active version is reported and skipped. If none of the postcodes has
grids, nothing is written and the command exits with 1. ``--kcid`` and ``--bcid`` without ``--grid``
are ignored with a warning.

pylovo-delete
-------------

.. code-block:: text

   pylovo-delete [-h] {networks,version,transformers,classification} ...

Deletes data from the database. Without arguments the help is shown. Deleting versions or
postcodes cascades to all dependent result tables and refreshes the materialised views.

Every command that deletes shows the database (host, port and name) and what it deletes, and asks
you to type the database name, like ``pylovo-setup reset``. ``--yes`` skips the question. Without a
terminal on stdin (a script, a job) and without ``--yes`` nothing is deleted and the command exits
with 1. The HTTP API passes ``--yes`` after its own typed confirmation.

``pylovo-delete version --version VERSION_ID [VERSION_ID ...] [--yes]``
   Delete one or more versions with all their grids in all postcodes, their equipment data and
   parameter snapshots. Unknown versions are reported and nothing is deleted. The option
   ``pylovo-delete --version`` of earlier releases is gone and names this subcommand in its error
   message.

``pylovo-delete networks --plz PLZ --version VERSION [--yes]``
   Delete the grids of one postcode in one version (the version itself is kept). The MV variants
   of the version that contain the postcode are deleted as well, because an MV grid spans several
   postcodes.

``pylovo-delete transformers [--yes]``
   Empty ``pylovo.transformers``, including the manual and DSO positions, which cannot be
   recreated from the OSM files. Generated grids keep their transformer positions but lose the
   link to the raw transformer (``osm_id`` becomes ``NULL``), see :doc:`transformer_data`.

``pylovo-delete classification --version CLASSIFICATION_ID [--yes]``
   Delete a classification with its sample set and result and refresh the view
   ``transformer_classified_with_grid`` (:doc:`classification`). The id is the integer
   ``CLASSIFICATION_VERSION``, not a ``VERSION_ID``; an unknown id is reported and nothing is deleted.

.. code-block:: bash

   pylovo-delete version --version docs_bf docs_km
   pylovo-delete networks --plz 85653 --version 1
   pylovo-delete classification --version 1
   pylovo-delete networks --plz 85653 --version 1 --yes    # in a script

pylovo-api
----------

.. code-block:: text

   pylovo-api [-h] [--host HOST] [--port PORT] [--root ROOT] [--log-level LEVEL] [--allowed-host HOST]

Starts the headless HTTP API that the GridPlanner UI uses (:doc:`http_api`). It needs the extra
``api``. It runs the other commands as jobs, so it has no generation options of its own.

``--host HOST``, ``--port PORT``
   Interface and port to bind (default ``127.0.0.1`` and ``8765``).
``--root ROOT``
   Project directory with ``config/`` (default: found automatically).
``--log-level LEVEL``
   uvicorn log level (``critical``, ``error``, ``warning``, ``info`` or ``debug``; default
   ``warning``).
``--allowed-host HOST``
   Also accept this ``Host`` header, for example the host name of a reverse proxy (repeatable).

.. code-block:: bash

   uv run --extra api pylovo-api
