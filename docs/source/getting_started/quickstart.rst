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

Check ``VERSION_ID`` and ``VERSION_COMMENT`` in ``config/config_generation.yaml``. Use a new
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

Open the :download:`basic-grid notebook
<../../../notebook_tutorials/grid_generation/0_basic_grid_generation.ipynb>` for a guided look
at the stored grids and one pandapower network. For maps and statistics, open the
:download:`statistics notebook
<../../../notebook_tutorials/grid_generation/4_basic_plotting.ipynb>` or use the GridPlanner
browser UI on the :doc:`../user_guide/http_api`. The notebooks use the
database configured in ``.env``; change their ``plz`` parameter to ``80803``. Install the
notebook environment with ``uv sync --extra notebooks`` if needed (:doc:`tutorials`).

Next steps
----------

* Understand the method: :doc:`../concepts/pipeline`.
* Use existing transformer positions: :doc:`../user_guide/transformer_data`.
* Explore applications: :doc:`../user_guide/applications`.
