Tool Usage
**********

Database connection
=============================
For connecting to the virtual machine,
see [Advanced Installation/Outside the VM][outside-the-vm-from-ssh-client-aka-your-own-computer]

The database connections parameters can be changed in ``config_data.py``

If you will use your own database you need to create a database and a user for the database
and use the ``config_data.py`` file to set the connection parameters.

Configuration
=============
If you want to manage the input data,
see [Advanced Installation/Load raw data to the database][load-raw-data-to-the-database]

To change the **parameters** and the **version**, edit the file ``config_version.py``.

Run
===
The grid generation scripts such as ``grid_generation_for_single_plz.py`` demonstrates how to run the grid generation tool.
If the grids for the given region and version are already generated, the code will terminate with an error statement.
There are other examples in the `executable_scripts` directory that you can use to manipulate the database by changing PLZ's
from the scripts. 

.. warning::
    Before running the scripts make sure you configured the IP of the database that you want to work for. 
    If you would like to connect to the remote db get your .env file from the responsible person and put it in the root directory of the project.
    If you are going to work on a local database or you will create your own db set the config from the `config_data.py` file.

Result inspection
==================
Download [QGIS]. Go to `QGIS` directory in pylovo. Open QGIS file.
Database connection settings have to be set to the database that is used by pylovo.
Initial data (ways, buildings and transformers)
as well as the networks (transformers, cables, buildings) can be visualised.
Go to QGIS visualisation docu for more details

Tutorials / Examples
=====================
Individual networks can also be visualised as explained in the `examples` directory.
In the examples notebooks you will learn more about:

* the objects / elements the LV grids are made up of
* the pandapower networks that are used to store the LV grids
* graph representation of the networks
* and parameter visualisation options

[QGIS]: https://www.qgis.org/de/site/forusers/download.html
Refer to the Jupyter notebooks and other analysis tools?