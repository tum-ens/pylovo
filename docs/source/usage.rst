Usage
*****

.. _installation:


Installation
============

Prerequisites
-------------

* Anaconda_: We strongly recommend setting up virtual environments for pylovo and the urbs optimizer

.. _Anaconda: https://www.anaconda.com/

Setup
-----
Start by cloning the repository from Github to a directory of your choice. 
The following command pulls the repository as well as the urbs submodule::
    git clone --recurse-submodules --remote-submodules https://github.com/tum-ens/pylovo.git

Next we set up our virtual environments. 
Open the anaconda powershell prompt and begin::
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
to the IDP_Maptool_Flask folder and running the following commands::
    conda activate TUM_Syngrid
    cd path/to/repo/pylovo/gui/IDP_Maptool_Flask
    flask --app maptool --debug run

