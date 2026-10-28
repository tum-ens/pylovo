Development
===========

Contributions are welcome. The workflow (issues, branches, commits, pull requests, reviews and
releases) is described in `CONTRIBUTING.md <https://github.com/tum-ens/pylovo/blob/main/CONTRIBUTING.md>`_.
In short: open an issue, work on a topic branch (``feature/<description>`` or ``fix/<description>``)
from ``dev``, make one commit per topic with a one-line message in the imperative mood, add an
entry to ``CHANGELOG.md`` under *Unreleased* and open a pull request into ``dev``. ``main`` holds
the released versions (tagged ``vX.Y.Z``).

Project layout
--------------

.. code-block:: text

   pylovo/
   |-- config/                   YAML configuration (config_generation_lv.yaml, config_generation_mv.yaml, ...)
   |-- data/                     input data shipped with the repository
   |   |-- municipal_register/   Gemeindeverzeichnis and RegioStaR tables (xlsx)
   |   |-- transformer_data/     Overpass queries and the processed OSM transformers of Bavaria (EPSG 25832)
   |   `-- buildings/, ways/     file-based input (not in git)
   |-- docs/
   |   |-- source/               this documentation (Sphinx)
   |   `-- scripts/              scripts that create the figures
   |-- api/                      headless HTTP API for browser clients and scripts (pylovo-api)
   |-- viewer/                   read-only map viewer (pylovo-viewer): server, static page, tests
   |-- notebook_tutorials/       Jupyter notebooks
   |-- QGIS/                     QGIS project templates
   |-- tests/                    unit tests (no database needed; database tests are opt-in)
   |   `-- db/                   the toy-town test database (builder and build script)
   |-- .github/                  CI workflows (documentation, tests, API contract, image), issue and PR templates
   |-- src/pylovo/               the Python package
   |   |-- cli/                  pylovo-* commands (setup, generate, mv, analyze, classify, import, export, delete)
   |   |-- database/             DatabaseClient, mixins, DatabaseConstructor, table definitions, migrations
   |   |-- infdb/                InfdbClient
   |   |-- data_import/          transformer, DSO, municipal register and building imports
   |   |-- electrical_backend/   backend interface, pandapower and OpenDSS
   |   |-- mv_grid/              MV stage (pylovo-mv)
   |   |-- classification/       grid classification (pylovo-classify)
   |   |-- analysis/             key figures, coverage and border diagnostics, MV validation, power-flow helpers
   |   |-- plotting/             plots and GIS export
   |   |-- ways_preprocessing_functions/   SQL functions installed by the setup
   |   |-- grid_generator.py     GridGenerator
   |   |-- cable_installer.py    CableInstaller
   |   |-- feeder_planning.py    feeder topology and cable sizing
   |   |-- load_editing.py       manual load edits of stored grids (used by pylovo-api)
   |   |-- postcode_border.py, station_voltage.py, version_snapshot.py
   |   |-- config_loader.py      configuration constants
   |   `-- utils.py              load model, logging
   |-- pyproject.toml            package metadata, dependencies, extras, console scripts
   `-- CHANGELOG.md

The wheel carries ``config/`` and the reference data of ``data/`` (municipal register, Overpass queries,
processed transformers) in the folder ``pylovo/_defaults``: ``pyproject.toml`` maps them there with hatch's
``force-include`` and lists them for the sdist, so they exist only once, in the repository. An editable
install reads the checkout directly. A new reference file has to be added to both lists. To check a
build, run ``uv build``, install the wheel into a fresh virtual environment and run
``pylovo-setup --help`` from an empty directory that holds only a ``.env`` (:ref:`install-wheel`).

Code conventions
----------------

* Python 3.12, linted with ruff (``uv run ruff check``; version and rules in ``pyproject.toml``).
* Docstrings in Google style (``Args:``, ``Returns:``, ``Raises:``); they are rendered in the
  :doc:`../api_reference` with napoleon.
* Import configuration values explicitly (``from pylovo.config_loader import VERSION_ID, ...``).
* All SQL of the pipeline lives in the mixins of :mod:`pylovo.database`; table changes go into
  :mod:`pylovo.database.config_table_structure`.
* A change that alters generated grids must be reproducible with a new ``VERSION_ID``; parameters
  that influence the result belong into the version snapshot
  (``PreprocessingMixin._generation_parameters_snapshot``).

Tests
-----

Quick checks without a database:

.. code-block:: bash

   uv run ruff check
   uv run python -m compileall -q src

``uvx pre-commit install`` (once per clone) runs ruff and a few file checks (large files, merge conflict
markers, YAML and TOML syntax; ``.pre-commit-config.yaml``) on the staged files of every commit.

The unit tests in ``tests`` and ``api/tests`` need no database. The HTTP API has an OpenAPI contract
snapshot ``api/openapi.json`` (see ``api/README.md``). On pull requests the CI builds the documentation,
runs the unit tests, runs the database tests on the toy-town test database (below) and checks the contract:

.. code-block:: bash

   uv run --extra api --extra plots pytest
   uv run --extra api python api/scripts/export_openapi.py   # after changing a route

Database tests
~~~~~~~~~~~~~~

The tests that need a database are opt-in: each group runs only when its variable is set and is
skipped with the reason otherwise.

.. list-table::
   :header-rows: 1
   :widths: 40 27 33
   :class: fixed-table

   * - Tests
     - Variable
     - Database
   * - ``tests/test_toy_town.py``: end-to-end smoke test of the generation (reads)
     - ``PYLOVO_TOY_TOWN_DATABASE`` = database name
     - the toy-town test database; the name must be the database pylovo's configuration points to
   * - ``api/tests`` marked ``requires_db``: regions, results, diagnostics, load editing (writes)
     - ``PYLOVO_API_TEST_DATABASE`` = database name
     - the same
   * - ``tests/test_postgis_fixtures.py``: SQL of the postcode border and the left-out buildings
     - ``PYLOVO_FIXTURE_DB`` = libpq connection string
     - an empty database with PostGIS and pgRouting available
   * - ``tests/test_database_review.py``: migrations, locks and schema checks
     - ``PYLOVO_REVIEW_TEST_DSN`` = libpq connection string
     - an empty database; the role needs ``CREATEDB`` and may create PostGIS and pgRouting

**The toy town.** ``tests/db/toy_town.py`` writes a made-up town into the InfDB source relations that
pylovo reads (``basedata.buildings``, ``ways_per_connection``, ``connection_lines``,
``classify_building_use`` and ``opendata.postcodes_germany``): postcode 99001 with a street grid,
apartment blocks, mixed buildings with shops, a school, a plant above the MV-direct threshold,
single-family houses, a LoD2 transformer station and a cycle path, the neighbouring village 99002 and
99003, a postcode without a polygon. The data are synthetic (no geodata files) and lie in the North Sea,
far from real buildings and OSM transformers. ``tests/db/build_test_database.py`` writes the toy town into
an empty database and runs ``pylovo-setup``, ``pylovo-generate`` and ``pylovo-analyze --per-grid`` for three
versions: ``1`` with the ``config/`` of the checkout, ``2`` with the existing transformer positions
(``USE_OPEN_TRANSFORMER_POSITIONS``) and ``3`` residential-only. It takes less than a minute. The API
tests derive their expectations from this database; they write (a manual transformer, load edits that
are undone again), but they can be repeated on the same database.

Locally, with the image of the CI (PostgreSQL 17, PostGIS 3.5, pgRouting 3.8) and GDAL's ``ogr2ogr``
installed (``pylovo-setup`` needs it):

.. code-block:: bash

   docker run -d --name pylovo-test-db -p 127.0.0.1:55432:5432 \
       -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pylovo_test pgrouting/pgrouting:17-3.5-3.8.0
   docker exec pylovo-test-db psql -U postgres -c "CREATE DATABASE pylovo_fixture" -c "CREATE DATABASE pylovo_review"
   export DBNAME=pylovo_test DBUSER=postgres PASSWORD=postgres HOST=127.0.0.1 PORT=55432
   uv run --extra api --extra plots python tests/db/build_test_database.py --database pylovo_test
   PYLOVO_TOY_TOWN_DATABASE=pylovo_test PYLOVO_API_TEST_DATABASE=pylovo_test \
   PYLOVO_FIXTURE_DB="host=127.0.0.1 port=55432 dbname=pylovo_fixture user=postgres password=postgres" \
   PYLOVO_REVIEW_TEST_DSN="host=127.0.0.1 port=55432 dbname=pylovo_review user=postgres password=postgres" \
       uv run --extra api --extra plots pytest tests/test_toy_town.py tests/test_postgis_fixtures.py \
       tests/test_database_review.py api/tests
   docker rm -f pylovo-test-db   # start again from scratch

pylovo reads ``.env`` with ``override=True``: a ``.env`` in the checkout replaces the exported variables.
Then put the test database into ``.env`` instead (and switch it back afterwards). The build script refuses
to run unless ``--database`` names the database the configuration points to, and it refuses a database
that already has the ``pylovo`` or the ``basedata`` schema; drop and create the database to build it again.

For behaviour-preserving changes of the generation, generate the same postcode before and after the change
in a disposable database and compare ``grid_result``, ``buildings_result``, ``lines_result`` and the
``pandapower_*`` tables; generation is deterministic for a fixed configuration.
``pylovo-setup reset --database NAME`` drops the pylovo schema of a database.

Building the documentation
--------------------------

.. code-block:: bash

   uv sync --extra docs
   uv run sphinx-build -W --keep-going -b html docs/source docs/build/html

The build needs neither a database nor ``.env``: the API reference is generated by sphinx-autoapi,
which parses ``src/pylovo`` without importing it. ``-W`` turns warnings into errors, as on Read the
Docs (``.readthedocs.yaml``, requirements in ``docs/requirements.txt``). The HTML output in
``docs/build`` is not committed.

Figures
~~~~~~~

The figures in ``docs/source/images`` are created by the scripts in ``docs/scripts`` and committed.
Run them from the repository root with the ``plots`` extra installed:

.. list-table::
   :header-rows: 1
   :widths: 36 18 46
   :class: fixed-table

   * - Script
     - Needs a database
     - Output
   * - ``plot_diagrams.py``
     - no
     - ``images/diagrams/pipeline``, ``architecture`` (SVG and PNG)
   * - ``plot_database_diagram.py``
     - no
     - ``images/diagrams/database_schema`` and the generated page
       ``concepts/database_tables.rst``; rerun it after changing the table definitions or the
       constraints of a migration
   * - ``make_demo_versions.py``
     - yes (writes)
     - generates version ``VERSION_ID`` and the demo versions ``docs_bf`` and ``docs_km`` for
       PLZ 85653 from temporary configuration copies
   * - ``plot_generation_steps.py``
     - yes
     - ``images/generation/*`` (step-by-step maps)
   * - ``plot_analysis.py``
     - yes
     - ``images/analysis/*`` (charts of stored results)
   * - ``plot_pylovo_examples.py``
     - yes, and internet
     - ``images/plotting/*`` (made with pylovo's own plotting functions)

The database-backed scripts expect a pylovo database with the demo region PLZ 85653 (Aying): an
OpenStreetMap-derived extract (ODbL) in the InfDB-shaped schemas ``basedata`` and ``opendata``,
a completed ``pylovo-setup`` and the versions created by ``make_demo_versions.py``. They only read
from the database, except ``make_demo_versions.py``. Maps derived from these data must carry the
attribution "© OpenStreetMap contributors, ODbL". The demo extract is not part of the repository:
the committed figures are the reference, and regenerating them needs a database with the demo region.
