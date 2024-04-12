QGIS Visualisation
******************

Open QGIS.

Visualisation structure
============================

The visualisation takes the geometry columns from the pylovo database. The layer names in QGIS correspond to the
database table names. In QGIS the layers have been grouped:

.. image:: ../../images/visualisation/layer_menu.png
    :width: 200
    :alt: Default view

Individual elements or groups can be selected for visualisation.

Raw Data
=========

Look at the geodata pylovo uses to generate the grids. This data does not change for different versions.

.. image:: ../../images/visualisation/qgis5.png
    :width: 600
    :alt: Default view

If you find any irregularities in your grid:
Check the raw data: Have the buildings been imported as expected? Are the ways complete and connected?

Grids
======

These are the results of the pylovo grid generation. If you have created networks for the same PLZ with multiple
versions
make sure to filter the version you would like to see.
Click on the filter symbol next to the layer name. In the query panel you can apply a filter like:
`"version_id"='3.8'`.

.. image:: ../../images/visualisation/qgis6.png
    :width: 600
    :alt: Default view

If you would only like to see a specific grid enter a query like:
`"version_id" = '3.3' AND "in_building_cluster" = '5' AND "k_mean_cluster" = '1'`.

Pylovo partitions the buildings of a PLZ for a grid using k-means cluster (kcid, k-means cluster ID) and
building cluster (bcid, builings cluster ID).

Some more specific information about the different layers:

Transformers
----------------

* black circle with white filling
* within circle: cluster - ID: [kcid].[bcid]
* e.g. 5.18: kcid 5, bcid 18
* transformers that a negative bcid are real transormers that were imported from OSM e.g. 1.-1

Buildings
----------------

The buildings (consumers) of each grid are colored in a different colour.
If you visualise a new postcode area that has a cluster ids that has not previously existed, e.g. 9.59 and 10.01,
they will have the same colour.
In this case douple click on the layer. Go to the `symbol` tab. click on `delete all`, `classify`.
Now for all cluster IDs the colours will be newly created.

Postcode
----------------

On the outside of the postcode area marked in red, you will find the PLZ code.


Clusters
---------------

By selecting the cluster layer, the cluster index for each grid can be shown. It can be identified by the color of the
transformer.

.. image:: ../../images/visualisation/clusters_qgis.png
    :width: 800
    :alt: Default view

Representative Grids
--------------------

Similarly the representative grids of the clusters can be shown.

.. image:: ../../images/visualisation/rep_grids_qgis.png
    :width: 800
    :alt: Default view

Visualize from csv
====================

With the files :code:`export_grid_gis_data_as_csv` and :code:`export_grid_gis_data_as_csv_for_multiple_plz` the
pandapower networks' information can exported to csv.  This is performed by the function

.. autofunction:: plotting.export_net.get_bus_line_geo_for_network

This way additional function about the grids can be visualised. This function would also enable to show grids in QGIS
that are not on the database.

.. image:: ../../images/visualisation/net_description.png
    :width: 800
    :alt: Default view
