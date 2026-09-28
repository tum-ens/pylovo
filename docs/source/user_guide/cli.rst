Command-line reference
======================

pylovo installs these commands (``[project.scripts]`` in ``pyproject.toml``). Run them from the
repository root, either with ``uv run <command>`` or in the activated environment. All of them
read ``.env`` and ``config/*.yaml`` at start (:doc:`configuration`) and return a non-zero exit code
on errors.

.. list-table::
   :header-rows: 1
   :widths: 25 75
   :class: fixed-table

   * - Command
     - Purpose
   * - ``pylovo-setup``
     - Create the schema ``pylovo`` with reference data (**drops existing results**).
   * - ``pylovo-generate``
     - Generate grids for postcodes or municipalities.
   * - ``pylovo-analyze``
     - Compute postcode and per-grid key figures.
   * - ``pylovo-import``
     - Import transformer candidates (OSM, DSO CSV).
   * - ``pylovo-export``
     - Export grid buses and lines as CSV.
   * - ``pylovo-delete``
     - Delete versions, postcodes or transformer candidates.

pylovo-setup
------------

.. code-block:: text

   pylovo-setup [-h] [{setup,reset}] [--database DATABASE] [--yes]

With no command, creates a new schema or migrates an existing one in place.
Only ``reset --database NAME`` deletes the PyLovo schema; the name must match
``DBNAME`` in ``.env``. Reset asks you to type the database name unless
``--yes`` is supplied. The browser API uses this explicit reset command after
its own typed confirmation. See :doc:`database_setup`.

pylovo-generate
---------------

.. code-block:: text

   pylovo-generate [-h] [--plz PLZ [PLZ ...]] [--ags AGS [AGS ...]] [--parallel | --no-parallel]

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

.. code-block:: bash

   pylovo-generate --plz 80803
   pylovo-generate --plz 80803 80802 80801 --no-parallel
   pylovo-generate --ags 09162000

pylovo-analyze
--------------

.. code-block:: text

   pylovo-analyze [-h] --plz PLZ [--per-grid] [--all]

Computes key figures of the grids of one postcode in the active version
(:doc:`analysing_grids`). Without further options only the postcode key figures
(``plz_parameters``) are computed.

``--plz PLZ``
   Postcode to analyse (required).
``--per-grid``
   Compute the key figures of every grid (``clustering_parameters``); the postcode key figures
   must exist already.
``--all``
   Postcode key figures followed by the per-grid key figures.

pylovo-import
-------------

.. code-block:: text

   pylovo-import [-h] {transformers-osm,transformers-dso-csv} ...

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

pylovo-delete
-------------

.. code-block:: text

   pylovo-delete [-h] [--version VERSION_ID [VERSION_ID ...]] {networks,version,transformers} ...

Deletes data from the database. Without arguments the help is shown. Deleting versions or
postcodes cascades to all dependent result tables and refreshes the materialised views.

``pylovo-delete --version VERSION_ID [VERSION_ID ...]``
   Delete one or more versions with all their grids in all postcodes, their equipment data and
   parameter snapshots. Unknown versions are reported and nothing is deleted. Cannot be combined
   with a subcommand.

``pylovo-delete version --version VERSION_ID [VERSION_ID ...]``
   The same as a subcommand.

``pylovo-delete networks --plz PLZ --version VERSION``
   Delete the grids of one postcode in one version (the version itself is kept).

``pylovo-delete transformers``
   Empty ``pylovo.transformers``. Refuses while generated grids still reference transformers
   (see :doc:`transformer_data`).

.. code-block:: bash

   pylovo-delete --version docs_bf docs_km
   pylovo-delete networks --plz 85653 --version 1
