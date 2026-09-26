Installation
============

pylovo is a Python package with command-line tools. It needs a PostgreSQL database with the
PostGIS and pgRouting extensions and, in the recommended setup, the InfDB input data in the same
database.

Requirements
------------

.. list-table::
   :header-rows: 1
   :widths: 28 72
   :class: fixed-table

   * - Component
     - Notes
   * - Operating system
     - Linux (Ubuntu) or Windows with WSL2. The commands below assume a Unix shell.
   * - Python
     - 3.12 or newer (``requires-python = ">=3.12"``), managed with
       `uv <https://docs.astral.sh/uv/>`_.
   * - PostgreSQL
     - With the extensions `PostGIS <https://postgis.net/>`_ and
       `pgRouting <https://pgrouting.org/>`_ installed on the server. pylovo uses
       ``pgr_extractVertices``, ``pgr_dijkstra``, ``pgr_dijkstraCost`` and
       ``pgr_dijkstraCostMatrix``. The development sandbox runs PostgreSQL 17, PostGIS 3.6 and
       pgRouting 4.0.
   * - GDAL (``ogr2ogr``)
     - Required by ``pylovo-setup``: the transformer GeoJSON is loaded with ``ogr2ogr``, and so
       are building shapefiles in the file-based mode. On Ubuntu: ``sudo apt install gdal-bin``.
   * - InfDB (recommended)
     - Provides buildings, streets and postcode polygons, see :doc:`../user_guide/input_data`.
       Without InfDB you provide these data as files (``USE_INFDB=False``).
   * - Internet access
     - Only for downloading transformers from the Overpass API and map tiles for plots.

Install pylovo
--------------

Install uv if you do not have it yet, clone the repository and create the environment:

.. code-block:: bash

   curl -LsSf https://astral.sh/uv/install.sh | sh     # Linux / macOS / WSL
   git clone https://github.com/tum-ens/pylovo.git
   cd pylovo
   uv sync

``uv sync`` creates ``.venv`` with pylovo installed in editable mode and all ``pylovo-*``
commands. Run them with ``uv run pylovo-...`` or activate the environment first
(``source .venv/bin/activate``).

Optional dependency groups (extras):

.. list-table::
   :header-rows: 1
   :widths: 22 45 33
   :class: fixed-table

   * - Extra
     - Adds
     - Needed for
   * - ``plots``
     - matplotlib, plotly, seaborn, contextily, ipywidgets, nbformat
     - :mod:`pylovo.plotting`, the tutorial notebooks
   * - ``docs``
     - Sphinx, furo, sphinx-autoapi, sphinx-copybutton, sphinx-design
     - Building this documentation
   * - ``api``
     - FastAPI, uvicorn, python-multipart, httpx
     - ``pylovo-api`` (:doc:`../user_guide/http_api`)

.. code-block:: bash

   uv sync --extra plots            # one extra
   uv sync --all-extras             # everything

.. note::

   pylovo reads ``config/*.yaml`` from the current working directory and writes logs to
   ``log/`` and downloaded data to ``data/`` relative to it. Run all commands from the
   repository root. See :ref:`config-file-lookup` for the other search locations.

Without uv, a virtual environment with pip works as well:

.. code-block:: bash

   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[plots]"

Prepare the database
--------------------

pylovo writes into the schema ``pylovo`` of one PostgreSQL database. With InfDB this is the
InfDB database itself: pylovo reads the InfDB schemas (``basedata``, ``opendata``) and writes
its own schema next to them, using the same connection settings.

#. Create or choose the database and a user that may create schemas and tables in it.
#. Make sure PostGIS and pgRouting are available. ``pylovo-setup`` creates missing extensions in
   the schema ``public``; you can also create them yourself beforehand:

   .. code-block:: sql

      CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public;
      CREATE EXTENSION IF NOT EXISTS pgrouting SCHEMA public;

   Never install extensions in the schema ``pylovo``: the setup drops that schema with
   ``CASCADE``. Older pylovo versions created pgRouting there; the setup now drops it with the
   schema and recreates it in ``public``. If PostGIS itself lives in ``pylovo``, the setup
   refuses to run, because dropping it would delete every geometry column in the database.

#. Write the connection settings into ``.env`` (next section).

Details and the tables created by the setup are described in
:doc:`../user_guide/database_setup`.

Set up InfDB
------------

`InfDB <https://github.com/tum-ens/InfDB>`_ is a dockerised infrastructure database developed at
TUM ENS that harmonises LoD2 buildings, Zensus 2022 statistics, basemap streets and postcode
polygons. Follow the `InfDB documentation <https://tum-ens.github.io/InfDB/usage/>`_ (release
3.0.0 or newer). In short:

#. Clone InfDB with its submodules:
   ``git clone --recurse-submodules git@github.com:tum-ens/InfDB.git``.
#. Start the database service: ``bash infdb.sh start``.
#. Configure the import in ``configs/config-infdb-import.yml`` and run ``bash infdb.sh import``.
#. Run the pylovo preprocessing tools for every municipality (AGS) you need:
   ``bash tools/tools.sh -p basedata-buildings <AGS>`` and
   ``bash tools/tools.sh -p basedata-ways <AGS>``.

These InfDB commands are maintained in the InfDB repository; check its documentation if they
have changed. The tables pylovo reads are listed in :ref:`infdb-tables`.

Configure the connection
------------------------

Copy ``.env.example`` to ``.env`` in the repository root and enter the database settings:

.. code-block:: bash

   cp .env.example .env

.. code-block:: ini

   USE_INFDB=True
   DBNAME="infdb"
   DBUSER="infdb_user"
   HOST="localhost"
   PORT="54321"
   PASSWORD="your_password"
   INFDB_SOURCE_SCHEMA="basedata"
   INFDB_OPENDATA_SCHEMA="opendata"
   TARGET_EPSG=25832

All variables are explained in :ref:`env-variables`. Then continue with the
:doc:`quickstart`.

Tips
----

* On Windows, run pylovo inside WSL2; ``ogr2ogr`` and the shell commands assume Linux.
* Keep ``.venv``, ``.env`` and ``log/`` out of version control (they are in ``.gitignore``).
* ``.env`` wins over environment variables of the same name (``load_dotenv(..., override=True)``).
  Check it first when pylovo connects to an unexpected database.
