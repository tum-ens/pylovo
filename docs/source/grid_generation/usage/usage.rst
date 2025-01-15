Generate Synthetic Grids
**********

Configuration
=============
| To allow distinction for different parameters you can define grids with different version entries in ``config_version.py``.
| Please enter your VERSION_ID and your VERSION_COMMENT in the ``config_version.py`` file.
| If you don't want to change any parameters you can start with the current base version configurations.

Create your first grid
=========================================
After defining your plz in the ``grid_generation_for_single_plz.py`` script, you can run:

::

    python3.12 executable_scripts/grid_generation_for_single_plz.py

If the grids for the given region and version are already generated, the code will terminate.

Apart from this you can:

- create grids for multiple PLZs or all PLZ within an AGS region.
- activate the flags to analyze the grid and visualize some basic results.
- export the grid data as csv.
- delete specified grids/versions.

.. note::
    - Before running the scripts make sure you followed all steps descripbed in the :doc:`installation/installation` section.
    - If you are from TUM and would like to connect to the remote db get your .env file from a ENS pylovo maintainer and save it in the root directory of the project.

Result inspection with QGIS
==================
- Download `QGIS <https://www.qgis.org/download/>`_. Go to the `QGIS` directory in pylovo and open the QGIS file.
- The database connection settings have to be set to the database that is used by pylovo.
- Initial data (ways, buildings and transformers) as well as the networks (transformers, cables, buildings) can be visualised.
- See :doc:`../../visualisation/qgis/qgis` visualisation docu for more details

Tutorials / Examples
=====================
In the `notebook_tutorials.py` you will learn more about the following topics:

* visualizing individual networks
* the objects / elements the LV grids are made up of
* the pandapower networks that are used to store the LV grids
* graph representation of the networks
* and parameter visualisation options