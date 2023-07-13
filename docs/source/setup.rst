Setup
*****

.. _installation:

Prerequisites
=============

* Anaconda_: We strongly recommend setting up virtual environments for pylovo and the urbs optimizer
* EduVPN_: If you are working from your own machine you will need a VPN to connect to the MWN network, which is necessary for accessing the database server

.. _Anaconda: https://www.anaconda.com/
.. _EduVPN: https://doku.lrz.de/vpn-eduvpn-installation-und-konfiguration-11491448.html?showLanguage=en_GB


Installation
============

Start by cloning the repository from Github to a directory of your choice. The following command pulls the repository as well as the urbs submodule
::
    git clone --recurse-submodules --remote-submodules https://github.com/tum-ens/pylovo.git

Next we set up our virtual environments. Open the anaconda powershell prompt and begin
::
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    conda env create -f environment.yml
    conda env create -f urbs310.yml

You can test whether the environments have been properly created via
::
    conda activate TUM_Syngrid
    conda activate urbs310
    #to leave an environment:
    conda deactivate

Finally we install pylovo
::
    cd path/to/repo/pylovo/
    pip install -e .

And with that pylovo should be ready to go! You can test whether everything went correctly by navigating 
to the IDP_Maptool_Flask folder and running the following commands  
::
    #activate environment
    conda activate TUM_Syngrid
    #navigate to maptool folder and start web server
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    flask --app maptool --debug run

.. _database_connection:

Database connection
===================

| To gain access to the pylovo database you will need to request a username and password from the ENS chair.
| If you are working from your own machine you will also need to utilize a VPN to connect to the MWN network, 
  which us a prerequisite for connecting to the database server. We recommend using EduVPN_ for this purpose. 
  Follow the instructions in the link to set up a connection.
  
Once you have received username and password you can open a tunnel to the database in a terminal of your choice.
::
    ssh -L 1111:localhost:5432 [username]@10.195.1.137

You will need to connect to the database every time you use pylovo.

