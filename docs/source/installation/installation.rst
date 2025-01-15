Installation
************

Setup Repository and Environment
==============
| Start by cloning the repository from GitHub into a directory of your choice.
| We recommend setting up a virtual environment to avoid conflicts with other packages. If you have installed the required Python version (Python3.12) on your system, you can create a virtual environment with the following command:

::

    python3.12 -m venv pylovo-env-3-12

Alternatively, you can use `pyenv <https://github.com/pyenv/pyenv>`_ or `virtualenv <https://virtualenv.pypa.io/en/latest/index.html#>`_ to use Python 3.12.

Next, you can activate the environment with:

::

    Linux: source pylovo-env/bin/activate
    Windows: pylovo-env/Scripts/activate

After activating the virtual environment, install the required packages:

::

    pip install -r requirements.txt

Database Configuration
=========================================
A. External users: Create your database
----------------------------------------
- Install PostgreSQL on your machine.
- Create your database with the appropriate configuration (dbname, user, password, host, port).
- Create a ``.env`` file in the root directory of the repository with these configurations or adjust the connections parameters in the ``config_data.py``.
- Your configurations might might look like this:

::

    DBNAME = "pylovo_db"
    USER = "postgres"
    HOST = "localhost"
    PORT = "5432"
    PASSWORD = "yourpassword"

- `GDAL <https://gdal.org/en/stable/index.html>`_ is required for some geo-transformations. Ensure it is installed on your system. (e.g. for Ubuntu 24.04: ``sudo apt install gdal-bin``).

Input data
~~~~~~~~~~
The minimum data requirements for the ``raw_data`` directory are described below.
Some larger data files, which you can request from the maintainers at ENS, are not included in the repository due to their size (an online source will be available soon):

- Building shapefiles (``res_<ags> and oth_<ags>``) in the ``buildings`` directory
- Street SQL data (``ways_public_2po_4pgr.sql``) in the ``ways`` directory
- The postcode data (``postcode.csv``) for Germany including the geometries in the ``raw_data`` directory

Other required data files that are already included in the raw_data directory of the repository:

- ``transformer_data/substations_bayern.geojson``
- ``consumer_categories.csv``
- ``equipment_data.csv``


Load raw data to the database
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The ``main_constructor.py`` script initializes and populates the **pylovo** database. Run:

::

    python3.12 executable_scripts/main_constructor.py

.. warning::

   This script should only be used when a new database is required, as it will overwrite existing tables in your database. Use caution when running this script in production environments.


Below is a detailed outline of its functionality:

1. **Database Initialization**
   A new database is created and initialized using the class ``SyngridDatabaseConstructor``. This includes the addition of required PostGIS and pgRouting extensions to your database.

2. **Importing Raw Data**

   - CSV files containing raw data (specified by a predefined file list, ``CSV_FILE_LIST``) are imported into the database.
   - The ways data is processed and imported into the database.
   - The imported ways data is converted into a format suitable for further analysis (e.g., transforming OSM road network data into custom "ways" tables).

3. **Enhancing the Database with SQL Functions**

   - Essential SQL functions for querying and processing the data are added to the database.
   - This includes utility functions for geospatial data transformations and advanced query support.

4. **Municipal Data Creation**
   The ``create_municipal_register`` function creates a table containing all German municipalities and cities, serving as the foundation for multiple processes:

   - **Grid Generation**: The PLZ code (Postleitzahl) defines areas for grid creation.
   - **Building Dataset Import**: The AGS (Amtlicher Gemeindeschlüssel) is required for linking municipal data with building datasets (see :doc:`../../grid_generation/index`).
   - **Area Classification**: The Regiostar class is necessary for categorizing municipalities, enabling accurate analyses (see :doc:`../../classification/index`).

B. ENS students and employees: Access ENS database
---------------------------------------------------
- The pylovo tool is designed to work with a database hosted on the ENS server. The database is accessible from the ENS network, so you must be connected to the ENS network to access it.
- If you are working from home, you also need to use a VPN to connect to the MWN network—a prerequisite for connecting to the database server. We recommend using EduVPN_. Follow the instructions in the link to set up a connection.

.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB

- To gain access to the pylovo database from your own machine, you will need to request a username and password from the ENS chair.


Additional hints
==============================
.. hint::
    - If you are working on Windows, we recommend using the Windows Subsystem for Linux (WSL) for the setup and to run the scripts.
    - To avoid path conflicts run all scripts from the directory root or setup your IDE running configuration accordingly.
    - As it appears within the repository make sure to exclude your virtual environment directory from version control and from your IDE project.
    - Our default spatial reference system for geographic data is EPSG:3035 (meters - LAEA Europe). OSM-Input data are often given in EPSG:3857 (meters - web maps). The pandapower networks are per default generated in EPSG:4326 (lon/lat - world). Be mindful of these different reference systems.


Explore more materials
==============================

For deeper understanding of the tool and the results you can...

- ...follow the documentation to generateo your first synthetic grid with pylovo :doc:`../../grid_generation/usage/usage`)
- ...go through the jupyter notebook tutorials.
- ...after generating grids open the QGIS file to directly visualize the data from your database with the predefined layouts (see :doc:`../visualisation/qgis/qgis`).
- ...read our publication (see :doc:`../../further_reading`) to understand the methodology in more detail


Optional steps
==============
If you want more control over your input data follow instructions below:

(Optional) Preprocess transformers from OSM data
------------------------------------------------
1. Copy the code from ``substations_bayern_query_overpass.txt`` into the OSM API `overpass turbo <https://overpass-turbo.eu/>`_. The file is in the ``raw_data`` package. Run the script in Overpass Turbo and export the results as a GeoJSON named ``substations_bayern.geojson``, then save it in the ``raw_data`` package.

   The query searches for the keywords "transformer" and "substation" in the area "Bayern." Substations from "Deutsche Bahn" as well as "historic" and "abandoned" substations are excluded. This query yields around 22,000 results. More information about transformer locations can be found on `OpenInfrastructureMap <https://openinframap.org/#12.73/48.18894/11.58542/>`_.

2. A second Overpass query is to be performed. Repeat the previous steps for ``shopping_mall_query_overpass.txt``.

   The query searches for all places tagged with keywords indicating that nearby transformers do not belong to the LV grid (for example, "shopping malls" are likely directly connected to the MV grid). Other filters include land use related to the oil industry (e.g., refineries), power plants (e.g., solar fields), military training areas, landuse "rail," landuse "education," and large surface parking.

   Make any changes to the Overpass queries that you see fit.

3. Open the ``process_trafos.py`` script. At the top, there are constants you can change: ``AREA_THRESHOLD``, ``MIN_DISTANCE_BETWEEN_TRAFOS``, ``VOLTAGE_THRESHOLD``, and ``EPSG``.

   - The script imports the GeoJSON files from steps 1 and 2, transforming the geodata according to the EPSG projection to calculate distances and areas. Transformers can be points or polygons.
   - First, any transformers that overlap are deleted. This often occurs in "Umspannwerke" (HV transformers) where multiple tags exist for the same location.
   - Second, all transformers larger than the ``AREA_THRESHOLD`` are deleted. LV transformers are either points or have smaller dimensions.


(Optional) Preprocess ways from OSM data
---------------------------------------
Use the following steps if you want to add more ways in addition to the default Bavarian ways provided with the ``public_2po_4pgr.sql`` file and set from ``main_constructor.py``:

1. Connect to the database via localhost.
2. Download the OSM street networks you require from `http://download.geofabrik.de/ <http://download.geofabrik.de/>`_.
3. Download Osm2po-5.3.6 from `https://osm2po.de/releases/ <https://osm2po.de/releases/>`_.

   .. note::
      It **must** be version 5.3.6. This guide does not work with later versions.

4. Extract the downloaded zip file.
5. Open the ``osm2po.config`` file in the extracted folder and ensure the following lines are set correctly (lines starting with ``#`` are commented out):
   - Line 59: ``tilesize=x``
   - Line 190: comment out ``.default.wtr.finalMask = car``
   - Lines 222-231: ensure only ``ferry`` is commented out
   - Line 341: must **not** be commented out, or the SQL file will not be generated.

6. Open a terminal and navigate to the ``Osm2po-5.3.6`` folder. Execute the following command:

::

    java -Xmx1g -jar osm2po-core-5.3.6-signed.jar prefix=public "C:/Users/path/to/osm/file/osm_file_name.pbf"

   - Replace ``C:/Users/path/to/osm/file/`` with the path to the Geofabrik file you downloaded earlier.
   - Replace ``osm_file_name.pbf`` with the name of the Geofabrik file.

7. Execute pylovo's ``main_constructor.py``.
   - Ensure the ``ways_to_db`` method is uncommented in ``main_constructor.py``.
   - The ways in the ``2po_4pgr`` table will be inserted into the ``ways`` table and can now be used by pylovo.


(Optional) Adjust SQL functions
-------------------------------
Prewritten SQL functions are created by the ``main_constructor`` script, so you can skip this step if you are using that script. The constructor uses the ``dump_functions.sql`` file in the ``pylovo`` folder. If you encounter issues or want to add SQL functions, edit and run the following file:

::

    psql -d pylovo_db -a -f "pylovo/dump_functions.sql"
