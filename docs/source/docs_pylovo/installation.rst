Installation
****************

Prerequisites
=============

* Anaconda_: We strongly recommend setting up virtual environments for pylovo and the urbs optimizer.
* EduVPN_: If you are working from your own machine you will need a VPN to connect to the MWN network. A connection to the network is necessary for accessing the database server.

.. _Anaconda: https://www.anaconda.com/
.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB

Install package
===============

| pylovo is developed as a Python package and can be installed with pip, ideally by using a virtual environment.
| Start by cloning the repository from Github to a directory of your choice. The following command pulls the repository as well as the urbs submodule

::

    git clone --recurse-submodules --remote-submodules https://github.com/tum-ens/pylovo.git

Next we set up our virtual environments. Open the anaconda powershell prompt and begin
::
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    conda env create -f environment.yml
    conda env create -f urbs310.yml

You can test whether the environments have been properly created via
::
    #to enter a virtual environment
    conda activate TUM_Syngrid
    conda activate urbs310
    #to leave a virtual environment:
    conda deactivate

Finally we install pylovo from the local repository. The developer option installs additional packages.
It is crucial that you enter the newly created conda environment before installing pylovo.

User
----

::

    #activate environment
    conda activate TUM_Syngrid
    #navigate to pylovo main code folder
    cd path/to/git_repo/pylovo
    pip install -e .


Developer
---------

::

    #activate environment
    conda activate TUM_Syngrid
    #navigate to pylovo main code folder
    cd path/to/git_repo/pylovo
    pip install -e .[dev]

And with that pylovo should be ready to go! You can test whether everything went correctly by navigating 
to the IDP_Maptool_Flask folder and running the following commands  
::
    #activate environment
    conda activate TUM_Syngrid
    #navigate to maptool folder and start web server
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    flask --app maptool --debug run

This should start the flask server and allow you to open the GUI by navigating to http:127.0.0.1:5000 in a webbrowser of your choice


Advanced installation - Database construction
===============================================

| Follow the instructions below, only if you want to create a new database for pylovo. 
  Make sure you have the required raw data.

| Initial steps to create a PostrgeSQL database on ENS virtual machine and connect to the db from local computer are listed below.

Install postgresql on linux
============================

| Since arbitrary package installation can be problematic due to the user rights, 
  postgresql can be installed inside a conda environment. The instruction at the following link should be sufficient to create and run a database.
| https://gist.github.com/gwangjinkim/f13bf596fefa7db7d31c22efd1627c7a


Postgis & PGRouting
===================

| The postgis extension has to be installed via conda as well. The extension can only be created by the base user

::

    conda install -c conda-forge postgis
    conda install -c conda-forge pgrouting
    # TODO hstore?



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

Prewritten SQL functions must be created for once, when the database is created. Run the file syngrid/dump_functions.sql:

::

    psql -d syngrid_db -a -f "syngrid/dump_functions.sql"

Load raw data to the database
=============================

| pylovo requires the correct table structure and input data to already be loaded into the database. 
  Make sure that you have the raw data files and paths configured in config_data.py

| Afterwards, the ETL process can be executed as:

::

    python main_constructor.py

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


.. _virtual environment: https://realpython.com/what-is-pip/#using-pip-in-a-python-virtual-environment
.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB