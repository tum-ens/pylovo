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
     - 3.12, 3.13 or 3.14 (``requires-python = ">=3.12,<3.15"``: some dependencies have no wheels for
       3.15 yet), managed with `uv <https://docs.astral.sh/uv/>`_.
   * - PostgreSQL
     - PostgreSQL 15 or newer, with the extensions `PostGIS <https://postgis.net/>`_ and
       `pgRouting <https://pgrouting.org/>`_ 3.8 or newer installed on the server. The schema uses
       foreign keys with ``ON DELETE SET NULL (column)``, which older servers reject, so
       ``pylovo-setup`` checks the server version before it changes anything. pylovo calls
       ``pgr_extractVertices``, ``pgr_dijkstra``, ``pgr_dijkstraCost``, ``pgr_dijkstraCostMatrix``
       and ``pgr_connectedComponents``. It is tested with PostgreSQL 17.10, PostGIS 3.6.4 and
       pgRouting 4.0.1.
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
     - matplotlib, plotly, seaborn, contextily, ipywidgets, nbformat, igraph
     - :mod:`pylovo.plotting` and ``pylovo-classify`` (:doc:`../user_guide/classification`)
   * - ``notebooks``
     - The ``plots`` extra, ipykernel, nbclient, jinja2
     - The tutorial notebooks (:doc:`tutorials`)
   * - ``docs``
     - Sphinx, furo, sphinx-autoapi, sphinx-copybutton, sphinx-design
     - Building this documentation
   * - ``api``
     - FastAPI, uvicorn, python-multipart, httpx
     - ``pylovo-api`` (:doc:`../user_guide/http_api`), the HTTP API for browser clients and scripts
   * - ``viewer``
     - FastAPI, uvicorn, httpx
     - ``pylovo-viewer`` (:doc:`../user_guide/viewer`), the read-only map viewer
   * - ``opendss``
     - altdss (OpenDSS via DSS-Extensions)
     - ``ELECTRICAL_BACKEND: opendss`` (:doc:`../concepts/electrical_backends`)
   * - ``legacy-ui``
     - Flask
     - The browser map ``pylovo-import transformers-ui`` for checking and editing transformer positions

.. code-block:: bash

   uv sync --extra plots            # one extra
   uv sync --all-extras             # everything

.. note::

   pylovo reads ``config/*.yaml`` from the current working directory and writes logs to
   ``log/`` relative to it. Input data and downloaded data are in the data directory (``data/``,
   or ``PYLOVO_DATA_DIR``). Run all commands from the repository root (or from the project
   directory of a pip installation, see below). See :ref:`config-file-lookup` for the other search locations.

Without uv, a virtual environment with pip works as well:

.. code-block:: bash

   python3.12 -m venv .venv      # or python3.13
   source .venv/bin/activate
   pip install -e ".[plots]"

.. _install-wheel:

Install without a checkout (pip)
--------------------------------

The wheel runs outside a checkout. Install it into a virtual environment, with the extras you need
(see the table above):

.. code-block:: bash

   python3.12 -m venv .venv      # or python3.13
   source .venv/bin/activate
   pip install pylovo            # or: pip install "pylovo[plots,api]"

Then work in a project directory of your own: put the ``.env`` (:ref:`env-variables`) there and run the
``pylovo-*`` commands from it. The wheel ships the default configuration and the read-only reference
data that ``pylovo-setup`` needs, in the folder ``pylovo/_defaults`` of the package. They are
the last fallback of the lookups, so nothing has to be copied to get started:

* ``config/``: the five default YAML files (:ref:`config-file-lookup`),
* ``data/``: the municipal register tables (Gemeindeverzeichnis, RegioStaR), the Overpass queries of
  the transformer import and the processed OpenStreetMap transformers of Bavaria
  (:doc:`../user_guide/transformer_data`). A file of the data directory (``./data``, or
  ``PYLOVO_DATA_DIR``) wins over the packaged copy of the same name.

The wheel contains the same files as ``config/`` and ``data/`` of the repository. Everything pylovo writes
goes to the project directory, never into the package: ``log/``, ``results/``, the downloaded and
processed transformers and your own input data in ``data/``. To change a parameter, copy the default
configuration into ``./config`` and edit the copies, which take precedence from then on:

.. code-block:: bash

   python -c "import importlib.resources as r, shutil; shutil.copytree(r.files('pylovo') / '_defaults' / 'config', 'config')"

``pylovo-api`` needs such a project directory with ``config/`` (``--root``, :doc:`../user_guide/http_api`).
The ``.env.example`` of the repository lists all variables; the wheel does not contain it. An editable
install (``pip install -e .``, ``uv sync``) has no ``pylovo/_defaults`` folder and uses ``config/`` and
``data/`` of the checkout.

Prepare the database
--------------------

pylovo writes into the schema ``pylovo`` of one PostgreSQL database. With InfDB this is the
InfDB database itself: pylovo reads the InfDB schemas (``basedata``, ``opendata``) and writes
its own schema next to them, using the same connection settings.

#. Create or choose the database (PostgreSQL 15 or newer) and a user that may create schemas and
   tables in it.
#. Make sure PostGIS and pgRouting (3.8 or newer) are available. ``pylovo-setup`` creates missing extensions in
   the schema ``public``; you can also create them yourself beforehand:

   .. code-block:: sql

      CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public;
      CREATE EXTENSION IF NOT EXISTS pgrouting SCHEMA public;

   Keep extensions outside ``pylovo``. The explicit reset command refuses to
   drop a schema that owns an extension, including PostGIS or pgRouting.

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

Copy ``.env.example`` to ``.env`` in the repository root (for a pip installation: into your project
directory, see :ref:`env-variables` for where pylovo looks for it) and enter the database settings:

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
