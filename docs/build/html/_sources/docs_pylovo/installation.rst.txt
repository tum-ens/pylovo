Installation
****************

Prerequisites
=============

* Environment: We strongly recommend setting up virtual environments for pylovo (and the urbs optimizer if the gui shall be used).
* EduVPN_: If you are working from your own machine you will need a VPN to connect to the MWN network. A connection to the network is necessary for accessing the database server.

.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB

Install package
===============

| pylovo is developed as a Python package and can be installed with pip, ideally by using a virtual environment.
| Start by cloning the repository from Github to a directory of your choice. The following command pulls the repository as well as the urbs submodule

::

    git clone --recurse-submodules --remote-submodules https://github.com/tum-ens/pylovo.git

Next we set up our virtual environments. If you are able to use python with version from the terminal such as
``python3.9``you can set up a virtual environment with that version following this command 
::

    python3.9 -m venv pylovovenv

You can also use virtualenv as an alternative to use the command:
::
    
    virtualenv pylovovenv --python=python3.9

You can then activate the environments by 
::
    # to activate a virtual environment
    source pylovovenv/bin/activate
    # to deactivate a virtual environment:
    deactivate

Finally, after activating the virtual environment that we crated. We install pylovo from the local repository. 
There are additional setup options for different user groups. Such as developers, which includes packages for documentation.
Gui users, which includes packages for the web interface and testing which includes packages for testing the code.
Both installation option steps are described below.
See inside ``setup.py`` for more detailed information and which packages to select when installing.

User
----

::

    # navigate to pylovo main code folder
    cd path/to/git_repo/pylovo
    pip install -e .

With this setup you are already ready to use the main constructor to create the tables and load the data into the database. 
Then you can use the grid generator scripts to create grids.

Data
----
    You should contact the responsible person (see author_email in setup.py) to get the raw data and place them in the correct folders.
    The essential files are:
        * building data for residential and other sectors: res.shp and oth.shp
        * ways data: public_2po_4pgr.sql
        * transformer data (optional): substations.geojson
    For the transformer files you need to do additional configuration in ``raw_data/proccess_trafos.py``.

Developer
---------

::

    # navigate to pylovo main code folder
    cd path/to/git_repo/pylovo
    pip install -e ."[dev]"

to use the gui, make it ."[gui]" or add it to the list like ."[dev, gui]".

If you have included the gui in the setup, with that pylovo should be ready to go! You can test whether everything went correctly by navigating 
to the IDP_Maptool_Flask folder and running the following commands  
::
    # navigate to maptool folder and start web server
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    flask --app maptool --debug run

This should start the flask server and allow you to open the GUI by navigating to http:127.0.0.1:5000 in a web browser of your choice


Advanced installation - Database construction
===============================================

| Follow the instructions below, only if you want to create a new database for pylovo. 
  Make sure you have the required raw data.

| Make sure that you have created the database with the proper config of dbname, user, password, host, port in the config_data.py file.

Install postgresql on linux
============================

| To install postgreSQL to your machine see the editions from here
| https://www.postgresql.org/download/

Access Database
===============

Outside the server (from ssh client a.k.a. your own computer)
--------------------------------------------------------------

| To gain access to the pylovo database from your own machine you will need to request a username and password from the ENS chair.
| If you are working from your own machine you will also need to utilize a VPN to connect to the MWN network, 
  which us a prerequisite for connecting to the database server. We recommend using EduVPN_ for this purpose. 
  Follow the instructions in the link to set up a connection. You will need to use EduVPN to connect to the profile 
  **Technische Universität München via LRZ-VPN**.

| Once you have connected via EduVPN the tool will be able to connect to the database automatically

Create SQL functions
====================

Prewritten SQL functions must be created for once, this will be done by the ``main_constructor``, so this step can be skipped to use that script. 
Constructor uses the dump_functions file in syngrid folder, for a detailed view on this it can be checked from that path. 
If you had a problem or if you prefer to do so run the file syngrid/dump_functions.sql:

::

    psql -d syngrid_db -a -f "syngrid/dump_functions.sql"

Load raw data to the database
=============================

| pylovo requires the correct table structure and input data to already be loaded into the database. 
  Make sure that you have the raw data files and paths configured in config_data.py
| To create the ``public_2po_4pgr`` table you need to put that file into raw_data folder before running the constructor.
| Make sure again you have created the database, configured with the proper dbname, user, password, host, port in the config_data.py file including the permissions.
| Afterwards, being in the root folder of the repository, the ETL process can be executed as:

::

    python executable_scripts/main_constructor.py

Input data model
====================================================================

The minimum data model is described below:

* res
* oth
* betriebsmittel
* postcode
* ways 
* consumer_categories
* transformers

Preprocess ways from OSM data
------------------------------
| Use the following steps if you want to add more ways in addition to the default Bavarian ways that
| are provided with the public_2po_4pgr.sql file and are set from the ``main_constructor.py``
#. Connect to database via localhost
#. Download the OSM-streetnets you require from http://download.geofabrik.de/
#. Download Osm2po-5.3.6 from https://osm2po.de/releases/ 
    * !!!Has to be version 5.3.6, this guide does not work with later versions!!!
#. Extract the downloaded zip file
#. Open the osm2po.config file in the extracted folder and make sure that all of the following lines are set correctly (lines starting with # are commented out)
    * Line 59:          tilesize=x
    * Line 190:         comment out “.default.wtr.finalMask = car” 
    * Line 222-231: 	make sure that only ferry is commented out
    * Line 341:         line must not be commented out, otherwise sql file will not be generated
#. Open terminal and navigate to folder Osm2po-5.3.6. Execute the following command:
    * java -Xmx1g -jar osm2po-core-5.3.6-signed.jar prefix=public "C:/Users/path/to/osm/file/osm_file_name.pbf"
    * change „C:/Users/path/to/osm/file/“ with path to geofabrik file you downloaded earlier
    * change „osm_file_name.pbf“ to name of the geofabrik file
#. Execute pylovo's main_constructor.py
    * make sure the ways_to_db method has been uncommented in main_constructor.py
    * the ways in the 2po_4pgr table will be inserted into the ways table and can now be used by pylovo

Further Input Data
===================

.. toctree::
    :maxdepth: 2

    municipal_register/municipal_register
    osm_trafos

.. _virtual environment: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment
.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB