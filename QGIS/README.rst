--------------------------------------
About the QGIS Project Templates:
--------------------------------------

This directory contains two QGIS project files that can be used as templates when accessing the pylovo database.

*template_local_db.qgz* is for accessing a local database with default values (localhost, port 5432,
dbname pylovo_db_local). It works out-of-the-box, but changing connection details of the database is cumbersome
because every single layer has to be edited.

*template_remote_db.qgz* on the other hand relies on the .pg_service.conf file for connection details, which makes it
easy to change the connection to any local or remote database but needs some additional configuration.

Apart from that, the two templates are identical.

------------------------------
Add the Service Configuration:
------------------------------

**On Linux:**
Copy the .pg_service.conf file to ~/.pg_service.conf

**On Windows:**
Add the environment variable PGSERVICEFILE over the Windows control panel and enter your preferred file path as value
(we suggest %userprofile%\.pg_service.conf). Then copy the .pg_service.conf file to that location.

After copying the file, you can edit its connection details (host, port, dbname, user, password) according to your
database. The layers in *template_remote_db.qgz* will automatically adopt the new datasource.

The concept of service config files is also described here:
https://docs.qgis.org/3.34/en/docs/user_manual/managing_data_source/opening_data.html#postgresql-service-connection-file.

------------------------------
Version Filtering and Styling:
------------------------------

QGIS projects have project-wide variables that can be edited by selecting *Project > Properties > Variables*.

For the grid data, multiple versions (generated with different parameter sets) may exist in the database. The versions
are identified by a unique version_id. The grid layers in the templates are filtered for a specific version. The
filtered version can be defined by setting the project variable *version_id*.

There are also some style settings applying to multiple layers that can be set by project variables. These are:
    * *building_border_thickness* - Thickness of building borders in meters in scale (corresponds to real-world size, not adjusted to zoom)
    * *circle_diameter* - Diameter of transformer/cluster markers in millimeters
    * *lines_thickness* - Thickness of power-supply lines in millimeters
    * *postcode_border_thickness* - Thickness of the border lines between postcode areas in millimeters
    * *ways_thickness* - Thickness of ways in millimeters

More on project variables can be found here:
https://docs.qgis.org/3.34/en/docs/user_manual/introduction/general_tools.html#storing-values-in-variables.