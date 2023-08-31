Installation
****************

Install package
===============

pylovo is developed as a Python package and can be installed with
`pip`, ideally by using a `virtual environment`_. Required Python dependencies are installed in the background. Open up a
terminal, clone and install pylovo with::

    git clone git@gitlab.lrz.de:tum-ens/pylovo.git


Install the package with pip editable from local repository, developer option installs additional packages:

User
----

::

    cd pylovo
    pip install -e .


Developer
---------

::

    sh
    cd pylovo
    pip install -e .[dev]



Advanced installation - Database construction
===============================================

| Follow the instructions below, only if you want to create a new database for pylovo. 
  Make sure you have the required raw data (! link to reuqired input data description).

| Initial steps to create a PostrgeSQL database on ENS virtual machine and connect to the db from local computer are listed below.

Install postgresql on linux
============================

| Since arbitrary package installation can be problematic due to the user rights, 
  postgresql can be installed inside a conda environment. Following instruction should be sufficient to create and run a database.
| https://gist.github.com/gwangjinkim/f13bf596fefa7db7d31c22efd1627c7a


Postgis & PGRouting
===================

| Postgis extension has to be installed via conda as well. The extension then can only be created by the base user

::

    conda install -c conda-forge postgis
    conda install -c conda-forge pgrouting
    # TODO hstore?



Access Database
===============

Inside the VM
--------------

| Within the vm the database is accessible with any tool like psql or from Python via engines.
| Basic inspection of db can be done within the bash with psql

::

    # psql -d db_name -U username 
    psql -d pylovo_db -U pylovo

Outside the VM (from ssh client a.k.a. your own computer)
---------------------------------------------------------

| To gain access to the pylovo database from your own machine you will need to request a username and password from the ENS chair.
| If you are working from your own machine you will also need to utilize a VPN to connect to the MWN network, 
  which us a prerequisite for connecting to the database server. We recommend using EduVPN_ for this purpose. 
  Follow the instructions in the link to set up a connection.
  
Once you have received username and password you can open a tunnel to the database in a terminal of your choice.
::
    ssh -L 1111:localhost:5432 [username]@10.195.1.137

You will need to connect to the database every time you use pylovo.

Create SQL functions
====================

Prewritten SQL functions must be created for once, when the database is created. Run the file syngrid/dump_functions.sql:

::

    psql -d syngrid_db -a -f "syngrid/dump_functions.sql"


Load raw data to the database
=============================

| pylovo requires the correct table structure and input data already loaded into the database. 
  Make sure that you have the raw data files (link to input data description) and paths configured in config_data.py

| Afterwards, the ETL process can be executed as:

::

    python main_constructor.py

Input data model (TODO add required columns, data type, description)
====================================================================

The minimum data model is described below:

* res
* oth
* betriebsmittel
* postcode
* ways 
* consumer_categories?
* transformers (optional)

Preprocess ways from OSM data
------------------------------

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
#. Navigate to newly created folder "public" and execute following command in the terminal:
    * psql -U syngrid -d syngrid_db -h localhost -p 1111 -f .\public_2po_4pgr.sql
#. Execute pylovo's main_constructor.py after table 2po_4pgr has been created in the database
    * make sure the ways_to_db method has been uncommented in main_constructor.py
    * the ways in the 2po_4pgr table will be inserted into the ways table and can now be used by pylovo

Preprocess transformers from OSM data (Optional)
-------------------------------------------------



.. _virtual environment: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment
.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB