Quickstart
==========

First install pylovo as in :doc:`installation`. The recommended input is `InfDB
<https://github.com/tum-ens/InfDB>`_, a PostgreSQL framework that prepares buildings, streets
and postcode areas from open data. Set up InfDB and its pylovo input tables first, then add the
``pylovo`` schema to **the same database**. See :doc:`../user_guide/database_setup` for details.

1. Configure the connection
---------------------------

From the pylovo repository root:

.. code-block:: bash

   cp .env.example .env

Set ``USE_INFDB=True`` and enter your InfDB connection values in ``.env``. For file-based input,
see :ref:`file-based-input`. All settings are listed under :ref:`env-variables`.

2. Review the configuration
---------------------------

Check ``VERSION_ID`` and ``VERSION_COMMENT`` in ``config/config_generation_lv.yaml``. Use a new
number whenever you change generation parameters (:ref:`versions`).

3. Set up the pylovo schema
---------------------------

Run setup to create or migrate the schema without deleting existing grids:

.. code-block:: bash

   uv run pylovo-setup

Only the explicit ``pylovo-setup reset --database NAME`` command deletes the
schema. See :doc:`../user_guide/database_setup` for prerequisites.

4. Generate grids
-----------------

.. code-block:: bash

   uv run pylovo-generate --plz 80803

More regions, municipalities and parallel runs are covered in
:doc:`../user_guide/region_selection` and :doc:`../user_guide/generating_grids`.

5. Look at the results
----------------------

The read-only map viewer shows the database of your ``.env`` as a map in the browser
(:doc:`../user_guide/viewer`):

.. code-block:: bash

   uv run --extra viewer pylovo-viewer       # http://127.0.0.1:8770/, opens the browser

For a guided look in Python, open ``notebook_tutorials/01_tour.ipynb`` (:doc:`/tutorials/01_tour`) and set its
``plz`` parameter to ``80803``: it reads the grids from the database of your ``.env``, maps them and shows the
voltage profile of one grid. The other notebooks of the learning path are listed in :doc:`tutorials`. Install the
notebook environment with ``uv sync --extra notebooks`` if needed.

Next steps
----------

* Understand the method: :doc:`../concepts/pipeline`.
* Use existing transformer positions: :doc:`../user_guide/transformer_data`.
* Explore applications: :doc:`../user_guide/applications`.
