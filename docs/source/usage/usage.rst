Usage
*****

.. _maptool: http://127.0.0.1:5000


Starting the webserver
===========================
First, you need to activate the virtual environment, navigate to the tool directory and start the flask server
::
    #if you gave your env another name, use that one instead of TUM_Syngrid
    conda activate TUM_Syngrid
    cd path/to/pylovo/gui/IDP_Maptool_Flask
    flask --app maptool --debug run

We are setting multiple flags for running the server:

* **app**:  The name of the folder containing the init file for the flask code, in this case maptool
* **debug**: If this flag is not set, the server will not automatically restart, if adjustments to the code are made

Next, open a new terminal and connect to the pylovo database as described on the :ref:`setup page <database_connection>`

Accessing the tool
==================
| Open a browser of your choice and enter the address `http://127.0.0.1:5000 <http://127.0.0.1:5000>`_.
| You should now have the following view:

[INSERT IMAGE HERE]


Using the tool
==============

.. toctree::
    postcode_editor
    network_editor
    urbs_setup
    urbs_results

